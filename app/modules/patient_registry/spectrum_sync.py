"""Spectrum portal -> draft entries + reports. Idempotent, one patient at a time, one portal session per patient.

Per date window: log in, read the day's list, log out. Then for EACH pending row: logout + login + open that
row's date, scrape tests, Print that row's report, download the PDF, log out (the portal keeps selection state
in the session, so sharing a session between patients produced merged reports). The report is verified to
belong to the row (single patient, matching name) before it is stored or linked. Then the entry is built
(portal: name/age/gender/tests/lab_cost; report: everything else) and saved through the shared
`entry_service.upsert` (so the normal duplicate rules apply). Entries are drafts: no gross amount is sent.

Never-twice guarantees: ledger `spectrum_sync_items` (unique source_key), a pre-check against existing entries,
the entry dedupe rules and the unique dedupe_key, identical-PDF detection, and a DB lock between runs.
The ledger holds ids and statuses only (no patient values).
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError
from rapidfuzz import fuzz

from app.core.audit import COLLECTION as AUDIT_COLLECTION
from app.core.config import ConfigError, PortalConfig
from app.core.db import utcnow
from app.core.events import EventBus
from app.core.filestore import FileStore
from app.domain.entries import service as entry_service
from app.domain.entries.dedupe import find_matches
from app.domain.entries.model import EntryError, empty
from app.domain.parsing import ParseError, UnreadableFile, UploadError, parse_report, validate_upload
from app.domain.parsing import standardize as std
from app.domain.parsing.readers import safe_filename
from app.domain.reports import service as reports
from app.modules.patient_registry import service as pr_service
from app.partners.spectrum.portal import PortalAuthError, PortalRow, SpectrumPortalClient
from app.partners.spectrum.sync_config import PortalSecrets, SyncCfg, load_sync_config, portal_settings

log = logging.getLogger(__name__)

MODULE = "patient_registry"
SYSTEM_ACTOR = "system:spectrum_sync"
LEDGER = "spectrum_sync_items"
RUNS = "spectrum_sync_runs"
STATE = "spectrum_sync_state"
LOCK_ID = "lock"
PARSE_TIMEOUT_SECONDS = 120
STARTUP_DELAY_SECONDS = 20
NAME_MATCH_MIN = 0.8  # report name vs portal name (titles ignored); lower = a different patient's report
PARSED_KEYS = ("registration_date", "lab_pid", "referred_by", "collection_time", "reporting_time", "phone", "email")
_TASKS: set[asyncio.Task] = set()


class SyncBusy(RuntimeError):
    """Another sync run holds the lock."""


class SyncUnavailable(RuntimeError):
    """Sync is switched off or not configured."""


class SyncInputError(ValueError):
    """The requested month / date range is not usable."""


class RowError(Exception):
    """One row could not be completed; it is retried on a later run."""


# ───────────────────────────── context & parameters ─────────────────────────────
@dataclass
class SyncContext:
    db: AsyncDatabase
    cfg: PortalConfig
    sync_cfg: SyncCfg
    secrets: PortalSecrets
    store: FileStore
    bus: EventBus
    client_factory: Callable[[], SpectrumPortalClient] | None = None  # tests inject a fake portal


@dataclass(frozen=True)
class RunParams:
    date_from: date
    date_to: date
    trigger: str  # scheduled | catch_up | manual | job | cli
    dry_run: bool = False
    retry_failed: bool = False


@dataclass
class Stats:
    windows: int = 0
    rows_seen: int = 0
    already_done: int = 0
    exhausted: int = 0
    skipped_existing_entry: int = 0
    would_process: int = 0
    attempted: int = 0
    created: int = 0
    merged: int = 0
    unchanged: int = 0
    existing_conflict: int = 0
    needs_review: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def context_from_app(app) -> SyncContext:
    state = app.state
    if getattr(state, "spectrum_sync_cfg", None) is None:
        state.spectrum_sync_cfg = load_sync_config(state.settings.config_dir)
    if getattr(state, "spectrum_portal_secrets", None) is None:
        state.spectrum_portal_secrets = PortalSecrets()
    return SyncContext(
        db=state.db,
        cfg=state.config,
        sync_cfg=state.spectrum_sync_cfg,
        secrets=state.spectrum_portal_secrets,
        store=state.filestore,
        bus=state.events,
        client_factory=getattr(state, "spectrum_client_factory", None),
    )


# ───────────────────────────── dates ─────────────────────────────
def today_local(cfg: PortalConfig) -> date:
    return datetime.now(ZoneInfo(cfg.app.timezone)).date()


def resolve_window(
    cfg: PortalConfig,
    sync_cfg: SyncCfg,
    *,
    month: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[date, date]:
    """Month ("YYYY-MM"), an explicit range, or (default) the daily lookback window. Never beyond today."""
    today = today_local(cfg)
    if month and (date_from or date_to):
        raise SyncInputError("Give a month or a date range, not both.")
    if month:
        try:
            year, mon = int(month[:4]), int(month[5:7])
            first = date(year, mon, 1)
        except (ValueError, IndexError):
            raise SyncInputError("Use the month format YYYY-MM, for example 2026-10.") from None
        if first > today:
            raise SyncInputError("That month has not started yet.")
        return first, min(date(year, mon, calendar.monthrange(year, mon)[1]), today)
    if date_from or date_to:
        if not (date_from and date_to):
            raise SyncInputError("Give both date_from and date_to.")
        if date_from > date_to:
            raise SyncInputError("date_from must not be after date_to.")
        if date_from > today:
            raise SyncInputError("date_from is in the future.")
        if (date_to - date_from).days > 366:
            raise SyncInputError("Choose a range of at most one year.")
        return date_from, min(date_to, today)
    return today - timedelta(days=sync_cfg.daily_run.lookback_days), today


def chunk_windows(date_from: date, date_to: date, size: int) -> Iterator[tuple[date, date]]:
    cur = date_from
    while cur <= date_to:
        end = min(cur + timedelta(days=size - 1), date_to)
        yield cur, end
        cur = end + timedelta(days=1)


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _prev_month(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def source_key(partner_key: str, row: PortalRow) -> str:
    return f"{partner_key}|{row.key}"


# ───────────────────────────── storage: indexes, lock, runs ─────────────────────────────
async def ensure_indexes(db: AsyncDatabase) -> None:
    await db[LEDGER].create_index("source_key", unique=True)
    await db[LEDGER].create_index([("status", 1), ("sid_date", -1)])
    await db[LEDGER].create_index("entry_id", sparse=True)
    await db[RUNS].create_index("started_at", expireAfterSeconds=180 * 24 * 3600)
    await db[RUNS].create_index("local_date")


async def _acquire_lock(ctx: SyncContext, owner: str) -> bool:
    now = utcnow()
    try:
        doc = await ctx.db[STATE].find_one_and_update(
            {"_id": LOCK_ID, "$or": [{"locked_until": None}, {"locked_until": {"$lt": now}}]},
            {"$set": {"owner": owner, "locked_at": now, "locked_until": now + timedelta(minutes=ctx.sync_cfg.lock_minutes)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:  # the lock document exists and is still held
        return False
    return bool(doc) and doc.get("owner") == owner


async def _extend_lock(ctx: SyncContext, owner: str) -> None:
    await ctx.db[STATE].update_one(
        {"_id": LOCK_ID, "owner": owner},
        {"$set": {"locked_until": utcnow() + timedelta(minutes=ctx.sync_cfg.lock_minutes)}},
    )


async def _release_lock(ctx: SyncContext, owner: str) -> None:
    await ctx.db[STATE].update_one({"_id": LOCK_ID, "owner": owner}, {"$set": {"locked_until": None}})


def _check_ready(ctx: SyncContext) -> None:
    if not ctx.sync_cfg.enabled:
        raise SyncUnavailable("Spectrum sync is switched off (enabled: false in config/spectrum_sync.yaml).")
    partner = ctx.cfg.partners.get(ctx.sync_cfg.partner_key)
    if partner is None or MODULE not in partner.enabled_in:
        raise SyncUnavailable(f"Laboratory '{ctx.sync_cfg.partner_key}' is not enabled in patient_registry.")
    if ctx.client_factory is None and not ctx.secrets.configured:
        raise SyncUnavailable("Set SPECTRUM_PORTAL_USERNAME and SPECTRUM_PORTAL_PASSWORD in the environment.")


async def launch(ctx: SyncContext, params: RunParams, *, wait: bool = False) -> str:
    """Take the lock, record the run and start it in the background. Returns the run id."""
    _check_ready(ctx)
    owner = uuid.uuid4().hex
    if not await _acquire_lock(ctx, owner):
        raise SyncBusy("A Spectrum sync is already running.")
    try:
        await ctx.db[RUNS].update_many(
            {"status": "running"}, {"$set": {"status": "interrupted", "finished_at": utcnow()}}
        )  # we hold the lock, so anything still "running" died with its process
        res = await ctx.db[RUNS].insert_one(
            {
                "trigger": params.trigger,
                "status": "running",
                "dry_run": params.dry_run,
                "retry_failed": params.retry_failed,
                "date_from": params.date_from.isoformat(),
                "date_to": params.date_to.isoformat(),
                "local_date": today_local(ctx.cfg).isoformat(),
                "started_at": utcnow(),
                "finished_at": None,
                "message": None,
                "stats": Stats().as_dict(),
            }
        )
    except Exception:
        await _release_lock(ctx, owner)
        raise
    task = asyncio.create_task(_execute(ctx, res.inserted_id, owner, params), name="spectrum-sync")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    if wait:
        await task
    return str(res.inserted_id)


async def _execute(ctx: SyncContext, run_id: Any, owner: str, params: RunParams) -> None:
    runner: _Runner | None = None
    status, message = "failed", None
    try:
        factory = ctx.client_factory or (lambda: SpectrumPortalClient(portal_settings(ctx.sync_cfg, ctx.secrets)))
        async with factory() as client:
            runner = _Runner(ctx, client, run_id, owner, params)
            try:
                status, message = await runner.run()
            finally:
                await client.logout()  # never leave a portal session open
    except PortalAuthError as exc:
        status, message = "failed", str(exc)
        log.error("Spectrum sync: %s", exc)
    except ConfigError as exc:
        status, message = "failed", str(exc)
    except asyncio.CancelledError:
        status, message = "interrupted", "The application was shutting down."
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Spectrum sync failed")
        status, message = "failed", f"{type(exc).__name__}: {exc}"[:300]
    finally:
        try:
            if runner is not None:
                await runner.publish_months()
            await ctx.db[RUNS].update_one(
                {"_id": run_id},
                {
                    "$set": {
                        "status": status,
                        "message": message,
                        "finished_at": utcnow(),
                        **({"stats": runner.stats.as_dict()} if runner else {}),
                    }
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Spectrum sync: could not record the run result")
        finally:
            await _release_lock(ctx, owner)


# ───────────────────────────── the run ─────────────────────────────
@dataclass
class _Saved:
    status: str  # done | needs_review
    outcome: str
    entry: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None


class _Runner:
    def __init__(self, ctx: SyncContext, client: SpectrumPortalClient, run_id: Any, owner: str, params: RunParams):
        self.ctx, self.client, self.run_id, self.owner, self.params = ctx, client, run_id, owner, params
        self.partner = ctx.cfg.partners[ctx.sync_cfg.partner_key]
        self.stats = Stats()
        self.months: set[str] = set()
        self.last_entry: dict[str, Any] | None = None
        self.failures_in_a_row = 0

    @property
    def db(self) -> AsyncDatabase:
        return self.ctx.db

    def _key(self, row: PortalRow) -> str:
        return source_key(self.partner.key, row)

    async def run(self) -> tuple[str, str | None]:
        sync = self.ctx.sync_cfg
        budget = sync.max_entries_per_run
        for w_from, w_to in chunk_windows(self.params.date_from, self.params.date_to, sync.window_days):
            await self._heartbeat()
            rows = await self.client.fresh_session(w_from, w_to)  # session 1: only reads the day's list
            await self.client.logout()
            self.stats.windows += 1
            self.stats.rows_seen += len(rows)
            pending = await self._pending(rows)
            if self.params.dry_run:
                self.stats.would_process += len(pending)
                continue
            for row in pending:
                if budget <= 0:
                    return "partial", f"Stopped after {sync.max_entries_per_run} entries; the next run continues."
                budget -= 1
                await self._process(row)
                if self.failures_in_a_row >= sync.abort_after_consecutive_failures:
                    return "failed", "Stopped: several rows in a row failed (is the portal down?). See the item list."
                await self._heartbeat()
        return ("completed_with_errors" if self.stats.failed else "completed"), None

    async def _heartbeat(self) -> None:
        await _extend_lock(self.ctx, self.owner)
        await self.db[RUNS].update_one({"_id": self.run_id}, {"$set": {"stats": self.stats.as_dict()}})

    # ── which rows still need work (one bulk query per window)
    async def _pending(self, rows: list[PortalRow]) -> list[PortalRow]:
        keys = [self._key(r) for r in rows]
        ledger: dict[str, dict[str, Any]] = {}
        if keys:
            async for d in self.db[LEDGER].find({"source_key": {"$in": keys}}, {"status": 1, "attempts": 1}):
                ledger[d["source_key"]] = d
        pending: list[PortalRow] = []
        for row, key in zip(rows, keys, strict=True):
            d = ledger.get(key)
            if d:
                if d["status"] in ("done", "needs_review"):
                    self.stats.already_done += 1
                    continue
                if (
                    d["status"] == "failed"
                    and d.get("attempts", 0) >= self.ctx.sync_cfg.max_attempts
                    and not self.params.retry_failed
                ):
                    self.stats.exhausted += 1
                    continue
            else:
                existing = await self._existing_with_report(row)
                if existing is not None:
                    self.stats.skipped_existing_entry += 1
                    if not self.params.dry_run:
                        await self._record_existing(row, key, existing)
                    continue
            pending.append(row)
        return pending

    async def _existing_with_report(self, row: PortalRow) -> dict[str, Any] | None:
        if not self.ctx.sync_cfg.skip_if_entry_has_report or not row.gender or row.age is None:
            return None
        probe = {
            "partner_key": self.partner.key,
            "registration_date": row.sid_date.isoformat(),
            "patient_name": row.patient_name,
            "gender": row.gender,
            "age": row.age,
            "age_unit": row.age_unit,
        }
        m = await find_matches(self.db, self.ctx.cfg, {k: v for k, v in probe.items() if v is not None})
        if m.exact is None:
            return None
        return m.exact if await reports.reports_for_entry(self.db, m.exact) else None

    @staticmethod
    def _base(row: PortalRow, partner_key: str) -> dict[str, Any]:
        return {
            "partner_key": partner_key,
            "branch": row.branch_code or row.branch_name,
            "sid_no": row.sid_no,
            "sid_date": row.sid_date.isoformat(),
            "first_seen_at": utcnow(),
        }

    async def _record_existing(self, row: PortalRow, key: str, entry: dict[str, Any]) -> None:
        now = utcnow()
        await self.db[LEDGER].update_one(
            {"source_key": key},
            {
                "$set": {
                    "status": "done",
                    "outcome": "existing_entry",
                    "entry_id": str(entry["_id"]),
                    "updated_at": now,
                    "completed_at": now,
                    "run_id": str(self.run_id),
                },
                "$setOnInsert": {**self._base(row, self.partner.key), "attempts": 0},
            },
            upsert=True,
        )

    # ── one patient = one portal session
    async def _claim(self, row: PortalRow, key: str) -> dict[str, Any]:
        return await self.db[LEDGER].find_one_and_update(
            {"source_key": key},
            {
                "$set": {"status": "processing", "updated_at": utcnow(), "run_id": str(self.run_id)},
                "$inc": {"attempts": 1},
                "$setOnInsert": self._base(row, self.partner.key),
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def _process(self, row: PortalRow) -> None:
        key = self._key(row)
        claim = await self._claim(row, key)
        self.stats.attempted += 1
        warnings: list[str] = []
        try:
            # logout + login + the row's own date: nothing from any other patient can be in this session
            await self.client.fresh_session(row.sid_date, row.sid_date)
            tests = await self.client.fetch_tests(row)
            stored = await self._report(row, key, claim, warnings)
            saved = await self._save_entry(row, tests, stored, warnings)
        except (PortalAuthError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the run
            self.stats.failed += 1
            self.failures_in_a_row += 1
            log.warning("Spectrum sync: SID %s failed: %s", row.sid_no, exc)
            await self.db[LEDGER].update_one(
                {"source_key": key},
                {"$set": {"status": "failed", "last_error": f"{type(exc).__name__}: {exc}"[:300], "warnings": warnings, "updated_at": utcnow()}},
            )
            return
        finally:
            await self.client.logout()
        self.failures_in_a_row = 0
        now = utcnow()
        sets: dict[str, Any] = {
            "status": saved.status,
            "outcome": saved.outcome,
            "warnings": warnings,
            "last_error": None,
            "updated_at": now,
            "completed_at": now,
            **(saved.extra or {}),
        }
        if saved.entry is not None:
            sets["entry_id"] = str(saved.entry["_id"])
        await self.db[LEDGER].update_one({"source_key": key}, {"$set": sets})

    @staticmethod
    def _mismatch(row: PortalRow, patient: dict[str, Any], flags: list[str] | None) -> str | None:
        """Why this report cannot be this row's report (None = fine)."""
        if "mixed_patients" in (flags or []):
            return f"The downloaded report for SID {row.sid_no} contains more than one patient; not saved."
        name = patient.get("patient_name")
        if name:
            sim = fuzz.token_sort_ratio(std.normalize_name(name), std.normalize_name(row.patient_name)) / 100
            if sim < NAME_MATCH_MIN:
                return f"The downloaded report for SID {row.sid_no} belongs to a different patient; not saved."
        return None

    async def _report(self, row: PortalRow, key: str, claim: dict[str, Any], warnings: list[str]) -> reports.StoredReport:
        ctx = self.ctx
        if claim.get("report_ref"):  # an earlier attempt already stored the report: do not download again
            meta = await reports.get_meta(self.db, claim["report_ref"])
            if meta is not None and self._mismatch(row, meta.get("patient") or {}, (meta.get("parse") or {}).get("flags")) is None:
                return reports.StoredReport(claim["report_ref"], meta, False)
        scraped = await self.client.fetch_report(row)
        data = scraped.data
        filename = safe_filename(f"spectrum_{row.sid_no}_{row.sid_date:%Y-%m-%d}.pdf")
        try:
            validate_upload(filename, data, list(ctx.cfg.app.upload.allowed), ctx.cfg.app.upload.max_mb * 1024 * 1024)
        except UploadError as exc:
            raise RowError(f"The portal report was rejected: {exc.detail}") from None
        sha = hashlib.sha256(data).hexdigest()
        today = today_local(ctx.cfg)
        months = [_month_key(row.sid_date), _month_key(_prev_month(row.sid_date)), _month_key(today), _month_key(_prev_month(today))]
        stored = await reports.find_by_hash(self.db, sha, months)
        if stored is not None:
            problem = self._mismatch(row, stored.doc.get("patient") or {}, (stored.doc.get("parse") or {}).get("flags"))
            if problem:
                raise RowError(problem)
        else:
            result = None
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(parse_report, data, filename, self.partner, ctx.cfg), timeout=PARSE_TIMEOUT_SECONDS
                )
            except (TimeoutError, UnreadableFile, ParseError) as exc:
                if not ctx.sync_cfg.create_entry_when_report_unreadable:
                    raise RowError(f"The report could not be read: {exc}") from None
                warnings.append("report_unreadable")
            if result is not None:
                problem = self._mismatch(row, result.fields, result.flags)
                if problem:
                    raise RowError(problem)  # nothing is stored or linked
                if result.needs_review:
                    warnings.append("report_needs_review")
            reg = result.fields.get("registration_date") if result else None
            stored = await reports.store_report(
                self.db,
                ctx.store,
                partner_key=self.partner.key,
                filename=filename,
                content_type="application/pdf",
                data=data,
                parse=result,
                actor_id=SYSTEM_ACTOR,
                source="spectrum_portal",
                month_key=str(reg or row.sid_date.isoformat())[:7],
            )
        await self.db[LEDGER].update_one({"source_key": key}, {"$set": {"report_ref": stored.ref}})
        return stored

    def _raw(self, row: PortalRow, tests: list[str], patient: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        raw = {k: patient[k] for k in PARSED_KEYS if not empty(patient.get(k))}
        portal = {"patient_name": row.patient_name, "age": row.age, "age_unit": row.age_unit, "gender": row.gender}
        for k, v in portal.items():
            if not empty(v):
                raw[k] = v
            elif not empty(patient.get(k)):
                raw[k] = patient[k]
                warnings.append(f"{k}_from_report")
        raw["tests"] = tests  # portal tests are never overwritten by the parsed report
        if row.amount is not None:
            raw["lab_cost"] = row.amount  # likewise lab_cost
        if empty(raw.get("registration_date")):
            raw["registration_date"] = row.sid_date.isoformat()
            warnings.append("registration_date_from_sid")
        raw["partner_key"] = self.partner.key
        return raw

    @staticmethod
    def _distinct_people(raw: dict[str, Any], possible: list[dict[str, Any]]) -> bool:
        """Same-looking names but different Patient IDs on the same day are different people."""
        pid = re.sub(r"\s+", "", raw.get("lab_pid") or "").casefold()
        if not pid or not possible:
            return False
        return all(
            (other := re.sub(r"\s+", "", c.get("lab_pid") or "").casefold()) and other != pid for c in possible
        )

    async def _save_entry(
        self, row: PortalRow, tests: list[str], stored: reports.StoredReport, warnings: list[str]
    ) -> _Saved:
        ctx = self.ctx
        raw = self._raw(row, tests, stored.doc.get("patient") or {}, warnings)

        async def upsert(**kw: Any) -> entry_service.UpsertResult:
            try:
                return await entry_service.upsert(
                    self.db, ctx.cfg, raw, module=MODULE, actor_id=SYSTEM_ACTOR, preserve_financials=True, **kw
                )
            except EntryError as exc:
                raise RowError(f"The entry was rejected: {exc.detail}") from None

        res = await upsert()
        if res.decision is not None:
            d = res.decision
            if d.kind == "conflict" and d.match is not None:
                await self._link(stored, d.match, warnings)
                self.stats.existing_conflict += 1
                return _Saved("done", "existing_conflict", d.match, {"conflict_fields": [c["field"] for c in d.conflicts]})
            if d.kind == "possible_match" and self._distinct_people(raw, d.possible):
                res = await upsert(force_new=True)
            if res.decision is not None:
                self.stats.needs_review += 1
                return _Saved("needs_review", "possible_duplicate", None, {"possible_entry_ids": [str(c["_id"]) for c in d.possible]})
        entry = res.entry
        await self._link(stored, entry, warnings)
        if res.outcome == "created":
            self.stats.created += 1
        elif res.outcome == "merged":
            self.stats.merged += 1
        else:
            self.stats.unchanged += 1
        if res.outcome != "unchanged":
            self.months |= res.months
            self.last_entry = entry
            await self._audit(
                "entry.create_auto" if res.outcome == "created" else "entry.merge_auto",
                entry.get("bill_no"),
                sorted(raw) if res.outcome == "created" else res.filled,
            )
        return _Saved("done", res.outcome, entry)

    async def _link(self, stored: reports.StoredReport, entry: dict[str, Any], warnings: list[str]) -> None:
        others = [r for r in await reports.reports_for_entry(self.db, entry) if r["ref"] != stored.ref]
        if others:
            warnings.append("entry_has_other_report")  # a person should look: one visit normally has one report
        await reports.link_entry(self.db, stored.ref, str(entry["_id"]), entry["partner_key"])

    async def _audit(self, action: str, target: str | None, fields: list[str]) -> None:
        try:
            now = utcnow()
            await self.db[AUDIT_COLLECTION].insert_one(
                {
                    "ts": now,
                    "ts_local": now.astimezone(ZoneInfo(self.ctx.cfg.app.timezone)).isoformat(timespec="seconds"),
                    "actor_id": None,
                    "actor_email": SYSTEM_ACTOR,
                    "role": "system",
                    "action": action,
                    "module": MODULE,
                    "submodule": self.partner.key,
                    "target": target,
                    "ip": None,
                    "device": "spectrum_sync",
                    "outcome": "success",
                    "fields_changed": fields,
                }
            )
        except Exception:  # noqa: BLE001
            log.exception("Spectrum sync: audit write failed")

    async def publish_months(self) -> None:
        """One Excel-register refresh for the whole run instead of one per entry."""
        if self.months and self.last_entry is not None and not self.params.dry_run:
            await pr_service.publish_saved(
                self.ctx.bus,
                self.db,
                self.ctx.cfg,
                entry=self.last_entry,
                months=self.months,
                action="auto_sync",
                actor_id=SYSTEM_ACTOR,
            )


# ───────────────────────────── startup & scheduler ─────────────────────────────
async def on_startup(app) -> None:
    sync_cfg = load_sync_config(app.state.settings.config_dir)
    app.state.spectrum_sync_cfg = sync_cfg
    app.state.spectrum_portal_secrets = PortalSecrets()
    if sync_cfg.enabled:
        partner = app.state.config.partners.get(sync_cfg.partner_key)
        if partner is None or MODULE not in partner.enabled_in:
            raise ConfigError([f"spectrum_sync.yaml: partner_key '{sync_cfg.partner_key}' is not a lab enabled in patient_registry"])
    try:
        await ensure_indexes(app.state.db)
    except Exception:  # noqa: BLE001
        log.exception("Spectrum sync: could not create indexes")
    start_scheduler(app)


def start_scheduler(app) -> asyncio.Task | None:
    cfg: SyncCfg = app.state.spectrum_sync_cfg
    if app.state.settings.environment == "test" or not (cfg.enabled and cfg.schedule.enabled):
        return None
    if not app.state.spectrum_portal_secrets.configured:
        log.warning("Spectrum sync is enabled but SPECTRUM_PORTAL_USERNAME / PASSWORD are not set; scheduler not started")
        return None
    task = asyncio.create_task(_scheduler_loop(app), name="spectrum-sync-scheduler")
    app.state.spectrum_sync_scheduler = task
    log.info("Spectrum sync scheduled daily at %s (%s)", cfg.schedule.run_time, app.state.config.app.timezone)
    return task


async def _sleep_until(tz: ZoneInfo, hour: int, minute: int) -> None:
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while (remaining := (target - datetime.now(tz)).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, 60))


