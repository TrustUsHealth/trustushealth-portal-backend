"""PDF Business Report: A4, 15 mm margins, branded header with embedded logo, vector charts, generated on request
from the same payloads the screens use. Synchronous: callers run it in a thread."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.modules.insights import charts as C
from app.modules.insights.fmt import RUPEE, compact_inr, hours_text, inr

FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
BRAND_LOGO = Path(__file__).resolve().parents[2] / "core" / "assets" / "brand-logo.png"
SLATE = HexColor("#34495E")
BRAND = HexColor("#E0242A")
INK = HexColor("#292C2D")
BODY = HexColor("#3A3D3E")
MUTED = HexColor("#525656")
BORDER = HexColor("#D9D5CE")
IVORY = HexColor("#FAF7F0")
SECTION_TITLES = {"overview": "Summary", "finance": "Finance", "labs": "Labs", "patients": "Patients"}
SECTION_ORDER = ("overview", "finance", "labs", "patients")
WIDTH = A4[0] - 30 * mm


class Fonts:
    def __init__(self, regular: str, bold: str, symbol: str):
        self.regular, self.bold, self.symbol = regular, bold, symbol


def register_fonts(font_dir: Path = FONT_DIR) -> Fonts:
    """Embed Noto Sans so the rupee sign prints. Without the font files the PDF falls back to Helvetica and writes
    `Rs ` instead of the sign, so it is always readable."""
    try:
        pdfmetrics.registerFont(TTFont("NotoSans", str(font_dir / "NotoSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("NotoSans-Bold", str(font_dir / "NotoSans-Bold.ttf")))
        return Fonts("NotoSans", "NotoSans-Bold", RUPEE)
    except (TTFError, OSError):
        return Fonts("Helvetica", "Helvetica-Bold", "Rs ")


class _Numbered(canvas.Canvas):
    footer = ""
    stamp = ""
    font = "Helvetica"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages: list[dict] = []

    def showPage(self):  # noqa: N802 - ReportLab API
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(self.font, 7.5)
            self.setFillColor(MUTED)
            self.drawString(15 * mm, 9 * mm, f"Page {self._pageNumber} of {total}   |   {self.stamp}")
            self.drawRightString(A4[0] - 15 * mm, 9 * mm, self.footer)
            super().showPage()
        super().save()


def _f(x: str | None) -> float:
    return float(x) if x is not None else 0.0


def build_pdf(data: dict[str, dict[str, Any]], meta: dict[str, Any], cfg: Any, fonts: Fonts | None = None) -> bytes:
    """data: section -> the same payload the screen endpoint returns. meta: period, labs, compare, generated, by."""
    fonts = fonts or register_fonts()
    sym = fonts.symbol
    money = lambda s, dp=0: inr(Decimal(s), dp, sym)  # noqa: E731
    axis = lambda v: compact_inr(Decimal(str(v)), sym)  # noqa: E731
    pal = cfg.chart_palette
    st = {
        "title": ParagraphStyle("t", fontName=fonts.bold, fontSize=17, leading=21, textColor=BRAND),
        "h1": ParagraphStyle("h1", fontName=fonts.bold, fontSize=14, leading=18, textColor=SLATE, spaceBefore=2, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName=fonts.bold, fontSize=10, leading=13, textColor=INK, spaceBefore=9, spaceAfter=4),
        "p": ParagraphStyle("p", fontName=fonts.regular, fontSize=9, textColor=BODY, leading=13),
        "small": ParagraphStyle("s", fontName=fonts.regular, fontSize=8, textColor=MUTED, leading=11),
        "meta": ParagraphStyle("meta", fontName=fonts.regular, fontSize=8.5, textColor=INK, leading=12),
        "meta_label": ParagraphStyle("meta-label", fontName=fonts.bold, fontSize=7, textColor=MUTED, leading=9),
    }

    def table(rows: list[list[str]], widths: list[float] | None = None, totals: bool = False, right_from: int = 1):
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [
            ("FONTNAME", (0, 0), (-1, -1), fonts.regular), ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (-1, -1), BODY), ("FONTNAME", (0, 0), (-1, 0), fonts.bold),
            ("TEXTCOLOR", (0, 0), (-1, 0), SLATE), ("BACKGROUND", (0, 0), (-1, 0), IVORY),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, SLATE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.35, BORDER), ("ALIGN", (right_from, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if totals:
            style += [("FONTNAME", (0, -1), (-1, -1), fonts.bold), ("LINEABOVE", (0, -1), (-1, -1), 0.8, SLATE)]
        t.setStyle(TableStyle(style))
        return t

    def kpi_table(kpis: list[dict]):
        cells = []
        for k in kpis:
            if k["value"] is None:
                v = "n/a"
            elif k["kind"] == "money":
                v = money(k["value"])
            else:
                v = f"{k['value']:.1f}%" if k["kind"] == "percent" else str(k["value"])
            if k["direction"] in ("up", "down") and k["delta_pct"] is not None:
                unit = " pts" if k["kind"] == "percent" else "%"
                chg = f"{'+' if k['direction'] == 'up' else '-'}{abs(k['delta_pct']):.1f}{unit}, {k['direction']}"
            elif k["direction"] == "new":
                chg = "New"
            elif k["direction"] == "flat":
                chg = "No change"
            else:
                chg = ""
            cells.append([Paragraph(k["label"], st["small"]), Paragraph(f"<b>{v}</b>", ParagraphStyle("k", parent=st["p"], fontSize=12, leading=15)),
                          Paragraph(f"{chg}<br/>{k['detail'] or ''}", st["small"])])
        w = WIDTH / len(cells)
        t = Table([cells], colWidths=[w] * len(cells))
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, BORDER), ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
                               ("BACKGROUND", (0, 0), (-1, -1), IVORY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                               ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
        return t

    def empty_or(sec: dict) -> list:
        return [Paragraph(sec["empty_message"], st["p"])] if sec["empty"] else []

    def chart_title(text: str):
        return Paragraph(text, st["h2"])

    def meta_cell(label: str, value: str):
        return [
            Paragraph(escape(label.upper()), st["meta_label"]),
            Paragraph(escape(value), st["meta"]),
        ]

    header_title = [
        Paragraph("BUSINESS REPORT", st["title"]),
        Paragraph("CONFIDENTIAL · INTERNAL USE", st["small"]),
    ]
    header = Table([[Image(str(BRAND_LOGO), width=42 * mm, height=17 * mm), header_title]],
                   colWidths=[50 * mm, WIDTH - 50 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, BRAND),
    ]))
    metadata = Table([
        [meta_cell("Period", str(meta["period"])), meta_cell("Labs", str(meta["labs"]))],
        [meta_cell("Compare", str(meta["compare"])), meta_cell("Generated", f"{meta['generated']} by {meta['by']}")],
    ], colWidths=[WIDTH / 2, WIDTH / 2])
    metadata.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), IVORY), ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, BORDER), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story: list = [header, Spacer(1, 8), metadata, Spacer(1, 8)]
    sections = [s for s in SECTION_ORDER if s in data]
    for i, name in enumerate(sections):
        sec = data[name]
        if i:
            story.append(PageBreak())
        story.append(Paragraph(SECTION_TITLES[name], st["h1"]))
        if name == "overview":
            story.append(kpi_table(sec["kpis"]))
            if sec["highlights"]:
                story += [chart_title("Highlights")] + [Paragraph("- " + h, st["p"]) for h in sec["highlights"]]
            story += empty_or(sec)
            if not sec["empty"]:
                p = sec["trend"]
                has_cmp = any(x["compare_billed_revenue"] is not None for x in p)
                story += [chart_title("Revenue and Margin"),
                          C.bar_line(WIDTH, 150, [x["label"] for x in p], [_f(x["billed_revenue"]) for x in p],
                                     [_f(x["margin"]) for x in p],
                                     [None if x["compare_billed_revenue"] is None else _f(x["compare_billed_revenue"]) for x in p] if has_cmp else None,
                                     pal.labs[0], pal.labs[2], pal.compare_line, axis, fonts.regular, "Billed Revenue", "Margin")]
        elif name == "finance":
            story += [kpi_table(sec["kpis"])] + empty_or(sec)
            if not sec["empty"]:
                s = sec["money_split"]
                series = [("Lab Cost", pal.labs[1], [_f(x["lab_cost"]) for x in s]), ("Margin", pal.labs[0], [max(_f(x["margin"]), 0) for x in s])]
                if any(_f(x["revenue_no_cost"]) for x in s):
                    series.append(("Cost not entered", pal.others, [_f(x["revenue_no_cost"]) for x in s]))
                story += [chart_title("Where the money goes"), C.stacked_bar(WIDTH, 140, [x["label"] for x in s], series, axis, fonts.regular)]
                if sec["margin_by_lab"]:
                    story += [chart_title(f"Margin % by Lab (marker at {sec['low_margin_threshold_pct']:g}%)"),
                              C.hbar(WIDTH, [(m["name"], m["margin_pct"], m["color"], f"{m['margin_pct']:.1f}%") for m in sec["margin_by_lab"]],
                                     axis, fonts.regular, marker=sec["low_margin_threshold_pct"], marker_label=f"{sec['low_margin_threshold_pct']:g}%")]
                d = sec["discounts"]
                story += [chart_title("Discounts"),
                          C.bar_line(WIDTH, 120, [x["label"] for x in d], [_f(x["discounts"]) for x in d],
                                     [x["discount_rate_pct"] or 0 for x in d], None, pal.labs[3], pal.labs[2], pal.compare_line,
                                     lambda v: f"{v:g}" if v < 100 else axis(v), fonts.regular, "Discounts Given", "Discount Rate %")]
                rows = [["Lab", "Billed Visits", "Billed Revenue", "Lab Cost", "Margin", "Margin %", "Discount Rate"]]
                pc = lambda v: "n/a" if v is None else f"{v:.1f}%"  # noqa: E731
                for r in [*sec["lab_table"]["rows"], {"name": "Total", **sec["lab_table"]["totals"]}]:
                    rows.append([r["name"], str(r["billed_visits"]), money(r["billed_revenue"], 2), money(r["lab_cost"], 2),
                                 money(r["margin"], 2), pc(r["margin_pct"]), pc(r["discount_rate_pct"])])
                st_ = sec["stats"]
                story += [chart_title("Lab-wise table"), table(rows, totals=True),
                          Spacer(1, 4), Paragraph(f"Avg Bill Value: {money(st_['avg_bill_value'], 2) if st_['avg_bill_value'] else 'n/a'}   |   "
                                                  f"Awaiting Billing: {st_['awaiting_billing']}   |   Below-Cost Bills: {st_['below_cost_bills']}", st["p"])]
        elif name == "labs":
            story += empty_or(sec)
            if not sec["empty"]:
                rows = [["Lab", "Visits", "Billed Revenue", "Margin %", "Median Turnaround"]]
                for c in sec["lab_cards"]:
                    rows.append([c["name"], str(c["visits"]), money(c["billed_revenue"], 2),
                                 "n/a" if c["margin_pct"] is None else f"{c['margin_pct']:.1f}%", c["median_turnaround_display"]])
                story += [chart_title("Lab summary"), table(rows)]
                v = sec["visits_by_lab"]
                story += [chart_title("Visits by Lab"),
                          C.stacked_bar(WIDTH, 130, [p["label"] for p in v["points"]],
                                        [(lab["name"], lab["color"], [float(p["counts"][lab["lab"]]) for p in v["points"]]) for lab in v["labs"]],
                                        lambda x: f"{x:g}", fonts.regular)]
                t = [(r["name"], r["median_hours"], r["slowest_10_hours"]) for r in sec["turnaround"] if r["sample"]]
                if t:
                    story += [chart_title("Report Turnaround by Lab (Median, then Slowest 10%)"),
                              C.hgrouped(WIDTH, t, (pal.labs[0], pal.labs[2]), hours_text, fonts.regular)]
        elif name == "patients":
            story += [kpi_table(sec["kpis"])] + empty_or(sec)
            if not sec["empty"]:
                n = sec["new_vs_returning"]
                story += [chart_title("New vs Returning"),
                          C.stacked_bar(WIDTH, 120, [x["label"] for x in n],
                                        [("New", pal.labs[0], [float(x["new"]) for x in n]), ("Returning", pal.labs[2], [float(x["returning"]) for x in n])],
                                        lambda x: f"{x:g}", fonts.regular)]
                g = [(b["band"], [(b["male"], pal.labs[0]), (b["female"], pal.labs[2]), (b["other"], pal.labs[3]), (b["not_recorded"], pal.others)])
                     for b in sec["age_gender"]]
                story += [chart_title("Age and Gender (Male, Female, Other, Not recorded)"), C.hstacked(WIDTH, g, fonts.regular)]
                tt = sec["top_tests"]
                rows = [(r["test"], float(r["visits"]), pal.labs[0], str(r["visits"])) for r in tt["rows"]]
                if tt["others"]:
                    rows.append(("Others", float(tt["others"]), pal.others, str(tt["others"])))
                if rows:
                    story += [chart_title("Top Tests (visits containing the test)"), C.hbar(WIDTH, rows, axis, fonts.regular)]
                tr = sec["top_referrers"]
                rows = [["Referrer", "Visits", "Billed Revenue", "Share"]]
                for r in [*tr["rows"], *([tr["others"]] if tr["others"] else [])]:
                    rows.append([r["referrer"], str(r["visits"]), money(r["billed_revenue"], 2), f"{r['share_pct']:.1f}%"])
                story += [chart_title("Top Referrers"), table(rows)]
    notes = [n for name in sections for n in data[name]["data_notes"]]
    story += [PageBreak(), Paragraph("Notes", st["h1"])]
    labs_sec = data.get("labs")
    if labs_sec and len(labs_sec["lab_cost_statement"]["months"]) > 1 and labs_sec["lab_cost_statement"]["rows"]:
        stmt = labs_sec["lab_cost_statement"]
        rows = [["Lab", *[m["label"] for m in stmt["months"]], "Total"]]
        rows += [[r["name"], *[money(r["cells"][m["key"]], 2) for m in stmt["months"]], money(r["total"], 2)] for r in stmt["rows"]]
        rows.append(["Total", *[money(stmt["totals"][m["key"]], 2) for m in stmt["months"]], money(stmt["grand_total"], 2)])
        story += [chart_title("Lab Cost Statement"), table(rows, totals=True)]
    story.append(chart_title("Data notes"))
    story += [Paragraph("- " + n, st["p"]) for n in dict.fromkeys(notes)] or [Paragraph("None.", st["p"])]
    story += [chart_title("How to read this report"),
              Paragraph("<b>Visit</b>: one patient entry (one person, one lab, one registration date). <b>Billed Visit</b>: a visit with a billed amount. "
                        "<b>Patient</b>: one person however many visits. <b>New / Returning Patient</b>: first visit inside, or before, the period. "
                        "<b>Billed Revenue</b>: what patients were billed (payments received are not tracked). <b>Discount Rate</b>: Discounts Given ÷ Gross Billed. "
                        "<b>Margin</b>: Billed Revenue − Lab Cost, using visits that have a lab cost. <b>Below-Cost Bill</b>: a bill with negative margin. "
                        "<b>Awaiting Billing</b>: visits registered but not yet billed. <b>Report Turnaround</b>: hours from collection to report, "
                        "Median (typical) and Slowest 10%.", st["p"])]

    out = io.BytesIO()
    stamp = f"Generated {meta['generated']}"
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                            bottomMargin=16 * mm, title="TrustUs Health Business Report", author="TrustUs Health")

    class Canvas(_Numbered):
        footer, font = cfg.report.footer, fonts.regular
    Canvas.stamp = stamp
    doc.build(story, canvasmaker=Canvas)
    return out.getvalue()


# ───────────────────────────── assembling a report from an analysis ─────────────────────────────
def render(a: Any, sections: list[str], by: str, tz_name: str, fonts: Fonts | None = None) -> bytes:
    """Build the PDF for an `Analysis` (the same one the screens use) and the chosen sections."""
    from datetime import datetime

    from app.modules.insights import compute as K

    builders = {"overview": K.build_overview, "finance": K.build_finance, "labs": K.build_labs, "patients": K.build_patients}
    data = {s: builders[s](a) for s in SECTION_ORDER if s in sections}
    names = [a.lab(k).name for k in a.labs_selected]
    when = datetime.fromisoformat(a.generated_at)
    meta = {
        "period": a.range.label(),
        "labs": ", ".join(names) if names else "All labs",
        "compare": {"previous": "Previous period", "last_year": "Same period last year", "none": "Off"}[a.compare_mode]
        + (f" ({a.compare.label()})" if a.compare else ""),
        "generated": f"{when:%d %b %Y, %H:%M} {'IST' if tz_name == 'Asia/Kolkata' else tz_name}",
        "by": by,
    }
    return build_pdf(data, meta, a.cfg, fonts)


def file_name(a: Any) -> str:
    return f"TrustUs_Business_Report_{a.range.start.isoformat()}_{a.range.end.isoformat()}.pdf"