"""Business Insights calculations. Pure Python over small in-memory visit records, so the same numbers
feed the screens and the PDF. Money is `Decimal` throughout and travels as strings; nothing here touches the database.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from app.modules.insights import periods as P
from app.modules.insights.fmt import hours_text, inr

ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_TURNAROUND_HOURS = 14 * 24
NOT_RECORDED = "Not recorded"
BUILDING_DAYS = 183  # about 6 months of history before repeat figures are trusted
BELOW_COST_LIST_LIMIT = 500

DEFAULT_TEMPLATES = {
    "revenue_change": "Billed Revenue was {value}, {direction} {pct}% compared with {compare_label}.",
    "top_lab": "{lab} brought in {share}% of Billed Revenue.",
    "margin_change": "Margin was {value}, {direction} {pct}% compared with {compare_label}.",
    "discount_alert": "Discount Rate is {rate}%, above the {threshold}% limit.",
    "below_cost": "{count} {bills} {were} below cost. Review them in Finance.",
    "slowest_lab": "{lab} has the slowest typical report turnaround ({median}).",
    "repeat_rate": "{pct}% of patients were returning patients.",
    "low_margin": "{labs} earned a margin below {threshold}%.",
}


# ───────────────────────────── records ─────────────────────────────
def to_dec(v: Any) -> Decimal | None:
    """Read a stored money value whatever its stored form (Decimal128, Decimal, number or text)."""
    if v is None or v == "":
        return None
    if hasattr(v, "to_decimal"):  # bson Decimal128
        v = v.to_decimal()
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation:
        return None


def money_str(d: Decimal) -> str:
    return format(d.quantize(CENT), "f")


@dataclass(slots=True)
class Visit:
    id: str
    bill_no: str | None
    day: date
    lab: str
    billed: bool
    customer: str | None
    tests: tuple[str, ...]
    referrer: str | None
    age_years: int | None
    gender: str | None
    gross: Decimal
    discount: Decimal
    net: Decimal
    lab_cost: Decimal | None
    collected: datetime | None
    reported: datetime | None

    @property
    def margin(self) -> Decimal | None:
        return None if self.lab_cost is None else self.net - self.lab_cost


def _dt(v: Any, tz: tzinfo) -> datetime | None:
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=tz) if d.tzinfo is None else d


def parse_day(v: Any) -> date | None:
    try:
        return v.date() if isinstance(v, datetime) else date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def parse_visit(doc: dict[str, Any], tz: tzinfo) -> Visit | None:
    day = parse_day(doc.get("registration_date"))
    if day is None:
        return None
    unit = doc.get("age_unit") or "Y"
    age = doc.get("age")
    age_years = None if age is None else (int(age) if unit == "Y" else 0)
    net = to_dec(doc.get("net_amount"))
    gross = to_dec(doc.get("gross_amount"))
    discount = to_dec(doc.get("discount_amount")) or ZERO
    if net is None:
        net = (gross - discount) if gross is not None else ZERO
    if gross is None:
        gross = net + discount
    cust = doc.get("customer_id")
    return Visit(
        id=str(doc.get("_id")),
        bill_no=doc.get("bill_no"),
        day=day,
        lab=doc.get("partner_key") or "unknown",
        billed=doc.get("status") == "complete",
        customer=str(cust) if cust else None,
        tests=tuple(t for t in (doc.get("tests") or []) if isinstance(t, str)),
        referrer=(doc.get("referred_by") or "").strip() or None,
        age_years=age_years,
        gender=doc.get("gender"),
        gross=gross,
        discount=discount,
        net=net,
        lab_cost=to_dec(doc.get("lab_cost")),
        collected=_dt(doc.get("collection_time"), tz) if doc.get("collection_time") else None,
        reported=_dt(doc.get("reporting_time"), tz) if doc.get("reporting_time") else None,
    )


@dataclass
class Dataset:
    visits: list[Visit]
    first_before: dict[str, date]  # customer -> earliest visit before the range began (any lab)


@dataclass(frozen=True)
class LabInfo:
    key: str
    name: str
    color: str


@dataclass
class Analysis:
    cfg: Any  # InsightsCfg
    range: P.DateRange
    compare: P.DateRange | None
    compare_mode: str
    preset: str
    grain: str
    labs_selected: list[str]
    labs: dict[str, LabInfo]
    others_color: str
    main: Dataset
    cmp: Dataset | None
    earliest: date | None
    today: date
    generated_at: str

    def lab(self, key: str) -> LabInfo:
        return self.labs.get(key) or LabInfo(key, key, self.others_color)


# ───────────────────────────── summaries ─────────────────────────────
def summarize(visits: list[Visit]) -> dict[str, Any]:
    billed = [v for v in visits if v.billed]
    costed = [v for v in billed if v.lab_cost is not None]
    revenue = sum((v.net for v in billed), ZERO)
    gross = sum((v.gross for v in billed), ZERO)
    discounts = sum((v.discount for v in billed), ZERO)
    lab_cost = sum((v.lab_cost for v in costed), ZERO)
    revenue_costed = sum((v.net for v in costed), ZERO)
    margin = revenue_costed - lab_cost
    return {
        "visits": len(visits),
        "billed_visits": len(billed),
        "awaiting": len(visits) - len(billed),
        "revenue": revenue,
        "gross": gross,
        "discounts": discounts,
        "lab_cost": lab_cost,
        "revenue_costed": revenue_costed,
        "revenue_no_cost": revenue - revenue_costed,
        "margin": margin,
        "costed_visits": len(costed),
        "missing_cost": len(billed) - len(costed),
        "below_cost": sum(1 for v in costed if v.margin < 0),
        "margin_pct": float(margin / revenue_costed * 100) if revenue_costed else None,
        "discount_rate": float(discounts / gross * 100) if gross else None,
        "avg_bill": (revenue / len(billed)) if billed else None,
    }


def patient_summary(ds: Dataset) -> dict[str, Any]:
    linked = {v.customer for v in ds.visits if v.customer}
    returning = {c for c in linked if c in ds.first_before}
    n = len(linked)
    return {
        "patients": n,
        "returning": len(returning),
        "new": n - len(returning),
        "unlinked": sum(1 for v in ds.visits if not v.customer),
        "repeat_rate": (len(returning) / n * 100) if n else None,
    }


def _round1(x: float) -> float:
    return round(x + 0.0, 1)


def kpi(
    key: str, label: str, value: Any, compare: Any, kind: str, spark: list[float] | None = None, detail: str | None = None
) -> dict[str, Any]:
    """kind: money | count | percent. Percent KPIs change in percentage points, the others in percent."""
    out: dict[str, Any] = {"key": key, "label": label, "kind": kind, "detail": detail, "sparkline": spark or []}
    out["value"] = money_str(value) if kind == "money" else (value if kind == "count" or value is None else _round1(value))
    out["compare_value"] = None
    out["delta_pct"] = None
    out["direction"] = None
    if compare is None and kind != "percent":
        return out
    if kind == "percent":
        if compare is None or value is None:
            return out
        out["compare_value"], delta = _round1(compare), value - compare
        out["delta_pct"] = _round1(delta)
        out["direction"] = "flat" if abs(delta) < 0.05 else ("up" if delta > 0 else "down")
        return out
    out["compare_value"] = money_str(compare) if kind == "money" else compare
    if compare == 0:
        out["direction"] = "flat" if value == 0 else "new"
        return out
    delta = float((Decimal(value) - Decimal(compare)) / Decimal(compare) * 100)
    out["delta_pct"] = _round1(delta)
    out["direction"] = "flat" if abs(delta) < 0.05 else ("up" if delta > 0 else "down")
    return out


# ───────────────────────────── trends ─────────────────────────────
def trend(visits: list[Visit], first_before: dict[str, date], rng: P.DateRange, grain: str) -> list[dict[str, Any]]:
    bks = P.buckets(rng, grain)
    by: dict[str, list[Visit]] = defaultdict(list)
    for v in visits:
        by[P.bucket_key(v.day, grain)].append(v)
    first_in = {}
    for v in visits:
        if v.customer and (v.customer not in first_in or v.day < first_in[v.customer]):
            first_in[v.customer] = v.day
    points = []
    for b in bks:
        vs = by.get(b.key, [])
        s = summarize(vs)
        custs = {v.customer for v in vs if v.customer}
        new = {c for c in custs if c not in first_before and P.bucket_key(first_in[c], grain) == b.key}
        points.append(
            {
                "key": b.key,
                "label": b.label,
                "visits": s["visits"],
                "billed_visits": s["billed_visits"],
                "billed_revenue": money_str(s["revenue"]),
                "margin": money_str(s["margin"]),
                "lab_cost": money_str(s["lab_cost"]),
                "revenue_no_cost": money_str(s["revenue_no_cost"]),
                "gross_billed": money_str(s["gross"]),
                "discounts": money_str(s["discounts"]),
                "discount_rate_pct": _round1(s["discount_rate"]) if s["discount_rate"] is not None else None,
                "patients": len(custs),
                "new": len(new),
                "returning": len(custs) - len(new),
            }
        )
    return points


def _spark(points: list[dict[str, Any]], key: str) -> list[float]:
    return [float(p[key]) for p in points]


# ───────────────────────────── turnaround ─────────────────────────────
def percentile(values: list[float], pct: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * pct / 100
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def turnaround_hours(visits: list[Visit]) -> list[float]:
    """Hours from collection to report; ignores missing times, reports before collection or later than 14 days."""
    out = []
    for v in visits:
        if v.collected and v.reported:
            h = (v.reported - v.collected).total_seconds() / 3600
            if 0 <= h <= MAX_TURNAROUND_HOURS:
                out.append(h)
    return out


def _turn(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median_hours": None, "slowest_10_hours": None, "median_display": hours_text(None),
                "slowest_10_display": hours_text(None), "sample": 0}
    med, p90 = statistics.median(values), percentile(values, 90)
    return {"median_hours": round(med, 1), "slowest_10_hours": round(p90, 1), "median_display": hours_text(med),
            "slowest_10_display": hours_text(p90), "sample": len(values)}


# ───────────────────────────── envelope ─────────────────────────────
def _range_out(r: P.DateRange | None) -> dict[str, str] | None:
    return None if r is None else {"start": r.start.isoformat(), "end": r.end.isoformat(), "label": r.label()}


def _margin_note(s: dict[str, Any]) -> list[str]:
    if s["billed_visits"] and s["missing_cost"]:
        return [
            f"Margin is based on {s['costed_visits']} of {s['billed_visits']} billed visits "
            f"({s['missing_cost']} have no lab cost)."
        ]
    return []


def _envelope(a: Analysis, notes: list[str], empty: bool, empty_message: str) -> dict[str, Any]:
    if a.compare and a.earliest and a.compare.end < a.earliest:
        notes = [*notes, f"There is no data before {P.fmt_day(a.earliest)}, so the comparison is empty."]
    return {
        "range": _range_out(a.range),
        "compare_range": _range_out(a.compare),
        "grain": a.grain,
        "generated_at": a.generated_at,
        "data_notes": notes,
        "empty": empty,
        "empty_message": empty_message if empty else None,
        "filters": {"period": a.preset, "labs": a.labs_selected, "compare": a.compare_mode},
    }


NO_BILLED = "No billed visits in this period"
NO_VISITS = "No visits in this period"


# ───────────────────────────── per-lab helpers ─────────────────────────────
def _by_lab(visits: list[Visit]) -> dict[str, list[Visit]]:
    out: dict[str, list[Visit]] = defaultdict(list)
    for v in visits:
        out[v.lab].append(v)
    return out


def _lab_rows(a: Analysis) -> list[dict[str, Any]]:
    rows = []
    for key, vs in _by_lab(a.main.visits).items():
        s = summarize(vs)
        info = a.lab(key)
        rows.append({"lab": key, "name": info.name, "color": info.color, "summary": s, "visits": vs})
    return sorted(rows, key=lambda r: (-r["summary"]["revenue"], -r["summary"]["visits"], r["name"]))


def _repeat_building(a: Analysis) -> bool:
    return a.earliest is None or (a.today - a.earliest).days < BUILDING_DAYS


def _patient_notes(a: Analysis, ps: dict[str, Any]) -> list[str]:
    notes = []
    if _repeat_building(a):
        notes.append("Repeat figures are still building. Patients seen before go-live look new.")
    if ps["unlinked"]:
        notes.append(f"{ps['unlinked']} visits are not linked to a patient and are left out of patient figures.")
    return notes


# ───────────────────────────── highlights ─────────────────────────────
def highlights(a: Analysis) -> list[str]:
    cfg = a.cfg
    s = summarize(a.main.visits)
    c = summarize(a.cmp.visits) if a.cmp else None
    ps = patient_summary(a.main)
    tpl = {**DEFAULT_TEMPLATES, **cfg.highlights.templates}
    label = P.COMPARE_LABELS[a.compare_mode]
    rows = [r for r in _lab_rows(a) if r["summary"]["revenue"] > 0]
    found: dict[str, str] = {}

    def change(key: str, now: Decimal, before: Decimal | None, name: str) -> None:
        if before is None or before == 0 or now == 0 and before == 0:
            return
        pct = float((now - before) / before * 100)
        if abs(pct) < 0.05:
            found[key] = f"{name} was {inr(now)}, unchanged compared with {label}."
            return
        found[key] = tpl[key].format(
            value=inr(now), direction="up" if pct > 0 else "down", pct=f"{abs(pct):.1f}", compare_label=label
        )

    if s["billed_visits"]:
        change("revenue_change", s["revenue"], c["revenue"] if c else None, "Billed Revenue")
        if s["costed_visits"]:
            change("margin_change", s["margin"], c["margin"] if c and c["costed_visits"] else None, "Margin")
    if len(rows) >= 2 and s["revenue"] > 0:
        top = rows[0]
        found["top_lab"] = tpl["top_lab"].format(lab=top["name"], share=f"{top['summary']['revenue'] / s['revenue'] * 100:.0f}")
    thr = cfg.thresholds
    if s["discount_rate"] is not None and s["discount_rate"] > thr.high_discount_pct:
        found["discount_alert"] = tpl["discount_alert"].format(
            rate=f"{s['discount_rate']:.1f}", threshold=f"{thr.high_discount_pct:g}"
        )
    if s["below_cost"]:
        n = s["below_cost"]
        found["below_cost"] = tpl["below_cost"].format(
            count=n, bills="bill" if n == 1 else "bills", were="was" if n == 1 else "were"
        )
    turn = [(r, turnaround_hours(r["visits"])) for r in _lab_rows(a)]
    turn = [(r, statistics.median(h)) for r, h in turn if h]
    if len(turn) >= 2:
        r, med = max(turn, key=lambda t: t[1])
        found["slowest_lab"] = tpl["slowest_lab"].format(lab=r["name"], median=hours_text(med))
    if ps["repeat_rate"] is not None and not _repeat_building(a):
        found["repeat_rate"] = tpl["repeat_rate"].format(pct=f"{ps['repeat_rate']:.0f}")
    low = [r["name"] for r in rows if r["summary"]["margin_pct"] is not None and r["summary"]["margin_pct"] < thr.low_margin_pct]
    if low:
        found["low_margin"] = tpl["low_margin"].format(labs=", ".join(low), threshold=f"{thr.low_margin_pct:g}")
    order = [k for k in cfg.highlights.order if k in found]
    return [found[k] for k in order][: cfg.highlights.max_items]


# ───────────────────────────── overview ─────────────────────────────
def build_overview(a: Analysis) -> dict[str, Any]:
    s, ps = summarize(a.main.visits), patient_summary(a.main)
    c = summarize(a.cmp.visits) if a.cmp else None
    cps = patient_summary(a.cmp) if a.cmp else None
    pts = trend(a.main.visits, a.main.first_before, a.range, a.grain)
    cpts = trend(a.cmp.visits, a.cmp.first_before, a.compare, a.grain) if a.cmp else []
    for i, p in enumerate(pts):
        p["compare_billed_revenue"] = cpts[i]["billed_revenue"] if i < len(cpts) else None
    cmp_ = lambda key: c[key] if c else None  # noqa: E731
    kpis = [
        kpi("billed_revenue", "Billed Revenue", s["revenue"], cmp_("revenue"), "money", _spark(pts, "billed_revenue")),
        kpi("margin", "Margin", s["margin"], cmp_("margin"), "money", _spark(pts, "margin"),
            detail=f"Margin % {s['margin_pct']:.1f}" if s["margin_pct"] is not None else "Margin % n/a"),
        kpi("visits", "Visits", s["visits"], cmp_("visits"), "count", _spark(pts, "visits")),
        kpi("patients", "Patients", ps["patients"], cps["patients"] if cps else None, "count", _spark(pts, "patients"),
            detail=f"New {ps['new']} / Returning {ps['returning']}"),
    ]
    total = s["revenue"]
    by_lab = [
        {"lab": r["lab"], "name": r["name"], "color": r["color"], "billed_revenue": money_str(r["summary"]["revenue"]),
         "share_pct": _round1(float(r["summary"]["revenue"] / total * 100)) if total else 0.0,
         "visits": r["summary"]["visits"]}
        for r in _lab_rows(a) if r["summary"]["revenue"] > 0 or r["summary"]["visits"] > 0
    ]
    attention = [
        {"key": "awaiting_billing", "label": "Awaiting Billing", "count": s["awaiting"],
         "link": "/patient_registry?status=draft"},
        {"key": "below_cost", "label": "Below-Cost Bills", "count": s["below_cost"], "link": "/insights/finance#below-cost"},
        {"key": "missing_lab_cost", "label": "Billed Visits missing Lab Cost", "count": s["missing_cost"],
         "link": "/patient_registry?status=complete"},
    ]
    out = _envelope(a, _margin_note(s), s["billed_visits"] == 0, NO_BILLED)
    out.update({"kpis": kpis, "highlights": highlights(a), "trend": pts, "revenue_by_lab": by_lab, "attention": attention})
    return out


# ───────────────────────────── finance ─────────────────────────────
def build_finance(a: Analysis) -> dict[str, Any]:
    s = summarize(a.main.visits)
    c = summarize(a.cmp.visits) if a.cmp else None
    pts = trend(a.main.visits, a.main.first_before, a.range, a.grain)
    g = lambda key: c[key] if c else None  # noqa: E731
    kpis = [
        kpi("billed_revenue", "Billed Revenue", s["revenue"], g("revenue"), "money", _spark(pts, "billed_revenue")),
        kpi("lab_cost", "Lab Cost", s["lab_cost"], g("lab_cost"), "money", _spark(pts, "lab_cost")),
        kpi("margin", "Margin", s["margin"], g("margin"), "money", _spark(pts, "margin"),
            detail=f"Margin % {s['margin_pct']:.1f}" if s["margin_pct"] is not None else "Margin % n/a"),
        kpi("discounts", "Discounts Given", s["discounts"], g("discounts"), "money", _spark(pts, "discounts"),
            detail=f"Discount Rate {s['discount_rate']:.1f}%" if s["discount_rate"] is not None else "Discount Rate n/a"),
    ]
    thr = a.cfg.thresholds.low_margin_pct
    rows, margin_by_lab = [], []
    for r in _lab_rows(a):
        ls = r["summary"]
        if not ls["billed_visits"]:
            continue
        rows.append(
            {"lab": r["lab"], "name": r["name"], "color": r["color"], "billed_visits": ls["billed_visits"],
             "billed_revenue": money_str(ls["revenue"]), "lab_cost": money_str(ls["lab_cost"]),
             "margin": money_str(ls["margin"]),
             "margin_pct": _round1(ls["margin_pct"]) if ls["margin_pct"] is not None else None,
             "discount_rate_pct": _round1(ls["discount_rate"]) if ls["discount_rate"] is not None else None}
        )
        if ls["margin_pct"] is not None:
            margin_by_lab.append(
                {"lab": r["lab"], "name": r["name"], "color": r["color"], "margin_pct": _round1(ls["margin_pct"]),
                 "below_threshold": ls["margin_pct"] < thr}
            )
    totals = {
        "billed_visits": s["billed_visits"], "billed_revenue": money_str(s["revenue"]),
        "lab_cost": money_str(s["lab_cost"]), "margin": money_str(s["margin"]),
        "margin_pct": _round1(s["margin_pct"]) if s["margin_pct"] is not None else None,
        "discount_rate_pct": _round1(s["discount_rate"]) if s["discount_rate"] is not None else None,
    }
    out = _envelope(a, _margin_note(s), s["billed_visits"] == 0, NO_BILLED)
    out.update(
        {
            "kpis": kpis,
            "money_split": [
                {"key": p["key"], "label": p["label"], "lab_cost": p["lab_cost"], "margin": p["margin"],
                 "revenue_no_cost": p["revenue_no_cost"], "billed_revenue": p["billed_revenue"]} for p in pts
            ],
            "margin_by_lab": margin_by_lab,
            "low_margin_threshold_pct": thr,
            "discounts": [{"key": p["key"], "label": p["label"], "discounts": p["discounts"],
                           "discount_rate_pct": p["discount_rate_pct"]} for p in pts],
            "lab_table": {"rows": rows, "totals": totals},
            "stats": {
                "avg_bill_value": money_str(s["avg_bill"]) if s["avg_bill"] is not None else None,
                "awaiting_billing": s["awaiting"],
                "below_cost_bills": s["below_cost"],
            },
        }
    )
    return out


def build_below_cost(a: Analysis) -> dict[str, Any]:
    bad = sorted((v for v in a.main.visits if v.billed and v.margin is not None and v.margin < 0), key=lambda v: (v.margin, v.day))
    items = [
        {"bill_no": v.bill_no, "date": v.day.isoformat(), "lab": v.lab, "lab_name": a.lab(v.lab).name,
         "billed": money_str(v.net), "lab_cost": money_str(v.lab_cost), "margin": money_str(v.margin)}
        for v in bad[:BELOW_COST_LIST_LIMIT]
    ]
    out = _envelope(a, [], not bad, "No below-cost bills in this period")
    out.update({"total": len(bad), "truncated": len(bad) > BELOW_COST_LIST_LIMIT, "items": items})
    return out


# ───────────────────────────── labs ─────────────────────────────
def build_labs(a: Analysis) -> dict[str, Any]:
    s = summarize(a.main.visits)
    rows = [r for r in _lab_rows(a) if r["summary"]["visits"] > 0]
    cards, turn_rows = [], []
    for r in rows:
        t = _turn(turnaround_hours(r["visits"]))
        ls = r["summary"]
        cards.append(
            {"lab": r["lab"], "name": r["name"], "color": r["color"], "visits": ls["visits"],
             "billed_revenue": money_str(ls["revenue"]),
             "margin_pct": _round1(ls["margin_pct"]) if ls["margin_pct"] is not None else None,
             "median_turnaround_hours": t["median_hours"], "median_turnaround_display": t["median_display"]}
        )
        turn_rows.append({"lab": r["lab"], "name": r["name"], "color": r["color"], **t})
    bks = P.buckets(a.range, a.grain)
    counts: dict[str, Counter] = defaultdict(Counter)
    for v in a.main.visits:
        counts[P.bucket_key(v.day, a.grain)][v.lab] += 1
    visits_by_lab = {
        "labs": [{"lab": r["lab"], "name": r["name"], "color": r["color"]} for r in rows],
        "points": [{"key": b.key, "label": b.label, "counts": {r["lab"]: counts[b.key][r["lab"]] for r in rows}}
                   for b in bks],
    }
    months = P.months_in(a.range)
    cost: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for v in a.main.visits:
        if v.billed and v.lab_cost is not None:
            cost[(v.lab, P.bucket_key(v.day, "month"))] += v.lab_cost
    stmt_rows = []
    for r in rows:
        cells = {m.key: money_str(cost[(r["lab"], m.key)]) for m in months}
        stmt_rows.append({"lab": r["lab"], "name": r["name"], "cells": cells,
                          "total": money_str(sum((cost[(r["lab"], m.key)] for m in months), ZERO))})
    stmt_totals = {m.key: money_str(sum((cost[(r["lab"], m.key)] for r in rows), ZERO)) for m in months}
    usable = turnaround_hours(a.main.visits)
    notes = _margin_note(s)
    if a.main.visits and len(usable) < len(a.main.visits):
        notes.append(f"Turnaround uses {len(usable)} of {len(a.main.visits)} visits (others lack both times or look implausible).")
    out = _envelope(a, notes, s["visits"] == 0, NO_VISITS)
    out.update(
        {
            "lab_cards": cards,
            "visits_by_lab": visits_by_lab,
            "turnaround": turn_rows,
            "lab_cost_statement": {
                "months": [{"key": m.key, "label": m.label} for m in months],
                "rows": stmt_rows,
                "totals": stmt_totals,
                "grand_total": money_str(sum((Decimal(x) for x in stmt_totals.values()), ZERO)),
            },
        }
    )
    return out


# ───────────────────────────── patients ─────────────────────────────
def _band(age: int | None, bands: list[Any]) -> str:
    if age is None:
        return NOT_RECORDED
    for b in bands:
        if age <= b.max:
            return b.label
    return bands[-1].label


def build_patients(a: Analysis) -> dict[str, Any]:
    cfg = a.cfg
    ps = patient_summary(a.main)
    cps = patient_summary(a.cmp) if a.cmp else None
    pts = trend(a.main.visits, a.main.first_before, a.range, a.grain)
    cp = lambda key: cps[key] if cps else None  # noqa: E731
    kpis = [
        kpi("patients", "Patients", ps["patients"], cp("patients"), "count", _spark(pts, "patients")),
        kpi("new_patients", "New", ps["new"], cp("new"), "count", _spark(pts, "new")),
        kpi("returning_patients", "Returning", ps["returning"], cp("returning"), "count", _spark(pts, "returning")),
        kpi("repeat_rate", "Repeat Rate", ps["repeat_rate"], cp("repeat_rate"), "percent"),
    ]
    labels = [b.label for b in cfg.age_bands]
    grid: dict[str, Counter] = {k: Counter() for k in [*labels, NOT_RECORDED]}
    for v in a.main.visits:
        g = v.gender if v.gender in ("Male", "Female", "Other") else "Not recorded"
        grid[_band(v.age_years, cfg.age_bands)][g] += 1
    bands = [
        {"band": k, "male": c["Male"], "female": c["Female"], "other": c["Other"], "not_recorded": c["Not recorded"],
         "total": sum(c.values())}
        for k, c in grid.items() if k != NOT_RECORDED or sum(c.values())
    ]
    aliases = {k.strip().lower(): v for k, v in cfg.test_aliases.items()}
    shown: dict[str, str] = {}
    tc: Counter = Counter()
    for v in a.main.visits:
        keys = set()
        for t in v.tests:
            k = t.strip().lower()
            if not k:
                continue
            k = aliases.get(k, t.strip()).lower()
            shown.setdefault(k, aliases.get(t.strip().lower(), t.strip()))
            keys.add(k)
        tc.update(keys)
    ranked = sorted(tc.items(), key=lambda kv: (-kv[1], shown[kv[0]]))
    top_tests = {"rows": [{"test": shown[k], "visits": n} for k, n in ranked[: cfg.top_n]],
                 "others": sum(n for _, n in ranked[cfg.top_n:])}
    ref: dict[str, dict[str, Any]] = {}
    for v in a.main.visits:
        key = (v.referrer or NOT_RECORDED).lower()
        r = ref.setdefault(key, {"referrer": v.referrer or NOT_RECORDED, "visits": 0, "rev": ZERO})
        r["visits"] += 1
        r["rev"] += v.net if v.billed else ZERO
    total_rev = sum((r["rev"] for r in ref.values()), ZERO)
    ordered = sorted(ref.values(), key=lambda r: (-r["visits"], -r["rev"], r["referrer"]))

    def ref_row(r: dict[str, Any]) -> dict[str, Any]:
        return {"referrer": r["referrer"], "visits": r["visits"], "billed_revenue": money_str(r["rev"]),
                "share_pct": _round1(float(r["rev"] / total_rev * 100)) if total_rev else 0.0}

    rest = ordered[cfg.top_n:]
    others = None
    if rest:
        rest_rev = sum((r["rev"] for r in rest), ZERO)
        others = {"referrer": "Others", "visits": sum(r["visits"] for r in rest), "billed_revenue": money_str(rest_rev),
                  "share_pct": _round1(float(rest_rev / total_rev * 100)) if total_rev else 0.0}
    out = _envelope(a, _patient_notes(a, ps), not a.main.visits, NO_VISITS)
    out.update(
        {
            "kpis": kpis,
            "repeat_building": _repeat_building(a),
            "new_vs_returning": [{"key": p["key"], "label": p["label"], "new": p["new"], "returning": p["returning"]} for p in pts],
            "age_gender": bands,
            "top_tests": top_tests,
            "top_referrers": {"rows": [ref_row(r) for r in ordered[: cfg.top_n]], "others": others},
        }
    )
    return out


def filters_payload(cfg: Any, today: date, fy_month: int, labs: dict[str, LabInfo], earliest: date | None,
                    others_color: str, compare_color: str) -> dict[str, Any]:
    presets = []
    for key in P.PRESETS[:-1]:
        r = P.resolve_period(key, today, fy_month)
        cr = P.compare_range(r, "previous")
        presets.append({"key": key, "label": P.PRESET_LABELS[key], **_range_out(r.range),
                        "compare_start": cr.start.isoformat(), "compare_end": cr.end.isoformat()})
    return {
        "presets": presets,
        "default_period": cfg.default_period,
        "default_compare": cfg.compare_default,
        "compare_options": [{"key": "previous", "label": "Previous period"},
                            {"key": "last_year", "label": "Same period last year"}, {"key": "none", "label": "Off"}],
        "labs": [{"key": v.key, "name": v.name, "color": v.color} for v in labs.values()],
        "earliest_data_date": earliest.isoformat() if earliest else None,
        "today": today.isoformat(),
        "thresholds": {"low_margin_pct": cfg.thresholds.low_margin_pct, "high_discount_pct": cfg.thresholds.high_discount_pct},
        "palette": {"others": others_color, "compare_line": compare_color},
        "report_sections": list(cfg.report.sections),
    }