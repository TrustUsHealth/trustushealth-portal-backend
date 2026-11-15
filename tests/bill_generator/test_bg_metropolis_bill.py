"""Metropolis: read the lab's PDF and change only the permitted cells."""

from collections import Counter
from datetime import date
from decimal import Decimal

import pymupdf
import pytest

from app.core.config import BillCfg
from app.domain.bills.errors import BillError
from app.partners.metropolis import bill

CFG = BillCfg(
    mode="edit_pdf",
    bill_no_source="lab",
    editable_cells=[
        "test_amount",
        "gross_total",
        "grand_total",
        "received_amount",
        "payment_amount",
        "amount_received_in_words",
    ],
    words_template="(INR) {words} ONLY.",
)


@pytest.fixture
def original(samples) -> bytes:
    return (samples / "metropolis" / "177934169.pdf").read_bytes()


def _words(data: bytes) -> Counter:
    page = pymupdf.open(stream=data, filetype="pdf")[0]
    return Counter(w[4] for w in page.get_text("words"))


def test_reads_the_sample(original) -> None:
    lab = bill.read_bill(original)
    assert lab.bill_no == "03932102694390"
    assert lab.patient_no == "P39326580613471"
    assert lab.bill_date == date(2026, 10, 14)
    assert lab.patient_name == "Jayantilal Nishar"
    assert (lab.age, lab.age_unit, lab.gender) == (65, "Y", "Male")
    assert lab.referred_by == "Dr. Chirag Parikh"
    assert [(r.name, r.amount) for r in lab.rows] == [("CBC Haemogram, EDTA Blood", Decimal("400.00"))]
    assert (lab.gross, lab.discount, lab.grand, lab.received, lab.due) == (
        Decimal("400.00"),
        Decimal("0.00"),
        Decimal("400.00"),
        Decimal("400.00"),
        Decimal("0.00"),
    )
    assert lab.payment == Decimal("400.00")
    assert lab.words_text == "(INR) FOUR HUNDRED ONLY."


def test_garbage_is_not_a_bill() -> None:
    with pytest.raises(BillError) as exc:
        bill.read_bill(b"%PDF-1.4 not really")
    assert exc.value.code == "unreadable_bill"


def test_new_amount_changes_only_the_permitted_cells(original) -> None:
    lab = bill.read_bill(original)
    out = bill.edit_bill(original, lab, amounts=[Decimal("450")], discount=Decimal("0"), bill_cfg=CFG)
    before, after = _words(original), _words(out)
    # five amount cells (row, gross, grand, received, payment) and the words line; nothing else
    assert after - before == Counter({"450.00": 5, "FIFTY": 1})
    assert before - after == Counter({"400.00": 5})
    assert pymupdf.open(stream=out, filetype="pdf").page_count == 1
    again = bill.read_bill(out)
    assert (again.gross, again.grand, again.received, again.payment, again.due) == (
        Decimal("450.00"),
        Decimal("450.00"),
        Decimal("450.00"),
        Decimal("450.00"),
        Decimal("0.00"),
    )
    assert again.words_text == "(INR) FOUR HUNDRED FIFTY ONLY."


def test_discount_changes_only_when_given(original) -> None:
    lab = bill.read_bill(original)
    out = bill.edit_bill(original, lab, amounts=[Decimal("450")], discount=Decimal("50"), bill_cfg=CFG)
    before, after = _words(original), _words(out)
    # grand, received, payment and the words stay 400.00 / FOUR HUNDRED: only the row, gross and discount change
    assert after - before == Counter({"450.00": 2, "50.00": 1})
    assert before - after == Counter({"400.00": 2, "0.00": 1})
    again = bill.read_bill(out)
    assert (again.gross, again.discount, again.grand) == (Decimal("450.00"), Decimal("50.00"), Decimal("400.00"))


def test_same_amounts_return_the_lab_pdf_untouched(original) -> None:
    lab = bill.read_bill(original)
    assert bill.edit_bill(original, lab, amounts=[Decimal("400")], discount=Decimal("0"), bill_cfg=CFG) == original


def test_row_count_must_match(original) -> None:
    lab = bill.read_bill(original)
    with pytest.raises(BillError) as exc:
        bill.edit_bill(original, lab, amounts=[Decimal("1"), Decimal("2")], discount=Decimal("0"), bill_cfg=CFG)
    assert exc.value.code == "amount_count"


def test_discount_cannot_exceed_total(original) -> None:
    lab = bill.read_bill(original)
    with pytest.raises(BillError) as exc:
        bill.edit_bill(original, lab, amounts=[Decimal("100")], discount=Decimal("101"), bill_cfg=CFG)
    assert exc.value.code == "discount_too_large"


def test_due_amount_must_stay_zero(original) -> None:
    doc = pymupdf.open(stream=original, filetype="pdf")
    page = doc[0]
    words = page.get_text("words")
    label = next(w for w in words if w[4] == "Due")
    cy = (label[1] + label[3]) / 2
    target = next(w for w in words if w[4] == "0.00" and abs((w[1] + w[3]) / 2 - cy) < 2)
    page.add_redact_annot(pymupdf.Rect(target[0], target[1] + 1.2, target[2], target[3] - 1.2), fill=False)
    page.apply_redactions()
    page.insert_text(pymupdf.Point(target[0], label[3] - 2.6), "5.00", fontname="helv", fontsize=8)
    tampered = doc.tobytes()
    lab = bill.read_bill(tampered)
    assert lab.due == Decimal("5.00")
    with pytest.raises(BillError) as exc:
        bill.edit_bill(tampered, lab, amounts=[Decimal("450")], discount=Decimal("0"), bill_cfg=CFG)
    assert exc.value.code == "due_not_zero"


def test_verification_rejects_any_other_difference(original) -> None:
    doc = pymupdf.open(stream=original, filetype="pdf")
    doc[0].insert_text(pymupdf.Point(100, 700), "Extra", fontname="helv", fontsize=8)
    other = doc.tobytes()
    with pytest.raises(BillError) as exc:
        bill._verify(original, other, [])
    assert exc.value.code == "verification_failed"


def test_cells_not_listed_as_editable_are_refused(original) -> None:
    lab = bill.read_bill(original)
    limited = CFG.model_copy(update={"editable_cells": ["test_amount"]})
    with pytest.raises(BillError) as exc:
        bill.edit_bill(original, lab, amounts=[Decimal("450")], discount=Decimal("0"), bill_cfg=limited)
    assert exc.value.code == "config"