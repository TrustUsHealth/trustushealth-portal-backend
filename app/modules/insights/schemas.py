"""Response shapes for Business Insights. Money travels as strings of exact decimals and is formatted at the edge."""

from typing import Literal

from pydantic import BaseModel

Grain = Literal["day", "week", "month"]
Direction = Literal["up", "down", "flat", "new"]


class RangeOut(BaseModel):
    start: str
    end: str
    label: str


class FiltersEcho(BaseModel):
    period: str
    labs: list[str]
    compare: str


class Envelope(BaseModel):
    range: RangeOut
    compare_range: RangeOut | None
    grain: Grain
    generated_at: str
    data_notes: list[str]
    empty: bool
    empty_message: str | None
    filters: FiltersEcho


class KpiOut(BaseModel):
    key: str
    label: str
    kind: Literal["money", "count", "percent"]
    value: str | int | float | None
    compare_value: str | int | float | None
    delta_pct: float | None  # percent change; percentage points for kind=percent
    direction: Direction | None  # null when Compare is off
    detail: str | None
    sparkline: list[float]


# ───────────── overview ─────────────
class TrendPoint(BaseModel):
    key: str
    label: str
    visits: int
    billed_visits: int
    billed_revenue: str
    margin: str
    lab_cost: str
    revenue_no_cost: str
    gross_billed: str
    discounts: str
    discount_rate_pct: float | None
    patients: int
    new: int
    returning: int
    compare_billed_revenue: str | None = None


class RevenueByLab(BaseModel):
    lab: str
    name: str
    color: str
    billed_revenue: str
    share_pct: float
    visits: int


class AttentionChip(BaseModel):
    key: str
    label: str
    count: int
    link: str


class OverviewOut(Envelope):
    kpis: list[KpiOut]
    highlights: list[str]
    trend: list[TrendPoint]
    revenue_by_lab: list[RevenueByLab]
    attention: list[AttentionChip]


# ───────────── finance ─────────────
class MoneySplit(BaseModel):
    key: str
    label: str
    lab_cost: str
    margin: str
    revenue_no_cost: str
    billed_revenue: str


class MarginByLab(BaseModel):
    lab: str
    name: str
    color: str
    margin_pct: float
    below_threshold: bool


class DiscountPoint(BaseModel):
    key: str
    label: str
    discounts: str
    discount_rate_pct: float | None


class LabFinanceRow(BaseModel):
    lab: str
    name: str
    color: str
    billed_visits: int
    billed_revenue: str
    lab_cost: str
    margin: str
    margin_pct: float | None
    discount_rate_pct: float | None


class LabFinanceTotals(BaseModel):
    billed_visits: int
    billed_revenue: str
    lab_cost: str
    margin: str
    margin_pct: float | None
    discount_rate_pct: float | None


class LabTable(BaseModel):
    rows: list[LabFinanceRow]
    totals: LabFinanceTotals


class FinanceStats(BaseModel):
    avg_bill_value: str | None
    awaiting_billing: int
    below_cost_bills: int


class FinanceOut(Envelope):
    kpis: list[KpiOut]
    money_split: list[MoneySplit]
    margin_by_lab: list[MarginByLab]
    low_margin_threshold_pct: float
    discounts: list[DiscountPoint]
    lab_table: LabTable
    stats: FinanceStats


class BelowCostItem(BaseModel):
    bill_no: str | None
    date: str
    lab: str
    lab_name: str
    billed: str
    lab_cost: str
    margin: str


class BelowCostOut(Envelope):
    total: int
    truncated: bool
    items: list[BelowCostItem]


# ───────────── labs ─────────────
class LabCard(BaseModel):
    lab: str
    name: str
    color: str
    visits: int
    billed_revenue: str
    margin_pct: float | None
    median_turnaround_hours: float | None
    median_turnaround_display: str


class LabRef(BaseModel):
    lab: str
    name: str
    color: str


class VisitsByLabPoint(BaseModel):
    key: str
    label: str
    counts: dict[str, int]


class VisitsByLab(BaseModel):
    labs: list[LabRef]
    points: list[VisitsByLabPoint]


class TurnaroundRow(BaseModel):
    lab: str
    name: str
    color: str
    median_hours: float | None
    slowest_10_hours: float | None
    median_display: str
    slowest_10_display: str
    sample: int


class MonthRef(BaseModel):
    key: str
    label: str


class CostStatementRow(BaseModel):
    lab: str
    name: str
    cells: dict[str, str]
    total: str


class CostStatement(BaseModel):
    months: list[MonthRef]
    rows: list[CostStatementRow]
    totals: dict[str, str]
    grand_total: str


class LabsOut(Envelope):
    lab_cards: list[LabCard]
    visits_by_lab: VisitsByLab
    turnaround: list[TurnaroundRow]
    lab_cost_statement: CostStatement


# ───────────── patients ─────────────
class NewReturningPoint(BaseModel):
    key: str
    label: str
    new: int
    returning: int


class AgeGenderRow(BaseModel):
    band: str
    male: int
    female: int
    other: int
    not_recorded: int
    total: int


class TestRow(BaseModel):
    test: str
    visits: int


class TopTests(BaseModel):
    rows: list[TestRow]
    others: int


class ReferrerRow(BaseModel):
    referrer: str
    visits: int
    billed_revenue: str
    share_pct: float


class TopReferrers(BaseModel):
    rows: list[ReferrerRow]
    others: ReferrerRow | None


class PatientsOut(Envelope):
    kpis: list[KpiOut]
    repeat_building: bool
    new_vs_returning: list[NewReturningPoint]
    age_gender: list[AgeGenderRow]
    top_tests: TopTests
    top_referrers: TopReferrers


# ───────────── filters ─────────────
class PresetOut(BaseModel):
    key: str
    label: str
    start: str
    end: str
    compare_start: str
    compare_end: str


class OptionOut(BaseModel):
    key: str
    label: str


class LabFilter(BaseModel):
    key: str
    name: str
    color: str


class ThresholdsOut(BaseModel):
    low_margin_pct: float
    high_discount_pct: float


class PaletteOut(BaseModel):
    others: str
    compare_line: str


class FiltersOut(BaseModel):
    presets: list[PresetOut]
    default_period: str
    default_compare: str
    compare_options: list[OptionOut]
    labs: list[LabFilter]
    earliest_data_date: str | None
    today: str
    thresholds: ThresholdsOut
    palette: PaletteOut
    report_sections: list[str]