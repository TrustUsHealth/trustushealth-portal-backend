from decimal import Decimal

import pytest

from app.domain.bills.words import amount_words, amount_words_only


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1200", "One Thousand Two Hundred Only"),
        ("350", "Three Hundred Fifty Only"),
        ("960", "Nine Hundred Sixty Only"),
        ("400", "Four Hundred Only"),
        ("19", "Nineteen Only"),
        ("100000", "One Lakh Only"),
        ("10000000", "One Crore Only"),
        ("12345678", "One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight Only"),
        ("0", "Zero Only"),
        ("150.50", "One Hundred Fifty And Fifty Paise Only"),
    ],
)
def test_indian_words(value: str, expected: str) -> None:
    assert amount_words_only(Decimal(value)) == expected


def test_words_without_only() -> None:
    assert amount_words(Decimal("450")) == "Four Hundred Fifty"


def test_negative_is_refused() -> None:
    with pytest.raises(ValueError):
        amount_words(Decimal("-1"))