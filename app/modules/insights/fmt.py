"""Number formatting shared by Highlights text and the PDF report (screens format on the client)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

RUPEE = "\u20b9"


def _group(whole: str) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(value: Decimal, decimals: int = 0, symbol: str = RUPEE) -> str:
    q = Decimal(1).scaleb(-decimals)
    v = value.quantize(q, rounding=ROUND_HALF_UP)
    text = format(abs(v), "f")
    whole, _, frac = text.partition(".")
    out = _group(whole) + (("." + frac) if frac else "")
    return f"{'-' if v < 0 else ''}{symbol}{out}"


def _trim(x: Decimal) -> str:
    return format(x.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f").removesuffix(".0")


def compact_inr(value: Decimal, symbol: str = RUPEE) -> str:
    """Axis form: 4.2L, 1.2Cr; below one lakh the full grouped number."""
    a = abs(value)
    sign = "-" if value < 0 else ""
    if a >= Decimal(10**7):
        return f"{sign}{symbol}{_trim(a / Decimal(10**7))}Cr"
    if a >= Decimal(10**5):
        return f"{sign}{symbol}{_trim(a / Decimal(10**5))}L"
    return inr(value, 0, symbol)


def hours_text(hours: float | None) -> str:
    """Hours, shown as days above 48 h"""
    if hours is None:
        return "No data"
    if hours > 48:
        return f"{hours / 24:.1f} days"
    return f"{hours:.1f} h"


def pct_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"