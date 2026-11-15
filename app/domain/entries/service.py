"""The ONE place entries are created, merged, edited and numbered. Every module calls these functions.

Bill numbers: the entry is inserted first (the unique keys settle duplicate races), then the next counter value is
assigned. A failed duplicate insert therefore never burns a bill number. If the process dies between the two steps,
`assign_missing_bill_nos` completes the entry at the next start.

A lab that prints its own bill number (Metropolis) replaces the internal one through `replace_bill_no`; the internal
number is kept in `bill_no_original` and the counter ignores lab numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import PortalConfig
from app.core.db import utcnow
from app.domain.customers import service as customers
from app.domain.entries import repository as repo
from app.domain.entries.dedupe import find_matches
from app.domain.entries.model import (
    EDITABLE,
    EntryError,
    check_final,
    clean_fields,
    derive,
    empty,
    month_of,
    same_value,
)
from app.domain.parsing import standardize as std

log = logging.getLogger(__name__)
COUNTERS = "counters"
COUNTER_ID = "bill_no"
MERGE_FIELDS = [f for f in EDITABLE if f not in ("partner_key", "registration_date")]
PRESERVED_BILL_FIELDS = ("gross_amount", "discount_amount", "lab_cost", "net_amount", "margin", "status")


class _VersionConflict(Exception):
    pass


@dataclass
class Decision:
    kind: str  # possible_match | conflict
    message: str
    match: dict[str, Any] | None = None
    possible: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    can_force_new: bool = True


@dataclass
class UpsertResult:
    outcome: str  # created | merged | unchanged | decision
    entry: dict[str, Any] | None = None
    filled: list[str] = field(default_factory=list)
    months: set[str] = field(default_factory=set)
    decision: Decision | None = None


# ───────────────────────────── bill_no ─────────────────────────────
async def _highest_issued(db: AsyncDatabase) -> int:
    """Highest INTERNAL number: lab numbers that replaced one are ignored; their `bill_no_original` counts."""
    best = 0
    queries = (
        ("bill_no", {"bill_no": {"$exists": True}, "bill_no_original": {"$exists": False}}),
        ("bill_no_original", {"bill_no_original": {"$exists": True}}),
    )
    for name, flt in queries:
        top = await db[repo.COL].find(flt).sort(name, -1).limit(1).to_list(length=1)
        try:
            if top:
                best = max(best, int(top[0][name]))
        except (KeyError, ValueError):
            continue
    return best


async def ensure_counter(db: AsyncDatabase, cfg: PortalConfig) -> None:
    """First launch: start from the highest existing bill_no (or the configured start)."""
    floor = max(cfg.app.bill_no.start - 1, await _highest_issued(db))
    try:
        await db[COUNTERS].update_one({"_id": COUNTER_ID}, {"$setOnInsert": {"seq": floor}}, upsert=True)
    except DuplicateKeyError:
        pass
    await db[COUNTERS].update_one({"_id": COUNTER_ID, "seq": {"$lt": floor}}, {"$set": {"seq": floor}})


async def next_bill_no(db: AsyncDatabase, cfg: PortalConfig) -> str:
    """Atomic increment: two staff saving at the same moment never get the same number."""
    try:
        await db[COUNTERS].update_one(
            {"_id": COUNTER_ID}, {"$setOnInsert": {"seq": cfg.app.bill_no.start - 1}}, upsert=True
        )
    except DuplicateKeyError:
        pass
    doc = await db[COUNTERS].find_one_and_update(
        {"_id": COUNTER_ID}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return str(doc["seq"]).zfill(cfg.app.bill_no.digits)


async def assign_missing_bill_nos(db: AsyncDatabase, cfg: PortalConfig) -> int:
    pending = await db[repo.COL].find({"bill_no": {"$exists": False}}).sort("created_at", 1).to_list(length=1000)
    for e in pending:
        await db[repo.COL].update_one(
            {"_id": e["_id"], "bill_no": {"$exists": False}}, {"$set": {"bill_no": await next_bill_no(db, cfg)}}
        )
    return len(pending)


async def bill_no_owner(db: AsyncDatabase, bill_no: str) -> dict[str, Any] | None:
    """The entry that currently holds this bill number, if any (read only)."""
    return await db[repo.COL].find_one({"bill_no": bill_no}, {"_id": 1})


async def replace_bill_no(
    db: AsyncDatabase, cfg: PortalConfig, entry_id: str, lab_bill_no: str, *, actor_id: str
) -> tuple[dict[str, Any], bool]:
    """A lab's own bill number replaces `bill_no`. The internal number is kept in `bill_no_original`.

    Returns (entry, replaced). Idempotent: the same number again changes nothing. A number held by another entry
    is refused with 409 `bill_no_taken` and nothing is written.
    """
    existing = await repo.get_entry(db, entry_id)
    if existing is None:
        raise EntryError(404, "Entry not found.")
    if existing.get("bill_no") == lab_bill_no:
        return existing, False
    taken = {
        "code": "bill_no_taken",
        "message": f"Bill No {lab_bill_no} is already used by another entry.",
    }
    owner = await db[repo.COL].find_one({"bill_no": lab_bill_no, "_id": {"$ne": existing["_id"]}}, {"_id": 1})
    if owner is not None:
        raise EntryError(409, taken)
    internal = existing.get("bill_no") or await next_bill_no(db, cfg)
    sets: dict[str, Any] = {
        "bill_no": lab_bill_no,
        "bill_no_original": existing.get("bill_no_original") or internal,
        "updated_at": utcnow(),
        "updated_by": actor_id,
    }
    try:
        res = await db[repo.COL].update_one(
            {"_id": existing["_id"], "version": existing.get("version", 1)},
            {"$set": sets, "$inc": {"version": 1}},
        )
    except DuplicateKeyError:
        raise EntryError(409, taken) from None
    if res.matched_count == 0:
        raise EntryError(409, "This entry was changed by someone else a moment ago. Please try again.")
    return await db[repo.COL].find_one({"_id": existing["_id"]}), True


# ───────────────────────────── helpers ─────────────────────────────
def months_of(*docs: dict[str, Any] | None) -> set[str]:
    return {month_of(d["registration_date"]) for d in docs if d and d.get("registration_date")}


def _require_lab(cfg: PortalConfig, partner_key: str | None, module: str) -> None:
    p = cfg.partners.get(partner_key or "")
    if p is None or module not in p.enabled_in:
        raise EntryError(422, "Please choose a valid laboratory for this module.")


def _label(cfg: PortalConfig, key: str) -> str:
    return next((f.label for f in cfg.fields if f.key == key), key)


def plan_merge(
    cfg: PortalConfig, existing: dict[str, Any], inc: dict[str, Any], resolutions: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Empty fields are filled; differing values are conflicts unless staff already chose."""
    fill: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for f in MERGE_FIELDS:
        new = inc.get(f)
        if empty(new):
            continue
        old = existing.get(f)
        if empty(old) or (f == "discount_amount" and old == 0):
            fill[f] = new
        elif same_value(f, old, new):
            continue
        else:
            choice = resolutions.get(f)
            if choice == "incoming":
                fill[f] = new
            elif choice == "both" and f == "tests":
                fill[f] = std.clean_test_list([*old, *new])
            elif choice != "existing":
                conflicts.append({"field": f, "label": _label(cfg, f), "existing": old, "incoming": new})
    return fill, conflicts