async def _scheduled_run(app, trigger: str) -> None:
    try:
        ctx = context_from_app(app)
        d_from, d_to = resolve_window(ctx.cfg, ctx.sync_cfg)
        await launch(ctx, RunParams(d_from, d_to, trigger), wait=True)
    except SyncBusy:
        log.info("Spectrum sync: a run is already in progress; %s run skipped", trigger)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Spectrum sync: %s run could not start", trigger)


async def _scheduler_loop(app) -> None:
    cfg: SyncCfg = app.state.spectrum_sync_cfg
    tz = ZoneInfo(app.state.config.app.timezone)
    hour, minute = (int(p) for p in cfg.schedule.run_time.split(":"))
    if cfg.schedule.catch_up_on_start:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        now = datetime.now(tz)
        if (now.hour, now.minute) >= (hour, minute):
            try:
                done = await app.state.db[RUNS].find_one(
                    {
                        "local_date": now.date().isoformat(),
                        "trigger": {"$in": ["scheduled", "catch_up"]},
                        "status": {"$in": ["completed", "completed_with_errors", "partial"]},
                    }
                )
            except Exception:  # noqa: BLE001
                log.exception("Spectrum sync: catch-up check failed")
                done = True
            if not done:
                await _scheduled_run(app, "catch_up")
    while True:
        await _sleep_until(tz, hour, minute)
        await _scheduled_run(app, "scheduled")