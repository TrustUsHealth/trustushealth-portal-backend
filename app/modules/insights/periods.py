"""Period presets, compare ranges, time grain and chart buckets.

Pure date logic: no database, no web framework. All dates are calendar dates in the portal timezone (IST), the same
`registration_date` values the Excel month sheets use.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

PRESETS = ("this_month", "last_month", "last_3_months", "this_fy", "last_fy", "custom")
COMPARES = ("previous", "last_year", "none")
PRESET_LABELS = {
    "this_month": "This Month",
    "last_month": "Last Month",
    "last_3_months": "Last 3 Months",
    "this_fy": "This FY",
    "last_fy": "Last FY",
    "custom": "Custom",
}
COMPARE_LABELS = {
    "previous": "the previous period",
    "last_year": "the same period last year",
    "none": "",
}
MAX_SPAN_DAYS = 3660  # ten years: a sanity limit, not a business rule
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class PeriodError(ValueError):
    """A filter combination that cannot be used; the message is shown to the Admin as is."""


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def label(self) -> str:
        return f"{fmt_day(self.start)} to {fmt_day(self.end)}"


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str


@dataclass(frozen=True)
class Resolved:
    """A period preset turned into dates, plus what the Compare rule needs."""

    preset: str
    range: DateRange
    running: bool  # still in progress (ends today), so "previous" means the same number of days
    unit_months: int | None  # calendar length of the preset (1, 3 or 12 months); None for custom


def fmt_day(d: date) -> str:
    return f"{d.day} {MONTH_ABBR[d.month - 1]} {d.year}"


def add_months(d: date, n: int) -> date:
    """Move by whole months, clamping the day (31 Mar - 1 month = 28/29 Feb)."""
    year, month0 = divmod(d.year * 12 + d.month - 1 + n, 12)
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def month_start(d: date) -> date:
    return d.replace(day=1)


def fy_start(d: date, start_month: int) -> date:
    year = d.year if d.month >= start_month else d.year - 1
    return date(year, start_month, 1)


def resolve_period(
    preset: str, today: date, fy_start_month: int, start: date | None = None, end: date | None = None
) -> Resolved:
    """Turn a preset (or a custom start/end) into dates. `Last 3 Months` = this month so far plus the two before."""
    if preset == "custom":
        if start is None or end is None:
            raise PeriodError("Choose both a start and an end date for a custom period.")
        if start > end:
            raise PeriodError("The start date must be on or before the end date.")
        if (end - start).days + 1 > MAX_SPAN_DAYS:
            raise PeriodError("That period is too long. Choose ten years or less.")
        return Resolved("custom", DateRange(start, end), running=end >= today, unit_months=None)
    ms = month_start(today)
    if preset == "this_month":
        return Resolved(preset, DateRange(ms, today), running=True, unit_months=1)
    if preset == "last_month":
        return Resolved(preset, DateRange(add_months(ms, -1), ms - timedelta(days=1)), running=False, unit_months=1)
    if preset == "last_3_months":
        return Resolved(preset, DateRange(add_months(ms, -2), today), running=True, unit_months=3)
    fys = fy_start(today, fy_start_month)
    if preset == "this_fy":
        return Resolved(preset, DateRange(fys, today), running=True, unit_months=12)
    if preset == "last_fy":
        return Resolved(preset, DateRange(add_months(fys, -12), fys - timedelta(days=1)), running=False, unit_months=12)
    raise PeriodError(f"Unknown period '{preset}'.")


def compare_range(res: Resolved, mode: str) -> DateRange | None:
    """The earlier range a period is measured against.

    A running period (this month so far) is compared with the same number of days of the earlier one, never with a
    full month.
    """
    if mode == "none":
        return None
    r = res.range
    if mode == "last_year":
        return DateRange(add_months(r.start, -12), add_months(r.end, -12))
    if mode != "previous":
        raise PeriodError(f"Unknown compare option '{mode}'.")
    if res.unit_months is None:  # custom: an equally long range right before
        end = r.start - timedelta(days=1)
        return DateRange(end - timedelta(days=r.days - 1), end)
    start = add_months(r.start, -res.unit_months)
    full_end = r.start - timedelta(days=1)
    if res.running:
        return DateRange(start, min(start + timedelta(days=r.days - 1), full_end))
    return DateRange(start, full_end)


def grain_for(r: DateRange) -> str:
    """Daily up to 31 days, weekly (Mon to Sun) up to 120 days, monthly beyond that."""
    if r.days <= 31:
        return "day"
    if r.days <= 120:
        return "week"
    return "month"


def bucket_key(d: date, grain: str) -> str:
    if grain == "day":
        return d.isoformat()
    if grain == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    return f"{d.year}-{d.month:02d}"


def _bucket_label(d: date, grain: str) -> str:
    if grain == "month":
        return f"{MONTH_ABBR[d.month - 1]} {d.year}"
    start = d - timedelta(days=d.weekday()) if grain == "week" else d
    return f"{start.day} {MONTH_ABBR[start.month - 1]}"


def buckets(r: DateRange, grain: str) -> list[Bucket]:
    """Every bucket the range touches, in order, including empty ones so charts have no gaps."""
    out: list[Bucket] = []
    seen: set[str] = set()
    d = r.start
    while d <= r.end:
        key = bucket_key(d, grain)
        if key not in seen:
            seen.add(key)
            out.append(Bucket(key, _bucket_label(d, grain)))
        d += timedelta(days=1)
    return out


def months_in(r: DateRange) -> list[Bucket]:
    return buckets(r, "month")