async def _apply(
    db: AsyncDatabase,
    existing: dict[str, Any],
    changes: dict[str, Any],
    actor_id: str,
    *,
    preserve_financials: bool = False,
) -> dict[str, Any]:
    merged = dict(existing)
    for k, v in changes.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    derived = derive(merged)
    values = {**changes, **derived}
    if preserve_financials:
        values.update({key: existing.get(key) for key in PRESERVED_BILL_FIELDS})
    ops: dict[str, Any] = {
        "$set": {
            **{k: v for k, v in values.items() if v is not None},
            "updated_at": utcnow(),
            "updated_by": actor_id,
        },
        "$inc": {"version": 1},
    }
    unset = {k: "" for k, v in values.items() if v is None}
    if unset:
        ops["$unset"] = unset
    res = await db[repo.COL].update_one({"_id": existing["_id"], "version": existing.get("version", 1)}, ops)
    if res.matched_count == 0:
        raise _VersionConflict
    return await db[repo.COL].find_one({"_id": existing["_id"]})


# ───────────────────────────── create / merge ─────────────────────────────
async def _create(
    db: AsyncDatabase,
    cfg: PortalConfig,
    inc: dict[str, Any],
    *,
    module: str,
    actor_id: str,
    resolutions: dict,
    preserve_financials: bool = False,
) -> UpsertResult:
    final = clean_fields(inc, cfg, defaults=True)
    final = {k: v for k, v in final.items() if not empty(v)}
    check_final(cfg, module, final)
    now = utcnow()
    doc = {
        **final,
        **{k: v for k, v in derive(final).items() if v is not None},
        "version": 1,
        "created_at": now,
        "created_by": actor_id,
        "updated_at": now,
        "updated_by": actor_id,
    }
    try:
        res = await db[repo.COL].insert_one(doc)
    except DuplicateKeyError:  # lost a race with a simultaneous save of the same patient
        m = await find_matches(db, cfg, inc)
        if m.exact:
            return await _merge(
                db,
                cfg,
                m.exact,
                inc,
                module=module,
                actor_id=actor_id,
                resolutions=resolutions,
                preserve_financials=preserve_financials,
            )
        raise EntryError(409, "This patient was just saved by someone else. Please reload and check.") from None
    eid = res.inserted_id
    await db[repo.COL].update_one(
        {"_id": eid, "bill_no": {"$exists": False}}, {"$set": {"bill_no": await next_bill_no(db, cfg)}}
    )
    entry = await db[repo.COL].find_one({"_id": eid})
    cust = await customers.link_entry_customer(db, cfg, entry)
    if cust:
        await db[repo.COL].update_one({"_id": eid}, {"$set": {"customer_id": cust}})
        entry["customer_id"] = cust
    return UpsertResult("created", entry=entry, months=months_of(entry))


