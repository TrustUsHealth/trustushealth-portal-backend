"""Metropolis bill: read the lab's own PDF and change only the permitted cells.

Measured on 177934169.pdf (Apache FOP, A4 595.3 x 841.9): text is Helvetica 8 with the digits drawn in an embedded
ArialMT subset; amounts in the tests table and the summary are right aligned (edges 469.73 / 533.13), the payment
amount is centred in its cell, the "(INR) ... ONLY." line is left aligned at x 156.36. New text is inserted in
Helvetica at the original size, colour and baseline. The renderer then verifies the result (see `_verify`).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pymupdf

from app.core.config import BillCfg
from app.domain import money
from app.domain.bills.errors import BillError
from app.domain.bills.words import amount_words
from app.domain.parsing import standardize as std

AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")
DATE_FORMATS = ["%d/%m/%Y %I:%M %p"]
TOL = 0.05
FONT = "helv"

DIFF_LEVEL = 64  # a pixel counts as changed only if a colour channel moves by more than this (0-255)
DPI = 100

Rect4 = tuple[float, float, float, float]
_ROW_KEY = re.compile(r"^row_\d+$")
_CELL_NAME = {
    "gross": "gross_total",
    "grand": "grand_total",
    "received": "received_amount",
    "payment": "payment_amount",
    "words": "amount_received_in_words",
}


def _unreadable(detail: str = "") -> BillError:
    more = f" ({detail})" if detail else ""
    return BillError(422, "unreadable_bill", f"This does not look like a Metropolis bill PDF{more}.")


@dataclass(frozen=True)
class Cell:
    rect: Rect4  # the text being replaced (union of its words)
    baseline: float
    size: float
    color: tuple[float, ...]
    words: tuple[tuple[float, float, float, float, str], ...]  # original words, rounded, for verification


@dataclass(frozen=True)
class LabRow:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class LabBill:
    bill_no: str
    patient_no: str
    bill_date: date
    patient_name: str
    age: int | None
    age_unit: str | None
    gender: str | None
    referred_by: str | None
    rows: tuple[LabRow, ...]
    gross: Decimal
    general_charges: Decimal
    tax: Decimal
    discount: Decimal
    grand: Decimal
    received: Decimal
    due: Decimal
    payment: Decimal
    words_text: str
    cells: dict[str, Cell]


# ───────────────────────────── reading ─────────────────────────────
def _rw(w: tuple) -> tuple[float, float, float, float, str]:
    return (round(w[0], 2), round(w[1], 2), round(w[2], 2), round(w[3], 2), w[4])


def _lines(words: list[tuple]) -> list[dict[str, Any]]:
    ordered = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[dict[str, Any]] = []
    for w in ordered:
        cy = (w[1] + w[3]) / 2
        if lines and abs(lines[-1]["cy"] - cy) < 2.5:
            lines[-1]["w"].append(w)
        else:
            lines.append({"cy": cy, "w": [w]})
    for ln in lines:
        ln["w"].sort(key=lambda w: w[0])
    return lines


def _split_kv(line_words: list[tuple], split: float) -> list[tuple[str, list[tuple]]]:
    """'Label : value' in the left and right column of one line -> [(label, value words)]."""
    out: list[tuple[str, list[tuple]]] = []
    for left in (True, False):
        ws = [w for w in line_words if (w[0] < split) == left]
        texts = [w[4] for w in ws]
        if ":" not in texts:
            continue
        i = texts.index(":")
        label = " ".join(texts[:i]).strip().casefold()
        if label:
            out.append((label, ws[i + 1 :]))
    return out


def _cluster(values: list[float], tol: float = 1.0) -> list[float]:
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _amount_word(value_words: list[tuple]) -> tuple | None:
    return next((w for w in value_words if AMOUNT_RE.match(w[4])), None)


def _cell(spans: list[dict], words: list[tuple]) -> Cell:
    x0, y0 = min(w[0] for w in words), min(w[1] for w in words)
    x1, y1 = max(w[2] for w in words), max(w[3] for w in words)
    first = words[0]
    cx, cy = (first[0] + first[2]) / 2, (first[1] + first[3]) / 2
    span = next(
        (s for s in spans if s["bbox"][0] <= cx <= s["bbox"][2] and s["bbox"][1] <= cy <= s["bbox"][3]), None
    )
    if span is None:
        raise _unreadable("text style not found")
    return Cell(
        rect=(x0, y0, x1, y1),
        baseline=float(span["origin"][1]),
        size=float(span["size"]),
        color=tuple(pymupdf.sRGB_to_pdf(span["color"])),
        words=tuple(_rw(w) for w in words),
    )


def read_bill(data: bytes) -> LabBill:
    """Read the lab's bill PDF in memory. CPU-bound: call through `asyncio.to_thread`."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        raise _unreadable("the file cannot be opened") from None
    with doc:
        if doc.page_count != 1:
            raise _unreadable("expected a single page")
        page = doc[0]
        words = page.get_text("words")
        lines = _lines(words)
        spans = [
            s for b in page.get_text("dict")["blocks"] if b["type"] == 0 for ln in b["lines"] for s in ln["spans"]
        ]
        drawings = page.get_drawings()
        split = page.rect.width / 2 - 5

        header_idx = next((i for i, ln in enumerate(lines) if {"Tests", "Type", "Status"} <= {w[4] for w in ln["w"]}), None)
        if header_idx is None:
            raise _unreadable("tests table not found")
        header_cy = lines[header_idx]["cy"]

        kv: dict[str, str] = {}
        for ln in lines[:header_idx]:
            for label, vws in _split_kv(ln["w"], split):
                kv[label] = " ".join(w[4] for w in vws).strip()
        for key in ("name", "bill no", "patient no", "bill date", "age/gender"):
            if not kv.get(key):
                raise _unreadable(f"'{key}' not found")

        # test rows: cell boundaries come from the table's own rules
        xs = _cluster([dr["rect"].x0 for dr in drawings if dr["rect"].width < 1 and dr["rect"].y0 - 1 <= header_cy <= dr["rect"].y1 + 1])
        if len(xs) < 7:
            raise _unreadable("tests table columns not found")
        ys = _cluster(
            [
                dr["rect"].y0
                for dr in drawings
                if dr["rect"].height < 1
                and dr["rect"].x0 <= xs[0] + 2
                and dr["rect"].x1 >= xs[1] - 2
                and dr["rect"].y0 >= header_cy
            ]
        )
        rows: list[LabRow] = []
        cells: dict[str, Cell] = {}
        for top, bottom in zip(ys, ys[1:], strict=False):
            band = [w for w in words if top < (w[1] + w[3]) / 2 < bottom]
            name_words = sorted((w for w in band if (w[0] + w[2]) / 2 < xs[1]), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
            amount = next(
                (w for w in band if xs[4] <= (w[0] + w[2]) / 2 < xs[5] and AMOUNT_RE.match(w[4])),
                None,
            )
            name = std.clean_text(" ".join(w[4] for w in name_words))
            if amount is None or not name:
                continue
            cells[f"row_{len(rows)}"] = _cell(spans, [amount])
            rows.append(LabRow(name=name, amount=money.to_decimal(amount[4])))
        if not rows:
            raise _unreadable("no test rows")
        table_bottom = ys[-1]

        # summary block and the "received in words" line
        summary: dict[str, list[tuple]] = {}
        for ln in lines:
            if ln["cy"] > table_bottom:
                for label, vws in _split_kv(ln["w"], split):
                    summary.setdefault(label, vws)

        def amount_of(label: str) -> tuple:
            word = _amount_word(summary.get(label, []))
            if word is None:
                raise _unreadable(f"'{label}' not found")
            return word

        gross_w, grand_w, received_w = amount_of("gross total"), amount_of("grand total"), amount_of("received amount")
        discount_w = amount_of("discount")
        cells.update(
            gross=_cell(spans, [gross_w]),
            discount=_cell(spans, [discount_w]),
            grand=_cell(spans, [grand_w]),
            received=_cell(spans, [received_w]),
        )
        words_ws = summary.get("amount received in words") or []
        if not words_ws or words_ws[0][4] != "(INR)":
            raise _unreadable("'amount received in words' not found")
        cells["words"] = _cell(spans, words_ws)

        pay_idx = next((i for i, ln in enumerate(lines) if {"Payment", "Mode"} <= {w[4] for w in ln["w"]}), None)
        if pay_idx is None:
            raise _unreadable("payment table not found")
        pay_cy = lines[pay_idx]["cy"]
        pxs = _cluster([dr["rect"].x0 for dr in drawings if dr["rect"].width < 1 and dr["rect"].y0 - 1 <= pay_cy <= dr["rect"].y1 + 1])
        if len(pxs) < 3:
            raise _unreadable("payment table columns not found")
        payment_w = next(
            (
                w
                for ln in lines[pay_idx + 1 :]
                for w in ln["w"]
                if pxs[1] <= (w[0] + w[2]) / 2 < pxs[2] and AMOUNT_RE.match(w[4])
            ),
            None,
        )
        if payment_w is None:
            raise _unreadable("payment amount not found")
        cells["payment"] = _cell(spans, [payment_w])

        parsed_age, parsed_unit, parsed_gender, _, _ = std.parse_age_gender(kv["age/gender"])
        bill_date = std.parse_date(kv["bill date"], DATE_FORMATS)
        name = std.standardize_name(kv["name"])
        if bill_date is None or not name:
            raise _unreadable("name or bill date not readable")

        return LabBill(
            bill_no=kv["bill no"],
            patient_no=kv["patient no"],
            bill_date=bill_date,
            patient_name=name,
            age=parsed_age,
            age_unit=parsed_unit,
            gender=parsed_gender,
            referred_by=std.standardize_doctor(kv.get("ref doctor")),
            rows=tuple(rows),
            gross=money.to_decimal(gross_w[4]),
            general_charges=money.to_decimal((_amount_word(summary.get("general charges", [])) or (0, 0, 0, 0, "0.00"))[4]),
            tax=money.to_decimal((_amount_word(summary.get("tax", [])) or (0, 0, 0, 0, "0.00"))[4]),
            discount=money.to_decimal(discount_w[4]),
            grand=money.to_decimal(grand_w[4]),
            received=money.to_decimal(received_w[4]),
            due=money.to_decimal((_amount_word(summary.get("due amount", [])) or (0, 0, 0, 0, "0.00"))[4]),
            payment=money.to_decimal(payment_w[4]),
            words_text=" ".join(w[4] for w in words_ws),
            cells=cells,
        )


# ───────────────────────────── editing ─────────────────────────────
def _place(cell: Cell, text: str, align: str) -> tuple[float, Rect4]:
    """x for the new text and the area it occupies (used to ignore it when checking nothing else moved)."""
    width = pymupdf.get_text_length(text, fontname=FONT, fontsize=cell.size)
    x0, _, x1, _ = cell.rect
    if align == "right":
        x = x1 - width
    elif align == "center":
        x = (x0 + x1) / 2 - width / 2
    else:
        x = x0
    area = (min(x, x0), cell.baseline - cell.size * 1.1, max(x + width, x1), cell.baseline + cell.size * 0.4)
    return x, area


def _align(key: str) -> str:
    return "left" if key == "words" else "center" if key == "payment" else "right"


def edit_bill(data: bytes, lab: LabBill, *, amounts: list[Decimal], discount: Decimal, bill_cfg: BillCfg) -> bytes:
    """Change only the permitted cells, verify, return the new PDF. CPU-bound: call through `asyncio.to_thread`."""
    if len(amounts) != len(lab.rows):
        raise BillError(422, "amount_count", "Enter one amount for every test row of the lab bill.")
    if lab.general_charges != 0 or lab.tax != 0:
        raise BillError(422, "unsupported_bill", "This bill has general charges or tax, which cannot be edited here.")
    if lab.due != 0:
        raise BillError(422, "due_not_zero", "This bill has an amount due, so it cannot be regenerated.")
    gross = money.total(amounts)
    discount = money.to_decimal(discount)
    grand = gross - discount
    if grand < 0:
        raise BillError(422, "discount_too_large", "The discount cannot be more than the total.")
    template = bill_cfg.words_template or "(INR) {words} ONLY."
    words_text = template.format(words=amount_words(grand).upper())

    plan: list[tuple[str, str]] = []
    for i, (row, amount) in enumerate(zip(lab.rows, amounts, strict=True)):
        if amount != row.amount:
            plan.append((f"row_{i}", money.fixed(amount)))
    for key, old, new in (
        ("gross", lab.gross, gross),
        ("discount", lab.discount, discount),
        ("grand", lab.grand, grand),
        ("received", lab.received, grand),
        ("payment", lab.payment, grand),
    ):
        if old != new:
            plan.append((key, money.fixed(new)))
    if words_text != lab.words_text:
        plan.append(("words", words_text))
    if not plan:
        return data  # nothing differs from the lab's own bill

    editable = set(bill_cfg.editable_cells)
    for key, _ in plan:
        needed = "test_amount" if _ROW_KEY.match(key) else _CELL_NAME.get(key)
        if needed is not None and needed not in editable:
            raise BillError(500, "config", f"The cell '{needed}' is not listed in editable_cells.")

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        page = doc[0]
        for key, _ in plan:
            x0, y0, x1, y1 = lab.cells[key].rect
            page.add_redact_annot(pymupdf.Rect(x0 + 0.2, y0 + 1.2, x1 - 0.2, y1 - 1.2), fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        changes: list[tuple[Cell, str, Rect4]] = []
        for key, text in plan:
            cell = lab.cells[key]
            x, area = _place(cell, text, _align(key))
            page.insert_text(pymupdf.Point(x, cell.baseline), text, fontname=FONT, fontsize=cell.size, color=cell.color)
            changes.append((cell, text, area))
        out = doc.tobytes(garbage=1, deflate=True)
    finally:
        doc.close()
    _verify(data, out, changes)
    return out


# ───────────────────────────── verification ─────────────────────────────
def _fail(why: str) -> BillError:
    return BillError(500, "verification_failed", f"The bill could not be edited safely, so no PDF was produced ({why}).")


def _take(pool: list[tuple], item: tuple) -> bool:
    """Remove from `pool` one entry equal to `item` within TOL (and with the same text for words)."""
    for i, other in enumerate(pool):
        if len(other) == len(item) and all(abs(other[k] - item[k]) <= TOL for k in range(4)) and other[4:] == item[4:]:
            pool.pop(i)
            return True
    return False


def _boxes(page: pymupdf.Page) -> tuple[list[tuple], list[tuple]]:
    drawn = [tuple(d["rect"]) for d in page.get_drawings()]
    images = [tuple(i["bbox"]) for i in page.get_image_info()]
    return drawn, images


def _raster(page: pymupdf.Page, zones: list[Rect4]) -> tuple[int, int, int, bytes]:
    """100 dpi render with the given zones painted white: (width, height, channels, samples)."""
    mat = pymupdf.Matrix(DPI / 72, DPI / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    for zone in zones:
        z = pymupdf.Rect(zone[0] - 3, zone[1] - 3, zone[2] + 3, zone[3] + 3)
        z.intersect(page.rect)
        if not z.is_empty:
            pix.set_rect((z * mat).irect, (255, 255, 255))
    return pix.width, pix.height, pix.n, bytes(pix.samples)


def _raster_diff(a: tuple[int, int, int, bytes], b: tuple[int, int, int, bytes]) -> tuple[int, Rect4 | None]:
    """Count pixels that really differ (> DIFF_LEVEL on any channel) and where, in points.

    MuPDF rewrites the content stream when redacting, which can move untouched glyphs by hundredths of a point.
    That changes anti-aliased edge pixels slightly, so exact byte equality is too strict. A deleted glyph,
    moved line or shifted image changes pixels by far more than DIFF_LEVEL.
    Returns (-1, None) when the render sizes differ.
    """
    wa, ha, na, sa = a
    wb, hb, nb, sb = b
    if (wa, ha, na) != (wb, hb, nb):
        return -1, None
    stride = wa * na
    count = 0
    x0, y0, x1, y1 = wa, ha, -1, -1
    for row in range(ha):
        ra = sa[row * stride : (row + 1) * stride]
        rb = sb[row * stride : (row + 1) * stride]
        if ra == rb:
            continue
        for i, (p, q) in enumerate(zip(ra, rb, strict=True)):
            if abs(p - q) > DIFF_LEVEL:
                px = i // na
                count += 1
                x0, x1 = min(x0, px), max(x1, px)
                y0, y1 = min(y0, row), max(y1, row)
    if not count:
        return 0, None
    s = 72 / DPI
    return count, (x0 * s, y0 * s, (x1 + 1) * s, (y1 + 1) * s)


def _verify(original: bytes, edited: bytes, changes: list[tuple[Cell, str, Rect4]]) -> None:
    """Same page count; only the intended words differ; rules and images untouched; nothing else moved."""
    a = pymupdf.open(stream=original, filetype="pdf")
    b = pymupdf.open(stream=edited, filetype="pdf")
    try:
        if a.page_count != b.page_count:
            raise _fail("page count changed")
        pa, pb = a[0], b[0]

        rest = [_rw(w) for w in pa.get_text("words")]
        for cell, _, _ in changes:
            for w in cell.words:
                if w not in rest:
                    raise _fail("a target word was not found")
                rest.remove(w)
        pool = [_rw(w) for w in pb.get_text("words")]
        for w in rest:
            if not _take(pool, w):
                raise _fail("other text changed or moved")
        expected = Counter(t for _, text, _ in changes for t in text.split())
        if Counter(w[4] for w in pool) != expected:
            raise _fail("the new text differs from what was intended")

        drawn_a, images_a = _boxes(pa)
        drawn_b, images_b = _boxes(pb)
        if len(drawn_a) != len(drawn_b) or len(images_a) != len(images_b):
            raise _fail("lines or images changed")
        pool_d = list(drawn_b)
        if not all(_take(pool_d, d) for d in drawn_a):
            raise _fail("lines moved")
        pool_i = list(images_b)
        if not all(_take(pool_i, i) for i in images_a):
            raise _fail("images moved")

        zones = [area for _, _, area in changes] + [cell.rect for cell, _, _ in changes]
        changed, box = _raster_diff(_raster(pa, zones), _raster(pb, zones))
        if changed == -1:
            raise _fail("the page size changed")
        if changed:
            x0, y0, x1, y1 = box  # type: ignore[misc]
            raise _fail(
                f"the page differs outside the edited cells: {changed} pixels between "
                f"x {x0:.0f}-{x1:.0f} pt and y {y0:.0f}-{y1:.0f} pt"
            )
    finally:
        a.close()
        b.close()