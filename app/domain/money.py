"""Exact money helpers. Money is Decimal everywhere; never float."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from bson.decimal128 import Decimal128

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("10000000")


def to_decimal(value: Any) -> Decimal:
    """Any stored or typed amount -> Decimal with two places. Floats read from old data go through str()."""
    if isinstance(value, bool):
        raise InvalidOperation("a boolean is not an amount")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, Decimal128):
        d = value.to_decimal()
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        d = Decimal(str(value).replace(",", "").strip())
    if not d.is_finite():
        raise InvalidOperation("amount is not finite")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def opt(value: Any) -> Decimal | None:
    return None if value is None or value == "" else to_decimal(value)


def total(values: Iterable[Any]) -> Decimal:
    return sum((to_decimal(v) for v in values), ZERO)


def fixed(value: Any) -> str:
    """Always two decimals: 400.00."""
    return f"{to_decimal(value):.2f}"


def plain(value: Any) -> str:
    """Whole number without symbol or separators; two decimals only if paise exist."""
    d = to_decimal(value)
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"