async def _merge(
    db: AsyncDatabase,
    cfg: PortalConfig,
    existing: dict[str, Any],
    inc: dict[str, Any],
    *,
    module: str,
    actor_id: str,
    resolutions: dict[str, str],
    preserve_financials: bool = False,
) -> UpsertResult:
    if preserve_financials:
        inc = {k: v for k, v in inc.items() if k not in ("gross_amount", "discount_amount", "lab_cost")}
    for _ in range(3):
        fill, conflicts = plan_merge(cfg, existing, inc, resolutions)
        if conflicts:
            same_id = (
                inc.get("lab_pid")
                and existing.get("lab_pid")
                and inc["lab_pid"].casefold() == existing["lab_pid"].casefold()
            )
            return UpsertResult(
                "decision",
                decision=Decision(
                    "conflict",
                    "This patient is already saved for this lab and date. Choose which value to keep for each field.",
                    match=existing,
                    conflicts=conflicts,
                    can_force_new=not same_id,
                ),
            )
        if not fill:
            return UpsertResult("unchanged", entry=existing, months=months_of(existing))
        check_final(cfg, module, {**existing, **fill})
        try:
            new = await _apply(db, existing, fill, actor_id, preserve_financials=preserve_financials)
        except _VersionConflict:
            existing = await repo.get_entry(db, str(existing["_id"])) or existing
            continue
        except DuplicateKeyError:
            raise EntryError(409, "Another entry already uses this Patient ID for this lab and date.") from None
        if not new.get("customer_id"):
            cust = await customers.link_entry_customer(db, cfg, new)
            if cust:
                await db[repo.COL].update_one({"_id": new["_id"]}, {"$set": {"customer_id": cust}})
                new["customer_id"] = cust
        return UpsertResult("merged", entry=new, filled=sorted(fill), months=months_of(existing, new))
    raise EntryError(409, "This entry was changed by someone else a moment ago. Please try again.")


