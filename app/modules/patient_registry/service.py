"""patient_registry glue: lab scope, report upload + parsing, API shaping, startup.
Business rules live in `app/domain` (shared by every module)."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import PartnerCfg, PortalConfig
from app.core.db import as_utc
from app.core.deps import CurrentUser
from app.core.events import EventBus
from app.core.filestore import FileStore
from app.core.rbac import decide_access, grants_for, visibility_map
from app.domain import excel_register
from app.domain.customers import service as customers
from app.domain.entries import service as entry_service
from app.domain.entries.dedupe import find_matches
from app.domain.entries.model import EDITABLE
from app.domain.parsing import ParseError, ParseResult, UnreadableFile, UploadError, parse_report, validate_upload
from app.domain.parsing.readers import safe_filename
from app.domain.reports import service as reports
from app.modules.patient_registry.schemas import (
    DuplicateDecisionOut,
    DuplicateHintOut,
    EntryOut,
    EntrySummaryOut,
    FieldConflictOut,
    ParsedFieldsOut,
    ParseOut,
    ReportRefOut,
)

log = logging.getLogger(__name__)
MODULE = "patient_registry"
PARSE_TIMEOUT_SECONDS = 25


async def on_startup(app) -> None:
    from app.domain.entries import repository as entry_repo

    db, cfg = app.state.db, app.state.config
    await entry_repo.ensure_indexes(db)
    await customers.ensure_indexes(db)
    await excel_register.ensure_indexes(db)
    await entry_service.ensure_counter(db, cfg)
    fixed = await entry_service.assign_missing_bill_nos(db, cfg)
    if fixed:
        log.warning("assigned bill numbers to %d entries that were interrupted before numbering", fixed)


# ───────────────────────────── access ─────────────────────────────
@dataclass
class Scope:
    user: CurrentUser
    labs: set[str]

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


async def allowed_labs(db: AsyncDatabase, cfg: PortalConfig, user: CurrentUser) -> set[str]:
    """Labs this user may work with: all for Admin or a module-wide grant, else only granted labs."""
    labs = [p.key for p in cfg.labs_for(MODULE)]
    if user.role == "admin":
        return set(labs)
    mod = cfg.module(MODULE)
    visible = (await visibility_map(db, cfg))[MODULE]
    granted = {s for s, st in (await grants_for(db, user.id)).get(MODULE, {}).items() if st == "granted"}
    return {k for k in labs if decide_access(mod, user.role, visible, granted, k)}


# ───────────────────────────── shaping responses ─────────────────────────────
def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def summary_out(doc: dict[str, Any]) -> EntrySummaryOut:
    return EntrySummaryOut(
        id=str(doc["_id"]),
        bill_no=doc.get("bill_no"),
        status=doc.get("status", "draft"),
        partner_key=doc["partner_key"],
        patient_name=doc["patient_name"],
        age=doc.get("age"),
        age_unit=doc.get("age_unit"),
        gender=doc.get("gender"),
        lab_pid=doc.get("lab_pid"),
        referred_by=doc.get("referred_by"),
        registration_date=date.fromisoformat(doc["registration_date"]),
        tests=doc.get("tests", []),
        net_amount=doc.get("net_amount"),
    )


def entry_out(doc: dict[str, Any], cfg: PortalConfig, *, admin: bool) -> EntryOut:
    p = cfg.partners.get(doc["partner_key"])
    return EntryOut(
        **summary_out(doc).model_dump(),
        version=doc.get("version", 1),
        partner_name=p.name if p else doc["partner_key"],
        collection_time=_dt(doc.get("collection_time")),
        reporting_time=_dt(doc.get("reporting_time")),
        phone=doc.get("phone"),
        email=doc.get("email"),
        lab_cost=doc.get("lab_cost"),
        gross_amount=doc.get("gross_amount"),
        discount_amount=doc.get("discount_amount"),
        margin=doc.get("margin") if admin else None,  # admin_only field (fields.yaml)
        customer_id=doc.get("customer_id"),
        created_at=as_utc(doc["created_at"]),
        updated_at=as_utc(doc["updated_at"]),
    )


def report_ref_out(d: dict[str, Any]) -> ReportRefOut:
    f = d["file"]
    return ReportRefOut(
        id=d["ref"],
        filename=f["name"],
        content_type=f["content_type"],
        size=f["size"],
        status=d.get("status", "stored"),
        created_at=as_utc(d["created_at"]),
    )


def decision_out(d: entry_service.Decision) -> DuplicateDecisionOut:
    return DuplicateDecisionOut(
        outcome=d.kind,  # type: ignore[arg-type]
        message=d.message,
        match=summary_out(d.match) if d.match else None,
        possible_matches=[summary_out(e) for e in d.possible],
        conflicts=[FieldConflictOut(**c) for c in d.conflicts],
        can_force_new=d.can_force_new,
    )


async def publish_saved(
    bus: EventBus, db: AsyncDatabase, cfg: PortalConfig, *, entry: dict[str, Any], months: set[str], action: str, actor_id: str
) -> None:
    await bus.publish(
        "entry.saved",
        {
            "entry_id": str(entry["_id"]),
            "bill_no": entry.get("bill_no"),
            "status": entry.get("status"),
            "months": sorted(months),
            "action": action,
            "actor_id": actor_id,
            "db": db,
            "cfg": cfg,
        },
    )


# ───────────────────────────── upload + parse ─────────────────────────────
def _month_keys(cfg: PortalConfig) -> tuple[str, str]:
    today = datetime.now(ZoneInfo(cfg.app.timezone)).date()
    prev = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
    return today.strftime("%Y-%m"), prev.strftime("%Y-%m")


async def _duplicate_hint(db: AsyncDatabase, cfg: PortalConfig, fields: dict[str, Any]) -> DuplicateHintOut | None:
    m = await find_matches(db, cfg, fields)
    if m.exact:
        return DuplicateHintOut(kind="exact", entries=[summary_out(m.exact)])
    if m.possible:
        return DuplicateHintOut(kind="possible", entries=[summary_out(e) for e in m.possible])
    return None


async def parse_and_store(
    db: AsyncDatabase,
    cfg: PortalConfig,
    store: FileStore,
    *,
    partner: PartnerCfg,
    filename: str,
    data: bytes,
    actor_id: str,
) -> ParseOut:
    """Validate, parse with the chosen lab's rules and store the report. The same file is never stored twice."""
    filename = safe_filename(filename)
    kind = validate_upload(filename, data, list(cfg.app.upload.allowed), cfg.app.upload.max_mb * 1024 * 1024)
    content_type = "application/pdf" if kind == "pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    sha = hashlib.sha256(data).hexdigest()
    this_month, last_month = _month_keys(cfg)

    stored = await reports.find_by_hash(db, sha, [this_month, last_month])
    result: ParseResult | None = None
    if stored is None:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(parse_report, data, filename, partner, cfg), timeout=PARSE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise UploadError(422, "Reading this file took too long. Please enter the details manually.") from None
        except UnreadableFile as exc:
            raise UploadError(422, str(exc)) from None
        except ParseError as exc:
            raise UploadError(400, str(exc)) from None
        month = (result.fields.get("registration_date") or this_month + "-01")[:7]
        stored = await reports.store_report(
            db,
            store,
            partner_key=partner.key,
            filename=filename,
            content_type=content_type,
            data=data,
            parse=result,
            actor_id=actor_id,
            source="upload",
            month_key=month,
        )
    duplicate_file = result is None or not stored.created
    doc = stored.doc
    fields = {k: v for k, v in doc.get("patient", {}).items() if v not in (None, "", [])}
    parse = doc.get("parse", {})
    hint = None
    if fields.get("patient_name") and fields.get("registration_date"):
        hint = await _duplicate_hint(db, cfg, {**fields, "partner_key": partner.key})
    return ParseOut(
        report_id=stored.ref,
        filename=doc["file"]["name"],
        partner_key=partner.key,
        file_duplicate=duplicate_file,
        existing_entry_id=doc.get("entry_id"),
        fields=ParsedFieldsOut(**{k: v for k, v in fields.items() if k in ParsedFieldsOut.model_fields}),
        confidence=float(parse.get("confidence", 0.0)),
        needs_verify=list(parse.get("needs_verify", [])),
        missing_fields=list(parse.get("missing_fields", [])),
        flags=list(parse.get("flags", [])),
        warnings=list(parse.get("warnings", [])),
        duplicate=hint,
    )


def input_fields(body_dump: dict[str, Any]) -> dict[str, Any]:
    """Keep only editable standard fields from a request dump."""
    return {k: v for k, v in body_dump.items() if k in EDITABLE}