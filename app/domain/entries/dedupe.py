"""Duplicate rules. Every check includes lab + registration date. Thresholds come from dedupe.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase
from rapidfuzz import fuzz

from app.core.config import PortalConfig
from app.domain.entries.repository import COL
from app.domain.parsing import standardize as std


@dataclass
class Matches:
    exact: dict[str, Any] | None = None
    rule: int | None = None  # 1 = same lab PID, 2 = same name + gender + age
    possible: list[dict[str, Any]] = field(default_factory=list)


async def find_matches(
    db: AsyncDatabase, cfg: PortalConfig, inc: dict[str, Any], *, exclude_id: Any = None
) -> Matches:
    partner, day = inc.get("partner_key"), inc.get("registration_date")
    name = inc.get("patient_name")
    if not partner or not day or not name:
        return Matches()
    flt: dict[str, Any] = {"partner_key": partner, "registration_date": day}
    if exclude_id is not None:
        flt["_id"] = {"$ne": exclude_id}
    cands = await db[COL].find(flt).to_list(length=500)

    n_norm = std.normalize_name(name)
    pid = (inc.get("lab_pid") or "").casefold()
    gender = inc.get("gender")
    age = std.age_years(inc.get("age"), inc.get("age_unit") or ("Y" if inc.get("age") is not None else None))
    tol, thr = cfg.dedupe.age_tolerance_years, cfg.dedupe.name_similarity_threshold
    phone = inc.get("phone")

    for c in cands:  # rule 1
        if pid and (c.get("lab_pid") or "").casefold() == pid:
            return Matches(exact=c, rule=1, possible=[])
    for c in cands:  # rule 2
        c_age = std.age_years(c.get("age"), c.get("age_unit"))
        if (
            c.get("name_norm") == n_norm
            and gender
            and c.get("gender") == gender
            and age is not None
            and c_age is not None
            and abs(age - c_age) <= tol
        ):
            return Matches(exact=c, rule=2, possible=[])

    scored: list[tuple[float, dict[str, Any]]] = []
    for c in cands:  # rule 3
        sim = fuzz.token_sort_ratio(n_norm, c.get("name_norm") or "") / 100
        if sim >= thr or (phone and c.get("phone") == phone):
            scored.append((sim, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    return Matches(possible=[c for _, c in scored[:5]])