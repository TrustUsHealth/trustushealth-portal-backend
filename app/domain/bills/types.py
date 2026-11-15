"""Plain data holders for bills."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BillRow:
    name: str
    amount: str  # already formatted for printing


@dataclass(frozen=True)
class DiscountBlock:
    discount: str
    net: str
    net_words: str


@dataclass(frozen=True)
class BillData:
    """Everything a Spectrum bill prints. Lab cost and margin are deliberately absent."""

    patient_title: str  # 'Mr.', 'Ms.' or ''  (derived from gender; titles are not stored)
    patient_name: str  # upper case
    age_gender: str
    referred_by: str
    registration_no: str
    bill_no: str
    registration_date: str  # dd/mm/yyyy
    client_name: str | None  # upper case, only when the client row is printed
    rows: tuple[BillRow, ...]
    total: str
    total_words: str
    discount_block: DiscountBlock | None
    filename: str