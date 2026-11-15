from decimal import Decimal

import pytest

from app.domain import money
from app.domain.bills.data import age_gender_text, bill_filename, build_bill_data, referred_by_text
from app.domain.bills.errors import BillError


def test_plain_sample_values(sushant_entry) -> None:
    d = build_bill_data(sushant_entry, include_client=False)
    assert (d.patient_title, d.patient_name) == ("Mr.", "SUSHANT SAWANT")
    assert d.age_gender == "56 Years / Male"
    assert d.referred_by == "Dr. TORASKAR"
    assert d.registration_no == "0200118334"
    assert d.bill_no == "556635"
    assert d.registration_date == "14/10/2026"
    assert [(r.name, r.amount) for r in d.rows] == [
        ("Sugar Fasting & Post Prandial", "200"),
        ("Routine Examination of Urine", "150"),
    ]
    assert d.total == "350"
    assert d.total_words == "Three Hundred Fifty Only"
    assert d.discount_block is None
    assert d.client_name is None
    assert d.filename == "SUSHANT_SAWANT-bill_14-10-2026.pdf"


def test_with_client_and_discount(ruju_entry) -> None:
    d = build_bill_data(ruju_entry, include_client=True)
    assert (d.patient_title, d.patient_name) == ("Ms.", "RUJU PRASANNA SHREEJI")
    assert d.client_name == "CHANDRAPRABH JAIN SANGH"
    assert d.age_gender == "43 Years / Female"
    assert d.total == "1200"
    assert d.total_words == "One Thousand Two Hundred Only"
    assert d.discount_block is not None
    assert (d.discount_block.discount, d.discount_block.net, d.discount_block.net_words) == (
        "240",
        "960",
        "Nine Hundred Sixty Only",
    )
    assert d.filename == "RUJU_PRASANNA_SHREEJI-bill_08-11-2024.pdf"


def test_client_name_hidden_unless_chosen(ruju_entry) -> None:
    assert build_bill_data(ruju_entry, include_client=False).client_name is None


def test_client_required_when_included(sushant_entry) -> None:
    with pytest.raises(BillError) as exc:
        build_bill_data(sushant_entry, include_client=True)
    assert exc.value.code == "client_name_required"


def test_amounts_must_match_tests(sushant_entry) -> None:
    sushant_entry["test_amounts"] = [Decimal("200")]
    with pytest.raises(BillError) as exc:
        build_bill_data(sushant_entry, include_client=False)
    assert exc.value.code == "amounts_missing"


def test_discount_cannot_exceed_total(sushant_entry) -> None:
    sushant_entry["discount_amount"] = Decimal("351")
    with pytest.raises(BillError) as exc:
        build_bill_data(sushant_entry, include_client=False)
    assert exc.value.code == "discount_too_large"


def test_paise_are_printed_only_when_present() -> None:
    assert money.plain(Decimal("200.00")) == "200"
    assert money.plain(Decimal("200.50")) == "200.50"
    assert money.fixed(Decimal("400")) == "400.00"


def test_age_units_and_blank_referrer() -> None:
    assert age_gender_text(6, "M", "Female") == "6 Months / Female"
    assert age_gender_text(1, "Y", "Male") == "1 Year / Male"
    assert age_gender_text(None, None, "Male") == "Male"
    assert referred_by_text(None) == ""
    assert referred_by_text("Self") == "SELF"


def test_filename_convention() -> None:
    from datetime import date

    assert bill_filename("Kalphem MS", date(2026, 10, 14)) == "KALPHEM_MS-bill_14-10-2026.pdf"