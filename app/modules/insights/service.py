"""Business Insights data access and caching. Reads only small `entries` fields (never files, never patient names,
phone or IDs) and keeps results for 5 minutes, cleared whenever an entry is saved or deleted."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.modules.insights import periods as P
from app.modules.insights.compute import Analysis, Dataset, LabInfo, parse_day, parse_visit

if TYPE_CHECKING:
    from app.core.config import PortalConfig

ENTRIES = "entries"
CACHE_SECONDS = 300
CACHE_MAX = 64
ID_CHUNK = 2000
PROJECTION = {
    "bill_no": 1, "registration_date": 1, "partner_key": 1, "status": 1, "customer_id": 1, "tests": 1,
    "referred_by": 1, "age": 1, "age_unit": 1, "gender": 1, "gross_amount": 1, "discount_amount": 1,
    "net_amount": 1, "lab_cost": 1, "collection_time": 1, "reporting_time": 1,
}  # an allow-list: nothing that identifies a patient is ever loaded


class InsightsError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


class ResponseCache:
    def __init__(self, ttl: int = CACHE_SECONDS, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._items: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        hit = self._items.get(key)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        self._items.pop(key, None)
        return None

    def set(self, key: Any, value: Any) -> None:
        if len(self._items) >= CACHE_MAX:
            self._items.pop(min(self._items, key=lambda k: self._items[k][0]))
        self._items[key] = (self.clock(), value)

    def clear(self) -> None:
        self._items.clear()


CACHE = ResponseCache()


def today_local(cfg: PortalConfig) -> date:
    return datetime.now(ZoneInfo(cfg.app.timezone)).date()


async def ensure_indexes(db) -> None:
    await db[ENTRIES].create_index([("registration_date", 1), ("partner_key", 1), ("status", 1)])
    await db[ENTRIES].create_index([("customer_id", 1), ("registration_date", 1)])


async def on_startup(app) -> None:
    CACHE.clear()
    await ensure_indexes(app.state.db)


# ───────────────────────────── filters ─────────────────────────────
def lab_infos(cfg: PortalConfig) -> dict[str, LabInfo]:
    """Labs come from the partner YAML files; a lab keeps its colour by position (or its pinned `chart_color`)."""
    palette = cfg.insights.chart_palette.labs
    return {
        key: LabInfo(key, p.name, p.chart_color or palette[i % len(palette)])
        for i, (key, p) in enumerate(cfg.partners.items())
    }


@dataclass(frozen=True)
class Params:
    preset: str
    range: P.DateRange
    compare: P.DateRange | None
    compare_mode: str
    labs: tuple[str, ...]

    @property
    def grain(self) -> str:
        return P.grain_for(self.range)


def resolve_params(
    cfg: PortalConfig, *, period: str | None, start: date | None, end: date | None, labs: str | None,
    compare: str | None, today: date | None = None,
) -> Params:
    today = today or today_local(cfg)
    mode = compare or cfg.insights.compare_default
    if mode not in P.COMPARES:
        raise InsightsError(422, f"Compare must be one of: {', '.join(P.COMPARES)}.")
    preset = "custom" if (start or end) else (period or cfg.insights.default_period)
    if preset not in P.PRESETS:
        raise InsightsError(422, f"Period must be one of: {', '.join(P.PRESETS)}.")
    try:
        res = P.resolve_period(preset, today, cfg.app.fiscal_year_start_month, start, end)
        cmp_range = P.compare_range(res, mode)
    except P.PeriodError as exc:
        raise InsightsError(422, str(exc)) from None
    keys = tuple(sorted({k.strip() for k in (labs or "").split(",") if k.strip()}))
    unknown = [k for k in keys if k not in cfg.partners]
    if unknown:
        raise InsightsError(422, f"Unknown lab: {', '.join(unknown)}.")
    return Params(preset, res.range, cmp_range, mode, keys)


# ───────────────────────────── data ─────────────────────────────
async def _dataset(db, rng: P.DateRange, labs: tuple[str, ...], tz) -> Dataset:
    query: dict[str, Any] = {"registration_date": {"$gte": rng.start.isoformat(), "$lte": rng.end.isoformat()}}
    if labs:
        query["partner_key"] = {"$in": list(labs)}
    docs = await db[ENTRIES].find(query, PROJECTION).to_list(length=None)
    visits = [v for d in docs if (v := parse_visit(d, tz)) is not None]
    raw = {str(d["customer_id"]): d["customer_id"] for d in docs if d.get("customer_id")}
    first_before: dict[str, date] = {}
    ids = list(raw.values())
    for i in range(0, len(ids), ID_CHUNK):
        prior = await db[ENTRIES].find(
            {"customer_id": {"$in": ids[i : i + ID_CHUNK]}, "registration_date": {"$lt": rng.start.isoformat()}},
            {"customer_id": 1, "registration_date": 1},
        ).to_list(length=None)
        for d in prior:
            day, key = parse_day(d.get("registration_date")), str(d["customer_id"])
            if day and (key not in first_before or day < first_before[key]):
                first_before[key] = day
    return Dataset(visits, first_before)


async def _earliest(db) -> date | None:
    docs = await db[ENTRIES].find({}, {"registration_date": 1}).sort("registration_date", 1).limit(1).to_list(length=1)
    return parse_day(docs[0].get("registration_date")) if docs else None


async def analysis(db, cfg: PortalConfig, params: Params, *, refresh: bool = False) -> Analysis:
    key = (id(db), params)
    if not refresh and (hit := CACHE.get(key)) is not None:
        return hit
    tz = ZoneInfo(cfg.app.timezone)
    today = today_local(cfg)
    main = await _dataset(db, params.range, params.labs, tz)
    cmp_ds = await _dataset(db, params.compare, params.labs, tz) if params.compare else None
    result = Analysis(
        cfg=cfg.insights, range=params.range, compare=params.compare, compare_mode=params.compare_mode,
        preset=params.preset, grain=params.grain, labs_selected=list(params.labs), labs=lab_infos(cfg),
        others_color=cfg.insights.chart_palette.others, main=main, cmp=cmp_ds, earliest=await _earliest(db),
        today=today, generated_at=datetime.now(tz).isoformat(timespec="seconds"),
    )
    CACHE.set(key, result)
    return result


async def earliest_date(db) -> date | None:
    return await _earliest(db)


def previous_month_range(today: date) -> P.DateRange:
    end = today.replace(day=1) - timedelta(days=1)
    return P.DateRange(end.replace(day=1), end)