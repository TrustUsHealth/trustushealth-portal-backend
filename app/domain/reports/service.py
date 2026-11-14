"""Stored reports in monthly collections `reports_YYYY_MM`. Files stay in the same document as metadata;
every list/lookup excludes the bytes."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.db import utcnow
from app.core.filestore import NO_FILE_DATA, FileStore, StoredFile
from app.domain.parsing.engine import ParseResult

log = logging.getLogger(__name__)
_REF_RE = re.compile(r"^(\d{4})_(0[1-9]|1[0-2]):([0-9a-f]{24})$")
_indexed: set[tuple[int, str]] = set()


@dataclass
class StoredReport:
    ref: str  # "YYYY_MM:<objectid>"
    doc: dict[str, Any]  # metadata only (no file bytes)
    created: bool


def month_collection(month_key: str) -> str:
    return "reports_" + month_key.replace("-", "_")


def make_ref(month_key: str, oid: ObjectId) -> str:
    return f"{month_key.replace('-', '_')}:{oid}"


def parse_ref(ref: str) -> tuple[str, ObjectId] | None:
    m = _REF_RE.match(ref or "")
    return (f"reports_{m.group(1)}_{m.group(2)}", ObjectId(m.group(3))) if m else None


async def ensure_indexes(db: AsyncDatabase, coll: str) -> None:
    key = (id(db), coll)
    if key in _indexed:
        return
    c = db[coll]
    await c.create_index("file.sha256", unique=True)  # same file twice = one report
    await c.create_index("entry_id", sparse=True)
    await c.create_index("message_id", sparse=True)
    await c.create_index([("partner_key", 1), ("created_at", -1)])
    _indexed.add(key)


def optimize_pdf(data: bytes) -> bytes:
    """Lossless clean-up and compression (storage watch). Falls back to the original on any problem."""
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            out = doc.tobytes(garbage=3, deflate=True)
        return out if 0 < len(out) < len(data) * 0.95 else data
    except Exception:  # noqa: BLE001
        return data


async def find_by_hash(db: AsyncDatabase, sha256: str, month_keys: list[str]) -> StoredReport | None:
    for mk in dict.fromkeys(month_keys):
        doc = await db[month_collection(mk)].find_one({"file.sha256": sha256}, NO_FILE_DATA)
        if doc:
            return StoredReport(make_ref(mk, doc["_id"]), doc, False)
    return None


async def store_report(
    db: AsyncDatabase,
    store: FileStore,
    *,
    partner_key: str,
    filename: str,
    content_type: str,
    data: bytes,
    parse: ParseResult | None,
    actor_id: str | None,
    source: str,
    month_key: str,
    message_id: str | None = None,
) -> StoredReport:
    coll = month_collection(month_key)
    await ensure_indexes(db, coll)
    sha256 = hashlib.sha256(data).hexdigest()
    existing = await db[coll].find_one({"file.sha256": sha256}, NO_FILE_DATA)
    if existing:
        return StoredReport(make_ref(month_key, existing["_id"]), existing, False)
    kept = optimize_pdf(data) if content_type == "application/pdf" else data
    patient = {k: v for k, v in (parse.fields if parse else {}).items() if v not in (None, "", [])}
    patient["partner_key"] = partner_key
    doc: dict[str, Any] = {
        "partner_key": partner_key,
        "file": await store.pack(
            name=filename, content_type=content_type, data=kept, original=data, month_key=month_key
        ),
        "patient": patient,
        "parse": parse.stored()
        if parse
        else {"source": "manual", "confidence": 0.0, "needs_verify": [], "flags": [], "warnings": []},
        "status": "needs_review" if (parse is None or parse.needs_review) else "stored",
        "entry_id": None,
        "source": source,
        "message_id": message_id,
        "created_at": utcnow(),
        "created_by": actor_id,
    }
    try:
        res = await db[coll].insert_one(doc)
    except Exception as exc:
        persisted = None
        drive_id = doc["file"].get("drive_id")
        keep_drive_file = False
        if drive_id:
            try:
                persisted = await db[coll].find_one({"file.drive_id": drive_id}, NO_FILE_DATA)
            except Exception:
                log.warning("Could not confirm report insert after MongoDB returned an error")
                keep_drive_file = True
        if persisted and persisted["file"].get("drive_id") == drive_id:
            keep_drive_file = True
            persisted_id = persisted.get("_id", doc.get("_id"))
            if persisted_id is not None:
                return StoredReport(make_ref(month_key, persisted_id), persisted, True)
        if not keep_drive_file:
            try:
                await store.cleanup(doc["file"])
            except Exception:
                log.warning("Could not clean up report file after MongoDB insert failure")
        if isinstance(exc, DuplicateKeyError):
            existing = await db[coll].find_one({"file.sha256": sha256}, NO_FILE_DATA)
            if existing:
                return StoredReport(make_ref(month_key, existing["_id"]), existing, False)
        raise
    doc["file"].pop("data", None)
    doc["_id"] = res.inserted_id
    return StoredReport(make_ref(month_key, res.inserted_id), doc, True)


async def get_meta(db: AsyncDatabase, ref: str) -> dict[str, Any] | None:
    parsed = parse_ref(ref)
    return await db[parsed[0]].find_one({"_id": parsed[1]}, NO_FILE_DATA) if parsed else None


async def read_file(db: AsyncDatabase, store: FileStore, ref: str) -> StoredFile | None:
    parsed = parse_ref(ref)
    return await store.read(db, parsed[0], parsed[1]) if parsed else None


async def link_entry(db: AsyncDatabase, ref: str, entry_id: str, partner_key: str) -> str:
    """Attach a report to an entry. Returns linked | already_linked | missing."""
    parsed = parse_ref(ref)
    if not parsed:
        return "missing"
    coll, oid = parsed
    res = await db[coll].update_one(
        {
            "_id": oid,
            "partner_key": partner_key,
            "$or": [{"entry_id": None}, {"entry_id": {"$exists": False}}, {"entry_id": entry_id}],
        },
        {"$set": {"entry_id": entry_id}},
    )
    if res.matched_count:
        return "linked"
    return "already_linked" if await db[coll].find_one({"_id": oid}, {"_id": 1}) else "missing"


async def reports_for_entry(db: AsyncDatabase, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Reports linked to an entry. They live in the registration month or the month the entry was created."""
    months = {entry["registration_date"][:7]}
    if entry.get("created_at"):
        months.add(entry["created_at"].strftime("%Y-%m"))
    out: list[dict[str, Any]] = []
    for mk in sorted(months):
        async for d in db[month_collection(mk)].find({"entry_id": str(entry["_id"])}, NO_FILE_DATA):
            out.append({**d, "ref": make_ref(mk, d["_id"])})
    return out