async def upsert(
    db: AsyncDatabase,
    cfg: PortalConfig,
    raw: dict[str, Any],
    *,
    module: str,
    actor_id: str,
    target_entry_id: str | None = None,
    force_new: bool = False,
    resolutions: dict[str, str] | None = None,
    preserve_financials: bool = False,
) -> UpsertResult:
    """Create a new entry or update the existing one, after the duplicate check."""
    resolutions = resolutions or {}
    if target_entry_id and force_new:
        raise EntryError(400, "Choose an existing entry or 'different patient', not both.")
    inc = {k: v for k, v in clean_fields(raw, cfg, defaults=False).items() if not empty(v)}
    _require_lab(cfg, inc.get("partner_key"), module)
    for key in ("patient_name", "registration_date"):
        if key not in inc:
            raise EntryError(422, f"{_label(cfg, key)} is required.")

    if target_entry_id:
        target = await repo.get_entry(db, target_entry_id)
        if target is None:
            raise EntryError(404, "The chosen entry was not found.")
        if target["partner_key"] != inc["partner_key"] or target["registration_date"] != inc["registration_date"]:
            raise EntryError(400, "A match must have the same lab and registration date.")
        return await _merge(
            db,
            cfg,
            target,
            inc,
            module=module,
            actor_id=actor_id,
            resolutions=resolutions,
            preserve_financials=preserve_financials,
        )

    m = await find_matches(db, cfg, inc)
    if m.exact and not force_new:
        return await _merge(
            db,
            cfg,
            m.exact,
            inc,
            module=module,
            actor_id=actor_id,
            resolutions=resolutions,
            preserve_financials=preserve_financials,
        )
    if m.exact and m.rule == 1:
        raise EntryError(
            409,
            "This Patient ID is already saved for this lab on this date, so it is the same patient. "
            "If the ID was typed wrongly, correct it first.",
        )
    if m.possible and not m.exact and not force_new:
        return UpsertResult(
            "decision",
            decision=Decision(
                "possible_match",
                "A similar patient is already saved for this lab and date. Is it the same person?",
                possible=m.possible,
            ),
        )
    return await _create(
        db,
        cfg,
        inc,
        module=module,
        actor_id=actor_id,
        resolutions=resolutions,
        preserve_financials=preserve_financials,
    )


# ───────────────────────────── edit / delete ─────────────────────────────
async def update_entry(
    db: AsyncDatabase,
    cfg: PortalConfig,
    entry_id: str,
    patch: dict[str, Any],
    *,
    module: str,
    actor_id: str,
    expected_version: int | None = None,
    preserve_financials: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Edit the same record (bill_no never changes). Returns (before, after, names of changed fields)."""
    existing = await repo.get_entry(db, entry_id)
    if existing is None:
        raise EntryError(404, "Entry not found.")
    if expected_version is not None and expected_version != existing.get("version", 1):
        raise EntryError(409, "This entry was changed by someone else. Please reload it.")
    if "partner_key" in patch:
        raise EntryError(422, "The laboratory of an entry cannot be changed.")
    cleaned = clean_fields(patch, cfg, defaults=False)
    if preserve_financials:
        cleaned = {k: v for k, v in cleaned.items() if k not in ("gross_amount", "discount_amount", "lab_cost")}
    for key in ("patient_name", "registration_date"):
        if key in cleaned and cleaned[key] is None:
            raise EntryError(422, f"{_label(cfg, key)} cannot be empty.")
    changes = {
        k: v
        for k, v in cleaned.items()
        if not (empty(existing.get(k)) and empty(v)) and (empty(existing.get(k)) or empty(v) or not same_value(k, existing[k], v))
    }
    if not changes:
        return existing, existing, []
    merged = {**existing, **{k: v for k, v in changes.items() if v is not None}}
    for k, v in changes.items():
        if v is None:
            merged.pop(k, None)
    check_final(cfg, module, merged)
    probe = {k: merged[k] for k in EDITABLE if not empty(merged.get(k))}
    m = await find_matches(db, cfg, probe, exclude_id=existing["_id"])
    if m.exact:
        raise EntryError(
            409,
            f"Another entry (Bill No {m.exact.get('bill_no')}) already matches this patient on this date. "
            "Open that entry instead of creating a second one.",
        )
    try:
        new = await _apply(db, existing, changes, actor_id, preserve_financials=preserve_financials)
    except _VersionConflict:
        raise EntryError(409, "This entry was changed by someone else. Please reload it.") from None
    except DuplicateKeyError:
        raise EntryError(409, "Another entry already uses this Patient ID for this lab and date.") from None
    return existing, new, sorted(changes)


async def delete(db: AsyncDatabase, entry_id: str) -> dict[str, Any]:
    """Admin deletion. The bill_no stays retired: the counter never goes back."""
    doc = await repo.delete_entry(db, entry_id)
    if doc is None:
        raise EntryError(404, "Entry not found.")
    return doc