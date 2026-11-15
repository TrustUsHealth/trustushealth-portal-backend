"""Bill data assembly: from an entry to exactly what a bill prints. Nothing is stored."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from app.domain import money
from app.domain.bills.errors import BillError
from app.domain.bills.types import BillData, BillRow, DiscountBlock
from app.domain.bills.words import amount_words_only
from app.domain.parsing import standardize as std

_TITLES = {"Male": "Mr.", "Female": "Ms."}
_UNITS = {"Y": ("Year", "Years"), "M": ("Month", "Months"), "D": ("Day", "Days")}


def age_gender_text(age: int | None, unit: str | None, gender: str | None) -> str:
    """'43 Years / Female'."""
    parts: list[str] = []
    if age is not None:
        one, many = _UNITS.get(unit or "Y", _UNITS["Y"])
        parts.append(f"{age} {one if age == 1 else many}")
    if gender:
        parts.append(gender)
    return " / ".join(parts)


def referred_by_text(value: Any) -> str:
    """'Dr. Toraskar' -> 'Dr. TORASKAR'."""
    v = std.clean_text(value)
    if not v:
        return ""
    if v.startswith("Dr. "):
        return "Dr. " + v[4:].upper()
    return v.upper()


def bill_filename(patient_name: str, day: date) -> str:
    """<NAME_WITH_UNDERSCORES>-bill_<dd-mm-yyyy>.pdf"""
    base = re.sub(r"[^A-Z0-9]+", "_", patient_name.upper()).strip("_") or "PATIENT"
    return f"{base}-bill_{day:%d-%m-%Y}.pdf"


def build_bill_data(
    entry: dict[str, Any], *, include_client: bool, discount_amount: Decimal | None = None
) -> BillData:
    tests = list(entry.get("tests") or [])
    raw_amounts = list(entry.get("test_amounts") or [])
    if not tests or len(tests) != len(raw_amounts):
        raise BillError(422, "amounts_missing", "Enter an amount for every test before making the bill.")
    amounts = [money.to_decimal(a) for a in raw_amounts]
    client = std.clean_text(entry.get("client_name"))
    if include_client and not client:
        raise BillError(422, "client_name_required", "Enter the client name, or switch off 'Include client name'.")

    gross = money.total(amounts)
    discount = money.opt(discount_amount) if discount_amount is not None else money.opt(entry.get("discount_amount"))
    discount = discount or money.ZERO
    if discount > gross:
        raise BillError(422, "discount_too_large", "The discount cannot be more than the total.")
    net = gross - discount

    day = date.fromisoformat(entry["registration_date"])
    gender = entry.get("gender")
    return BillData(
        patient_title=_TITLES.get(gender or "", ""),
        patient_name=std.clean_text(entry["patient_name"]).upper(),
        age_gender=age_gender_text(entry.get("age"), entry.get("age_unit"), gender),
        referred_by=referred_by_text(entry.get("referred_by")),
        registration_no=entry.get("lab_pid") or "",
        bill_no=entry.get("bill_no") or "",
        registration_date=day.strftime("%d/%m/%Y"),
        client_name=client.upper() if include_client else None,
        rows=tuple(BillRow(name=t, amount=money.plain(a)) for t, a in zip(tests, amounts, strict=True)),
        total=money.plain(gross),
        total_words=amount_words_only(gross),
        discount_block=(
            DiscountBlock(money.plain(discount), money.plain(net), amount_words_only(net)) if discount > 0 else None
        ),
        filename=bill_filename(entry["patient_name"], day),
    )