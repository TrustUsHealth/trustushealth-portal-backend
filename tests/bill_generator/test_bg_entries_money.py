"""domain/entries: exact money, test_amounts, and the lab bill number replacement."""

from decimal import Decimal

import pytest

from app.domain import money
from app.domain.entries import service as entry_service
from app.domain.entries.model import EntryError, check_final, clean_fields, derive, same_value

RAW = {
    "partner_key": "spectrum",
    "patient_name": "Sushant Sawant",
    "age": 56,
    "gender": "Male",
    "lab_pid": "0200118334",
    "registration_date": "2026-10-14",
    "tests": ["Sugar Fasting & Post Prandial", "Routine Examination of Urine"],
    "test_amounts": ["200", "150"],
    "lab_cost": "100",
}


def test_amounts_are_decimal_and_gross_is_their_sum(bg_cfg) -> None:
    out = clean_fields(RAW, bg_cfg, defaults=True)
    assert out["test_amounts"] == [Decimal("200.00"), Decimal("150.00")]
    assert out["gross_amount"] == Decimal("350.00")
    assert out["discount_amount"] == Decimal("0.00")
    assert all(isinstance(v, Decimal) for v in (out["lab_cost"], out["gross_amount"], out["discount_amount"]))


def test_floats_never_enter(bg_cfg) -> None:
    out = clean_fields({"lab_cost": 0.1 + 0.2, "gross_amount": "10.005"}, bg_cfg, defaults=False)
    assert out["lab_cost"] == Decimal("0.30")
    assert out["gross_amount"] == Decimal("10.01")  # half-up, exact
    assert isinstance(out["lab_cost"], Decimal)


def test_derived_values_are_exact() -> None:
    d = derive(
        {
            "patient_name": "A B",
            "registration_date": "2026-10-14",
            "test_amounts": [Decimal("100.10"), Decimal("200.20")],
            "gross_amount": Decimal("1"),  # ignored: the amounts win
            "discount_amount": Decimal("50.05"),
            "lab_cost": Decimal("100"),
        }
    )
    assert d["gross_amount"] == Decimal("300.30")
    assert d["net_amount"] == Decimal("250.25")
    assert d["margin"] == Decimal("150.25")
    assert d["status"] == "complete"


def test_old_float_values_are_read_back_exactly() -> None:
    d = derive({"patient_name": "A B", "registration_date": "2026-10-14", "gross_amount": 0.1, "lab_cost": 0.02})
    assert d["net_amount"] == Decimal("0.10")
    assert d["margin"] == Decimal("0.08")


def test_amount_and_test_counts_must_match(bg_cfg) -> None:
    doc = {**clean_fields(RAW, bg_cfg, defaults=True), "test_amounts": [Decimal("1")]}
    with pytest.raises(EntryError):
        check_final(bg_cfg, "bill_generator", doc)


def test_bill_generator_requires_amounts_and_lab_cost(bg_cfg) -> None:
    doc = clean_fields({k: v for k, v in RAW.items() if k not in ("test_amounts", "lab_cost")}, bg_cfg, defaults=True)
    with pytest.raises(EntryError) as exc:
        check_final(bg_cfg, "bill_generator", doc)
    assert "Test Amounts" in str(exc.value.detail) and "Lab Cost" in str(exc.value.detail)


def test_same_value_compares_money_exactly() -> None:
    assert same_value("gross_amount", Decimal("10.00"), 10.0)
    assert not same_value("gross_amount", Decimal("10.00"), Decimal("10.01"))
    assert same_value("test_amounts", [Decimal("1.0")], [1])


async def test_create_gives_decimal_totals_and_a_bill_no(bg_cfg, bg_db) -> None:
    res = await entry_service.upsert(bg_db, bg_cfg, RAW, module="bill_generator", actor_id="u1")
    e = res.entry
    assert res.outcome == "created"
    assert e["bill_no"] == str(bg_cfg.app.bill_no.start).zfill(bg_cfg.app.bill_no.digits)
    assert e["net_amount"] == Decimal("350.00")
    assert e["margin"] == Decimal("250.00")
    assert e["status"] == "complete"


async def test_same_patient_again_keeps_one_entry_and_one_bill_no(bg_cfg, bg_db) -> None:
    first = await entry_service.upsert(bg_db, bg_cfg, RAW, module="bill_generator", actor_id="u1")
    second = await entry_service.upsert(bg_db, bg_cfg, RAW, module="bill_generator", actor_id="u1")
    assert second.outcome == "unchanged"
    assert second.entry["_id"] == first.entry["_id"]
    assert second.entry["bill_no"] == first.entry["bill_no"]


async def test_replace_bill_no_keeps_the_original_and_is_idempotent(bg_cfg, bg_db) -> None:
    e = (await entry_service.upsert(bg_db, bg_cfg, RAW, module="bill_generator", actor_id="u1")).entry
    internal = e["bill_no"]
    new, replaced = await entry_service.replace_bill_no(bg_db, bg_cfg, str(e["_id"]), "03932102694390", actor_id="u1")
    assert replaced and new["bill_no"] == "03932102694390" and new["bill_no_original"] == internal
    again, replaced_again = await entry_service.replace_bill_no(
        bg_db, bg_cfg, str(e["_id"]), "03932102694390", actor_id="u1"
    )
    assert not replaced_again and again["bill_no_original"] == internal


async def test_a_lab_number_used_by_another_entry_is_refused(bg_cfg, bg_db) -> None:
    a = (await entry_service.upsert(bg_db, bg_cfg, RAW, module="bill_generator", actor_id="u1")).entry
    other = {**RAW, "patient_name": "Kalphem Ms", "lab_pid": "0200118999", "gender": "Female"}
    b = (await entry_service.upsert(bg_db, bg_cfg, other, module="bill_generator", actor_id="u1")).entry
    await entry_service.replace_bill_no(bg_db, bg_cfg, str(a["_id"]), "03932102694390", actor_id="u1")
    with pytest.raises(EntryError) as exc:
        await entry_service.replace_bill_no(bg_db, bg_cfg, str(b["_id"]), "03932102694390", actor_id="u1")
    assert exc.value.status == 409 and exc.value.detail["code"] == "bill_no_taken"
    assert (await bg_db["entries"].find_one({"_id": b["_id"]}))["bill_no"] == b["bill_no"]  # nothing was written


async def test_counter_ignores_lab_numbers(bg_cfg, bg_db) -> None:
    e = (await entry_service.upsert(bg_db, bg_cfg, RAW, module="bill_generator", actor_id="u1")).entry
    internal = int(e["bill_no"])
    await entry_service.replace_bill_no(bg_db, bg_cfg, str(e["_id"]), "99999999999999", actor_id="u1")
    assert await entry_service._highest_issued(bg_db) == internal
    await entry_service.ensure_counter(bg_db, bg_cfg)
    assert await entry_service.next_bill_no(bg_db, bg_cfg) == str(internal + 1).zfill(bg_cfg.app.bill_no.digits)


def test_money_helper_rejects_non_numbers() -> None:
    with pytest.raises(Exception):
        money.to_decimal("abc")