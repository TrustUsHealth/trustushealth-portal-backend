"""Spectrum bill: generated with ReportLab from two fixed layouts.

Every number below was MEASURED on the sample bills (points, origin top-left, y grows downwards):
- plain       <- SUSHANT_SAWANT-bill_14-10-2026.pdf: A4 595.28 x 841.89, Helvetica 11 (embedded as Liberation Sans),
                 header ink #141414, table 72.0 / 297.64 / 523.28, top 213.7, rows 18.32 with a 0.85 border box per cell,
                 signature 127.56 x 112.45 at x 411.03, 56.7 below the last table.
- with_client <- RUJU_PRASANNAJI-bill_08-11-2024.pdf: Letter 612 x 792, Calibri 12 (embedded as Carlito),
                 table 72.03 / 303.38 / 540.23, top 238.83, rows 16.5 / 16.25 / 16.25 with thin rules,
                 signature 100.5 x 90 at x 439.5, 71.7 below the last table.
The discount table position for the plain variant is the with_client gap (the plain sample has no discount).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.core.config import BillCfg
from app.domain.bills.errors import BillError
from app.domain.bills.registry import APP_DIR, asset_path
from app.domain.bills.types import BillData

_FONT_DIR = APP_DIR / "assets" / "fonts"
_FONT_FILES = {
    "BillSans": "LiberationSans-Regular.ttf",  # metric-compatible with Helvetica / Arial
    "BillSans-Bold": "LiberationSans-Bold.ttf",
    "BillCalibri": "Carlito-Regular.ttf",  # metric-compatible with Calibri
    "BillCalibri-Bold": "Carlito-Bold.ttf",
}
_registered = False


def _register_fonts() -> None:
    global _registered
    if _registered:
        return
    for name, file in _FONT_FILES.items():
        pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / file)))
    _registered = True


@dataclass(frozen=True)
class Layout:
    page: tuple[float, float]
    font: str
    bold: str
    size: float
    header_rgb: tuple[float, float, float]
    title_upper: bool
    x0: float
    xm: float
    x1: float
    top: float
    pad: float
    style: str  # "cells" (a box per cell) | "lines" (thin rules)
    row_h: tuple[float, ...]
    block_row_h: tuple[float, ...]
    first_off: float  # baseline below the top of the first row of a table
    row_off: float  # baseline below the top of the other rows
    lead: float  # extra line spacing when a cell wraps
    block_gap: float
    border: float
    sig_gap: float
    sig_x: float
    sig_w: float
    sig_h: float
    sig_text_x: float
    sig_text_off: float  # baseline of "Authorised Signatory" below the top of the signature


PLAIN = Layout(
    page=(595.28, 841.89),
    font="BillSans",
    bold="BillSans-Bold",
    size=11.0,
    header_rgb=(20 / 255, 20 / 255, 20 / 255),
    title_upper=True,
    x0=72.0,
    xm=297.64,
    x1=523.28,
    top=213.7,
    pad=2.83,
    style="cells",
    row_h=(18.32,),
    block_row_h=(18.32,),
    first_off=12.18,
    row_off=12.18,
    lead=13.2,
    block_gap=24.52,
    border=0.85,
    sig_gap=56.7,
    sig_x=411.03,
    sig_w=127.56,
    sig_h=112.45,
    sig_text_x=425.2,
    sig_text_off=141.73,
)

WITH_CLIENT = Layout(
    page=(612.0, 792.0),
    font="BillCalibri",
    bold="BillCalibri-Bold",
    size=12.0,
    header_rgb=(0.0, 0.0, 0.0),
    title_upper=False,
    x0=72.4,
    xm=303.63,
    x1=539.85,
    top=239.2,
    pad=4.6,
    style="lines",
    row_h=(16.5, 16.25, 16.25),
    block_row_h=(16.5, 16.5, 16.25),
    first_off=11.88,
    row_off=11.75,
    lead=14.6,
    block_gap=24.52,
    border=0.5,
    sig_gap=71.69,
    sig_x=439.5,
    sig_w=100.5,
    sig_h=90.0,
    sig_text_x=437.67,
    sig_text_off=110.79,
)

Cell = tuple[str, float, float, bool]  # text, x, baseline (from top), bold


def _patient(data: BillData, lay: Layout) -> str:
    title = data.patient_title.upper() if lay.title_upper else data.patient_title
    return f"{title} {data.patient_name}" if title else data.patient_name


def _header_cells(data: BillData, lay: Layout, include_client: bool) -> list[Cell]:
    patient = _patient(data, lay)
    if not include_client:
        y = (128.41, 152.39, 176.38)
        return [
            ("Name", 77.67, y[0], False),
            (f":  {patient}", 158.9, y[0], True),
            ("Age/Gender", 77.67, y[1], False),
            (f":  {data.age_gender}", 158.9, y[1], True),
            ("Referred by", 77.67, y[2], False),
            (f":  {data.referred_by}", 158.9, y[2], True),
            ("Registration No.", 348.44, y[0], False),
            (f":  {data.registration_no}", 461.26, y[0], True),
            ("Bill No.", 348.44, y[1], False),
            (f":  {data.bill_no}", 461.26, y[1], True),
            ("Registration Date", 348.44, y[2], False),
            (f":  {data.registration_date}", 461.26, y[2], True),
        ]
    y = (131.05, 155.05, 178.8, 202.58)
    return [
        ("Client Name", 72.03, y[0], False),
        (":", 137.48, y[0], False),
        (data.client_name or "", 143.55, y[0], True),
        ("Patient Name", 72.03, y[1], False),
        (":", 138.79, y[1], False),
        (patient, 144.8, y[1], True),
        ("Age/Gender", 72.03, y[2], False),
        (":", 139.8, y[2], False),
        (data.age_gender, 145.76, y[2], True),
        ("Referred by", 72.03, y[3], False),
        (":", 140.17, y[3], False),
        (data.referred_by, 146.26, y[3], True),
        ("Registration Date", 379.65, y[0], False),
        (":", 467.07, y[0], False),
        (data.registration_date, 473.2, y[0], True),
        ("Registration No", 380.09, y[1], False),
        (":", 466.54, y[1], False),
        (data.registration_no, 472.7, y[1], True),
        ("Bill No.", 381.4, y[2], False),
        (":", 470.48, y[2], False),
        (data.bill_no, 476.45, y[2], True),
    ]


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    return simpleSplit(text, font, size, width) or [""]


def _draw_table(
    c: Canvas, lay: Layout, top: float, rows: list[tuple[str, str, bool]], heights: tuple[float, ...]
) -> float:
    """Draws a two-column table; returns the y (from top) of its bottom edge."""
    page_h = lay.page[1]
    left_w = lay.xm - lay.x0 - 2 * lay.pad
    right_w = lay.x1 - lay.xm - 2 * lay.pad
    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    ys = [top]
    y = top
    for i, (left, right, bold) in enumerate(rows):
        font = lay.bold if bold else lay.font
        left_lines = _wrap(left, font, lay.size, left_w)
        right_lines = _wrap(right, font, lay.size, right_w)
        lines = max(len(left_lines), len(right_lines))
        height = heights[i % len(heights)] + (lines - 1) * lay.lead
        offset = lay.first_off if i == 0 else lay.row_off
        c.setFont(font, lay.size)
        for k, text in enumerate(left_lines):
            c.drawString(lay.x0 + lay.pad, page_h - (y + offset + k * lay.lead), text)
        for k, text in enumerate(right_lines):
            c.drawString(lay.xm + lay.pad, page_h - (y + offset + k * lay.lead), text)
        y += height
        ys.append(y)

    if lay.style == "cells":
        c.setLineWidth(lay.border)
        for i in range(len(rows)):
            h = ys[i + 1] - ys[i]
            c.rect(lay.x0, page_h - ys[i + 1], lay.xm - lay.x0, h, stroke=1, fill=0)
            c.rect(lay.xm, page_h - ys[i + 1], lay.x1 - lay.xm, h, stroke=1, fill=0)
    else:
        for idx, yy in enumerate(ys):
            c.setLineWidth(0.75 if idx in (0, len(ys) - 1) else lay.border)
            c.line(lay.x0, page_h - yy, lay.x1, page_h - yy)
        for xx, width in ((lay.x0, 0.75), (lay.xm, lay.border), (lay.x1, 0.75)):
            c.setLineWidth(width)
            c.line(xx, page_h - ys[0], xx, page_h - ys[-1])
    return y


def render(data: BillData, *, include_client: bool, bill_cfg: BillCfg) -> bytes:
    """Render one Spectrum bill. CPU-bound: call through `asyncio.to_thread`."""
    variant = "with_client" if include_client else "plain"
    if variant not in bill_cfg.variants:
        raise BillError(422, "variant_unavailable", "This bill layout is not enabled for the lab.")
    if include_client and not data.client_name:
        raise BillError(422, "client_name_required", "Enter the client name, or switch off 'Include client name'.")
    lay = WITH_CLIENT if include_client else PLAIN
    _register_fonts()
    page_h = lay.page[1]

    buffer = io.BytesIO()
    c = Canvas(buffer, pagesize=lay.page, pageCompression=1, invariant=1)
    c.setTitle("Bill")

    c.setFillColorRGB(*lay.header_rgb)
    for text, x, y, bold in _header_cells(data, lay, include_client):
        c.setFont(lay.bold if bold else lay.font, lay.size)
        c.drawString(x, page_h - y, text)

    rows = (
        [("TESTS", "AMOUNT", True)]
        + [(r.name, r.amount, False) for r in data.rows]
        + [("Total", data.total, True), ("Total (In Words)", data.total_words, True)]
    )
    bottom = _draw_table(c, lay, lay.top, rows, lay.row_h)
    if data.discount_block is not None:
        block = data.discount_block
        block_rows = [
            ("Discount Given", block.discount, True),
            ("Net Total Amount", block.net, True),
            ("Net Total Amount (In Words)", block.net_words, True),
        ]
        bottom = _draw_table(c, lay, bottom + lay.block_gap, block_rows, lay.block_row_h)

    sig_top = bottom + lay.sig_gap
    text_y = sig_top + lay.sig_text_off
    if text_y + 36 > page_h:
        raise BillError(422, "too_many_tests", "There are too many tests to fit on one bill page.")
    signature = asset_path(bill_cfg.signature_asset or "assets/bill/signature.png")
    c.drawImage(str(signature), lay.sig_x, page_h - (sig_top + lay.sig_h), width=lay.sig_w, height=lay.sig_h)
    c.setFillColorRGB(0, 0, 0)
    c.setFont(lay.font, lay.size)
    c.drawString(lay.sig_text_x, page_h - text_y, "Authorised Signatory")
    c.showPage()
    c.save()
    return buffer.getvalue()