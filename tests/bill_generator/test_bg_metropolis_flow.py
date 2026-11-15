"""The Metropolis flow end to end (service level): match, total check, edit, save, lab bill number."""

from decimal import Decimal

import pymupdf
import pytest

from app.domain.bills.errors import BillError
from app.domain.entries.model import EntryError
from app.modules.bill_generator import service

ACTOR = "u1"


@pytest.fixture
def lab_pdf(samples) -> bytes:
    return (samples / "metropolis" / "177934169.pdf").read_bytes()


async def _render(bg_db, bg_cfg, pdf, amounts, **kw):
    return await service.render_lab_bill(
        bg_db,
        bg_cfg,
        bg_cfg.partners["metropolis"],
        data=pdf,
        amounts=amounts,
        lab_cost=kw.pop("lab_cost", Decimal("300")),
        update_total=kw.pop("update_total", False),
        entry_id=kw.pop("entry_id", None),
        force_new=kw.pop("force_new", False),
        actor_id=ACTOR,
    )


async def test_first_render_creates_the_entry_with_the_lab_number(bg_db, bg_cfg, lab_pdf) -> None:
    res = await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")])
    e = res.entry
    assert pymupdf.open(stream=res.pdf, filetype="pdf").page_count == 1
    assert res.filename == "JAYANTILAL_NISHAR-bill_14-10-2026.pdf"
    assert e["bill_no"] == "03932102694390"
    assert e["bill_no_original"] != e["bill_no"]
    assert e["partner_key"] == "metropolis" and e["lab_pid"] == "P39326580613471"
    assert e["tests"] == ["CBC Haemogram, EDTA Blood"]
    assert e["test_amounts"] == [Decimal("450.00")]
    assert e["gross_amount"] == Decimal("450.00") and e["net_amount"] == Decimal("450.00")
    assert e["margin"] == Decimal("150.00") and e["status"] == "complete"
    assert "bill_no" in res.changed
    assert await bg_db["entries"].count_documents({}) == 1


async def test_regenerating_the_same_bill_is_idempotent(bg_db, bg_cfg, lab_pdf) -> None:
    first = await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")])
    second = await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")])
    assert second.entry["_id"] == first.entry["_id"]
    assert second.entry["bill_no"] == "03932102694390"
    assert second.entry["version"] == first.entry["version"]
    assert second.changed == [] and second.pdf == first.pdf
    assert await bg_db["entries"].count_documents({}) == 1


async def test_total_mismatch_asks_first_then_updates(bg_db, bg_cfg, lab_pdf) -> None:
    await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")])
    with pytest.raises(BillError) as exc:
        await _render(bg_db, bg_cfg, lab_pdf, [Decimal("500")])
    assert exc.value.status == 409 and exc.value.code == "total_mismatch"
    assert exc.value.extra == {"entry_total": "450.00", "bill_total": "500.00"}
    stored = await bg_db["entries"].find_one({})
    assert stored["gross_amount"] == Decimal("450.00")  # nothing changed while asking
    res = await _render(bg_db, bg_cfg, lab_pdf, [Decimal("500")], update_total=True)
    assert res.entry["gross_amount"] == Decimal("500.00")


async def test_lab_number_taken_by_another_entry_saves_nothing(bg_db, bg_cfg, lab_pdf) -> None:
    from app.domain.entries import service as entry_service

    other = await entry_service.upsert(
        bg_db,
        bg_cfg,
        {
            "partner_key": "spectrum",
            "patient_name": "Someone Else",
            "registration_date": "2026-10-01",
            "tests": ["CBC"],
            "test_amounts": ["100"],
            "lab_cost": "50",
        },
        module="bill_generator",
        actor_id=ACTOR,
    )
    await entry_service.replace_bill_no(bg_db, bg_cfg, str(other.entry["_id"]), "03932102694390", actor_id=ACTOR)
    with pytest.raises(BillError) as exc:
        await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")])
    assert exc.value.status == 409 and exc.value.code == "bill_no_taken"
    assert await bg_db["entries"].count_documents({}) == 1  # no second entry was created


async def test_lab_cost_is_required_to_save(bg_db, bg_cfg, lab_pdf) -> None:
    with pytest.raises((EntryError, BillError)):
        await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")], lab_cost=None)
    assert await bg_db["entries"].count_documents({}) == 0


async def test_inspect_reports_rows_and_match(bg_db, bg_cfg, lab_pdf) -> None:
    before = await service.inspect_lab_bill(bg_db, bg_cfg, bg_cfg.partners["metropolis"], data=lab_pdf)
    assert before["match"]["kind"] == "none" and before["bill_no_taken"] is False
    assert before["rows"][0]["amount"] == Decimal("400.00")  # printed amount when the entry has none
    await _render(bg_db, bg_cfg, lab_pdf, [Decimal("450")])
    after = await service.inspect_lab_bill(bg_db, bg_cfg, bg_cfg.partners["metropolis"], data=lab_pdf)
    assert after["match"]["kind"] == "exact" and after["bill_no_taken"] is False
    assert after["rows"][0]["amount"] == Decimal("450.00")  # prefilled from the entry
    assert after["entry_gross"] == Decimal("450.00")


def test_amounts_parsing() -> None:
    assert service.parse_amounts('["450.00", 10]') == [Decimal("450.00"), Decimal("10.00")]
    for bad in ("nope", "[]", '["x"]', '["-1"]'):
        with pytest.raises(BillError):
            service.parse_amounts(bad)


def test_rate_limit_is_per_user_and_per_minute() -> None:
    for _ in range(service.RENDER_PER_MINUTE):
        service.check_rate("render-test", "user-a", service.RENDER_PER_MINUTE)
    with pytest.raises(BillError) as exc:
        service.check_rate("render-test", "user-a", service.RENDER_PER_MINUTE)
    assert exc.value.status == 429
    service.check_rate("render-test", "user-b", service.RENDER_PER_MINUTE)  # another user is unaffected