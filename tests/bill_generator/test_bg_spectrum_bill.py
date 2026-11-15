"""Layout golden tests: the generated bills must match the two measured sample bills."""

from decimal import Decimal

import pymupdf
import pytest

from app.core.config import BillCfg
from app.domain.bills.data import build_bill_data
from app.domain.bills.errors import BillError
from app.modules.bill_generator import service
from app.partners.spectrum import bill

CFG = BillCfg(
    mode="generate",
    variants=["plain", "with_client"],
    signature_asset="assets/bill/signature.png",
    amount_words="indian_title_case_only",
    discount_block="when_discount_above_zero",
)


def _words_with_baseline(page: pymupdf.Page) -> list[tuple[str, float, float]]:
    spans = [s for b in page.get_text("dict")["blocks"] if b["type"] == 0 for ln in b["lines"] for s in ln["spans"]]
    out = []
    for w in page.get_text("words"):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        span = next(
            (
                s
                for s in spans
                if s["bbox"][0] - 0.5 <= cx <= s["bbox"][2] + 0.5 and s["bbox"][1] - 0.5 <= cy <= s["bbox"][3] + 0.5
            ),
            None,
        )
        if span is not None:
            out.append((w[4], w[0], span["origin"][1]))
    return out


def _assert_matches_sample(sample_path, generated: bytes, *, tol_x: float = 1.5, tol_y: float = 0.8) -> None:
    sample = pymupdf.open(sample_path)[0]
    page = pymupdf.open(stream=generated, filetype="pdf")[0]
    assert page.rect.width == pytest.approx(sample.rect.width, abs=0.05)
    assert page.rect.height == pytest.approx(sample.rect.height, abs=0.05)
    got = _words_with_baseline(page)
    for text, x0, base in _words_with_baseline(sample):
        assert any(
            t == text and abs(x - x0) <= tol_x and abs(b - base) <= tol_y for t, x, b in got
        ), f"word {text!r} at ({x0:.1f}, {base:.1f}) not reproduced"
    assert len(got) == len(_words_with_baseline(sample))
    # the signature image sits where the sample has it
    sample_img, img = sample.get_image_info()[0]["bbox"], page.get_image_info()[0]["bbox"]
    assert all(abs(a - b) <= 0.6 for a, b in zip(sample_img, img, strict=True))
    # the table's outer box matches
    def outer(p):
        rects = [d["rect"] for d in p.get_drawings()]
        return (
            min(r.x0 for r in rects),
            min(r.y0 for r in rects),
            max(r.x1 for r in rects),
            max(r.y1 for r in rects),
        )

    assert all(abs(a - b) <= 1.0 for a, b in zip(outer(sample), outer(page), strict=True))


def test_plain_variant_matches_sample(samples, sushant_entry) -> None:
    data = build_bill_data(sushant_entry, include_client=False)
    pdf = bill.render(data, include_client=False, bill_cfg=CFG)
    _assert_matches_sample(samples / "spectrum" / "SUSHANT_SAWANT-bill_14-10-2026.pdf", pdf)


def test_with_client_variant_matches_sample(samples, ruju_entry) -> None:
    data = build_bill_data(ruju_entry, include_client=True)
    pdf = bill.render(data, include_client=True, bill_cfg=CFG)
    _assert_matches_sample(samples / "spectrum" / "RUJU_PRASANNAJI-bill_08-11-2024.pdf", pdf)


async def test_spectrum_render_applies_bill_only_discount(bg_cfg, ruju_entry) -> None:
    original_discount = ruju_entry["discount_amount"]
    pdf, _ = await service.spectrum_pdf(
        bg_cfg.partners["spectrum"],
        ruju_entry,
        include_client=True,
        discount_amount=Decimal("100"),
    )
    text = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
    assert "Discount Given" in text and "Net Total Amount" in text
    assert "100" in text and "One Thousand One Hundred Only" in text
    assert ruju_entry["discount_amount"] == original_discount


def test_fonts_are_embedded_open_fonts(sushant_entry, ruju_entry) -> None:
    plain = pymupdf.open(
        stream=bill.render(build_bill_data(sushant_entry, include_client=False), include_client=False, bill_cfg=CFG),
        filetype="pdf",
    )[0]
    client = pymupdf.open(
        stream=bill.render(build_bill_data(ruju_entry, include_client=True), include_client=True, bill_cfg=CFG),
        filetype="pdf",
    )[0]
    assert any("LiberationSans" in f[3] for f in plain.get_fonts())
    assert any("Carlito" in f[3] for f in client.get_fonts())


def test_client_row_does_not_exist_when_off(ruju_entry) -> None:
    data = build_bill_data(ruju_entry, include_client=False)
    page = pymupdf.open(stream=bill.render(data, include_client=False, bill_cfg=CFG), filetype="pdf")[0]
    text = page.get_text()
    assert "Client" not in text
    assert "CHANDRAPRABH" not in text
    assert "Discount Given" in text  # the discount block is independent of the client row


def test_discount_block_only_when_discount(sushant_entry, ruju_entry) -> None:
    plain = pymupdf.open(
        stream=bill.render(build_bill_data(sushant_entry, include_client=False), include_client=False, bill_cfg=CFG),
        filetype="pdf",
    )[0].get_text()
    assert "Discount" not in plain and "Net Total" not in plain
    sushant_entry["discount_amount"] = 50
    with_discount = pymupdf.open(
        stream=bill.render(build_bill_data(sushant_entry, include_client=False), include_client=False, bill_cfg=CFG),
        filetype="pdf",
    )[0].get_text()
    assert "Discount Given" in with_discount and "Three Hundred Only" in with_discount


def test_lab_cost_and_margin_never_printed(sushant_entry) -> None:
    sushant_entry.update(lab_cost=100, margin=250)
    page = pymupdf.open(
        stream=bill.render(build_bill_data(sushant_entry, include_client=False), include_client=False, bill_cfg=CFG),
        filetype="pdf",
    )[0]
    text = page.get_text().lower()
    assert "cost" not in text and "margin" not in text


def test_too_many_tests_is_refused(sushant_entry) -> None:
    from decimal import Decimal

    sushant_entry["tests"] = [f"Test number {i}" for i in range(60)]
    sushant_entry["test_amounts"] = [Decimal(10)] * 60
    with pytest.raises(BillError) as exc:
        bill.render(build_bill_data(sushant_entry, include_client=False), include_client=False, bill_cfg=CFG)
    assert exc.value.code == "too_many_tests"


def test_variant_must_be_enabled(sushant_entry) -> None:
    only_plain = CFG.model_copy(update={"variants": ["plain"]})
    with pytest.raises(BillError):
        bill.render(
            build_bill_data(sushant_entry, include_client=True | False), include_client=True, bill_cfg=only_plain
        )