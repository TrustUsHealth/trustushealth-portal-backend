"""Entry collection access: indexes, lookups, search."""

from __future__ import annotations

import re
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import PortalConfig
from app.domain.parsing import standardize as std

COL = "entries"


async def ensure_indexes(db: AsyncDatabase) -> None:
    c = db[COL]
    await c.create_index("bill_no", unique=True, sparse=True)  # never reused, never duplicated
    await c.create_index("dedupe_key", unique=True, sparse=True)  # race safety for lab_pid + date (PRD 6.2)
    await c.create_index([("partner_key", 1), ("registration_date", 1)])
    await c.create_index([("registration_date", -1), ("bill_no", -1)])
    await c.create_index("name_norm")
    await c.create_index("lab_pid", sparse=True)
    await c.create_index("customer_id", sparse=True)
    await c.create_index("status")


def to_oid(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


async def get_entry(db: AsyncDatabase, entry_id: str) -> dict[str, Any] | None:
    oid = to_oid(entry_id)
    return await db[COL].find_one({"_id": oid}) if oid else None


async def delete_entry(db: AsyncDatabase, entry_id: str) -> dict[str, Any] | None:
    oid = to_oid(entry_id)
    return await db[COL].find_one_and_delete({"_id": oid}) if oid else None


async def search(
    db: AsyncDatabase,
    cfg: PortalConfig,
    *,
    labs: set[str],
    q: str | None = None,
    partner_key: str | None = None,
    status: str | None = None,
    referred_by: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    bill_no: str | None = None,
    lab_pid: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    allowed = [partner_key] if partner_key and partner_key in labs else [] if partner_key else sorted(labs)
    if not allowed:
        return [], 0
    flt: dict[str, Any] = {"partner_key": {"$in": allowed}}
    ands: list[dict[str, Any]] = []
    if status:
        flt["status"] = status
    if date_from or date_to:
        flt["registration_date"] = {
            **({"$gte": date_from} if date_from else {}),
            **({"$lte": date_to} if date_to else {}),
        }
    if bill_no:
        digits = re.sub(r"\D", "", bill_no)
        flt["bill_no"] = digits.zfill(cfg.app.bill_no.digits) if digits else bill_no
    if lab_pid:
        flt["lab_pid"] = {"$regex": "^" + re.escape(lab_pid.strip()), "$options": "i"}
    if referred_by:
        flt["referred_by"] = {"$regex": re.escape(referred_by.strip()), "$options": "i"}
    if q and q.strip():
        q = q.strip()
        digits = re.sub(r"\D", "", q)
        ors: list[dict[str, Any]] = [{"lab_pid": {"$regex": "^" + re.escape(q), "$options": "i"}}]
        if q.isdigit():
            ors.append({"bill_no": digits.zfill(cfg.app.bill_no.digits)})
        if len(digits) >= 6:
            ors.append({"phone": {"$regex": re.escape(digits[-10:]) + "$"}})
        tokens = std.normalize_name(q).split()
        if tokens:
            ors.append({"$and": [{"name_norm": {"$regex": r"(^|\s)" + re.escape(t)}} for t in tokens]})
        ands.append({"$or": ors})
    if ands:
        flt["$and"] = ands

    total = await db[COL].count_documents(flt)
    cursor = (
        db[COL]
        .find(flt)
        .sort([("registration_date", -1), ("bill_no", -1)])
        .skip(max(page - 1, 0) * page_size)
        .limit(page_size)
    )
    return await cursor.to_list(length=page_size), total