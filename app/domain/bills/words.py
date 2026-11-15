"""Amount in words, Indian system (crore, lakh, thousand, hundred), Title Case."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain import money

_ONES = [
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _below_1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_below_100(rest))
    return " ".join(parts)


def _int_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts: list[str] = []
    for divisor, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        quotient, n = divmod(n, divisor)
        if quotient:
            parts.append(f"{_int_words(quotient)} {name}")
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_words(amount: Any) -> str:
    """1200 -> 'One Thousand Two Hundred'. Paise, when present: 'Fifty And Fifty Paise'."""
    d: Decimal = money.to_decimal(amount)
    if d < 0:
        raise ValueError("an amount in words cannot be negative")
    rupees = int(d)
    paise = int((d - rupees) * 100)
    text = _int_words(rupees)
    if paise:
        text = f"{text} And {_int_words(paise)} Paise"
    return text


def amount_words_only(amount: Any) -> str:
    """1200 -> 'One Thousand Two Hundred Only'."""
    return f"{amount_words(amount)} Only"