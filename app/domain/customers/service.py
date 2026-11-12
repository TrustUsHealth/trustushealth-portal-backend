"""Patient identity across visits. Entries link to a customer; the link is optional."""

from __future__ import annotations

import re
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.asynchronous.database import AsyncDatabase
from rapidfuzz import fuzz

from app.core.config import PortalConfig
from app.core.db import utcnow
from app.domain.parsing import standardize as std

COL = "customers"


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db[COL].create_index("name_norm")
    await db[COL].create_index("phone", sparse=True)


def _is_same(cfg: PortalConfig, c: dict[str, Any], e: dict[str, Any], n_norm: str) -> bool:
    tol, thr = cfg.dedupe.age_tolerance_years, cfg.dedupe.name_similarity_threshold
    if e.get("phone") and c.get("phone") == e["phone"]:
        if fuzz.token_sort_ratio(n_norm, c.get("name_norm") or "") / 100 >= thr:
            return True
    if c.get("name_norm") != n_norm:
        return False
    if c.get("gender") and e.get("gender") and c["gender"] != e["gender"]:
        return False
    a, b = std.age_years(c.get("age"), c.get("age_unit")), std.age_years(e.get("age"), e.get("age_unit"))
    return a is None or b is None or abs(a - b) <= tol


async def link_entry_customer(db: AsyncDatabase, cfg: PortalConfig, entry: dict[str, Any]) -> str | None:
    """Find or create the customer for an entry; fills empty customer details. Returns the customer id."""
    name = entry.get("patient_name")
    if not name:
        return None
    n_norm = entry.get("name_norm") or std.normalize_name(name)
    cands: dict[Any, dict[str, Any]] = {}
    if entry.get("phone"):
        for c in await db[COL].find({"phone": entry["phone"]}).to_list(length=20):
            cands[c["_id"]] = c
    for c in await db[COL].find({"name_norm": n_norm}).to_list(length=50):
        cands[c["_id"]] = c
    now = utcnow()
    for c in cands.values():
        if _is_same(cfg, c, entry, n_norm):
            fill = {k: entry[k] for k in ("phone", "email", "gender", "age", "age_unit") if entry.get(k) and not c.get(k)}
            if fill:
                await db[COL].update_one({"_id": c["_id"]}, {"$set": {**fill, "updated_at": now}})
            return str(c["_id"])
    doc = {
        "name": name,
        "name_norm": n_norm,
        **{k: entry[k] for k in ("age", "age_unit", "gender", "phone", "email") if entry.get(k) is not None},
        "created_at": now,
        "updated_at": now,
    }
    return str((await db[COL].insert_one(doc)).inserted_id)


async def get_customer(db: AsyncDatabase, customer_id: str) -> dict[str, Any] | None:
    try:
        return await db[COL].find_one({"_id": ObjectId(customer_id)})
    except (InvalidId, TypeError):
        return None


async def list_customers(
    db: AsyncDatabase, *, q: str | None, page: int, page_size: int
) -> tuple[list[dict[str, Any]], int]:
    flt: dict[str, Any] = {}
    if q and q.strip():
        ors: list[dict[str, Any]] = []
        tokens = std.normalize_name(q).split()
        if tokens:
            ors.append({"$and": [{"name_norm": {"$regex": r"(^|\s)" + re.escape(t)}} for t in tokens]})
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 6:
            ors.append({"phone": {"$regex": re.escape(digits[-10:]) + "$"}})
        flt = {"$or": ors} if ors else {"_id": None}
    total = await db[COL].count_documents(flt)
    cursor = db[COL].find(flt).sort("name_norm", 1).skip(max(page - 1, 0) * page_size).limit(page_size)
    return await cursor.to_list(length=page_size), total