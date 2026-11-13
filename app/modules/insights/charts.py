"""Vector chart helpers for the PDF report (ReportLab graphics, no images). One helper per chart type, so charts stay
sharp in print and the file stays small. Each returns a `Drawing`."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String, Wedge
from reportlab.lib.colors import HexColor

INK = HexColor("#3A3D3E")
MUTED = HexColor("#525656")
GRID = HexColor("#D9D5CE")


def nice_max(top: float, ticks: int = 4) -> tuple[float, float]:
    """Round the top of an axis up to a tidy number; returns (max, step)."""
    if top <= 0:
        return 1.0, 1.0 / ticks
    raw = top / ticks
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return step * ticks, step


def _fit(text: str, width: float, size: float) -> str:
    limit = max(int(width / (size * 0.52)), 3)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(d: Drawing, x: float, y: float, s: str, font: str, size: float, anchor: str = "start", color=INK) -> None:
    d.add(String(x, y, s, fontName=font, fontSize=size, fillColor=color, textAnchor=anchor))


def legend(d: Drawing, items: Sequence[tuple[str, str, str]], font: str, y: float, x: float = 0, size: float = 7) -> None:
    """items: (label, colour, kind) with kind box | dash."""
    for label, color, kind in items:
        if kind == "dash":
            d.add(Line(x, y + 3, x + 10, y + 3, strokeColor=HexColor(color), strokeWidth=1.2, strokeDashArray=[2, 2]))
        else:
            d.add(Rect(x, y, 7, 7, fillColor=HexColor(color), strokeColor=None))
        _text(d, x + 12, y, label, font, size)
        x += 18 + len(label) * size * 0.52


def _axes(d, x0, y0, w, h, top, step, fmt, font, size=6.5):
    n = int(round(top / step))
    for i in range(n + 1):
        y = y0 + h * i / n
        d.add(Line(x0, y, x0 + w, y, strokeColor=GRID, strokeWidth=0.4))
        _text(d, x0 - 3, y - 2, fmt(step * i), font, size, "end", MUTED)


def _xlabels(d, labels, x0, y0, slot, font, size=6.5):
    skip = max(1, math.ceil(len(labels) / 12))
    for i, lab in enumerate(labels):
        if i % skip == 0:
            _text(d, x0 + slot * (i + 0.5), y0 - 9, _fit(lab, slot * skip, size), font, size, "middle", MUTED)


def bar_line(w: float, h: float, labels: list[str], bars: list[float], line: list[float] | None,
             compare: list[float | None] | None, color: str, line_color: str, compare_color: str, fmt: Callable,
             font: str, bar_name: str, line_name: str | None) -> Drawing:
    d = Drawing(w, h)
    x0, y0, pw, ph = 42, 22, w - 50, h - 42
    vals = [*bars, *(line or []), *[c for c in (compare or []) if c is not None]]
    top, step = nice_max(max(vals + [0]))
    low = min(vals + [0])
    if low < 0:
        top, step = nice_max(max(top, -low))
    _axes(d, x0, y0, pw, ph, top, step, fmt, font)
    slot = pw / max(len(labels), 1)
    for i, v in enumerate(bars):
        bh = ph * max(v, 0) / top
        d.add(Rect(x0 + slot * i + slot * 0.18, y0, slot * 0.64, bh, fillColor=HexColor(color), strokeColor=None))
        if len(labels) <= 8 and v:
            _text(d, x0 + slot * (i + 0.5), y0 + bh + 2, fmt(v), font, 6, "middle")
    def poly(series, col, dash=None):
        pts = [(x0 + slot * (i + 0.5), y0 + ph * v / top) for i, v in enumerate(series) if v is not None]
        if len(pts) > 1:
            d.add(PolyLine([c for p in pts for c in p], strokeColor=HexColor(col), strokeWidth=1.4, strokeDashArray=dash))
        for px, py in pts:
            d.add(Circle(px, py, 1.6, fillColor=HexColor(col), strokeColor=None))
    if compare:
        poly(compare, compare_color, [3, 2])
    if line:
        poly(line, line_color)
    _xlabels(d, labels, x0, y0, slot, font)
    items = [(bar_name, color, "box")]
    if line and line_name:
        items.append((line_name, line_color, "box"))
    if compare:
        items.append(("Compare", compare_color, "dash"))
    legend(d, items, font, h - 8, x0)
    return d


def stacked_bar(w: float, h: float, labels: list[str], series: list[tuple[str, str, list[float]]], fmt: Callable,
                font: str) -> Drawing:
    d = Drawing(w, h)
    x0, y0, pw, ph = 42, 22, w - 50, h - 42
    totals = [sum(s[2][i] for s in series) for i in range(len(labels))]
    top, step = nice_max(max(totals + [0]))
    _axes(d, x0, y0, pw, ph, top, step, fmt, font)
    slot = pw / max(len(labels), 1)
    for i in range(len(labels)):
        y = y0
        for _, col, vals in series:
            bh = ph * vals[i] / top
            if bh > 0:
                d.add(Rect(x0 + slot * i + slot * 0.18, y, slot * 0.64, bh, fillColor=HexColor(col), strokeColor=None))
            y += bh
    _xlabels(d, labels, x0, y0, slot, font)
    legend(d, [(n, c, "box") for n, c, _ in series], font, h - 8, x0)
    return d


def hbar(w: float, rows: list[tuple[str, float, str, str]], fmt: Callable, font: str, marker: float | None = None,
         marker_label: str = "", top: float | None = None) -> Drawing:
    """rows: (label, value, colour, value text)."""
    rh = 15
    h = rows and len(rows) * rh + 14 or 20
    d = Drawing(w, h)
    lw = 110
    top = top or nice_max(max([r[1] for r in rows] + [marker or 0, 0]))[0]
    pw = w - lw - 60
    for i, (label, v, col, text) in enumerate(rows):
        y = h - 14 - i * rh
        _text(d, lw - 4, y, _fit(label, lw - 6, 7), font, 7, "end")
        d.add(Rect(lw, y - 2, pw * max(v, 0) / top, 9, fillColor=HexColor(col), strokeColor=None))
        _text(d, lw + pw * max(v, 0) / top + 3, y, text, font, 7)
    if marker is not None:
        mx = lw + pw * marker / top
        d.add(Line(mx, 2, mx, h - 6, strokeColor=INK, strokeWidth=0.8, strokeDashArray=[2, 2]))
        _text(d, mx, h - 5, marker_label, font, 6, "middle", MUTED)
    return d


def hgrouped(w: float, rows: list[tuple[str, float | None, float | None]], colors: tuple[str, str],
             texts: Callable, font: str) -> Drawing:
    rh = 22
    d = Drawing(w, len(rows) * rh + 16)
    lw, pw = 110, w - 110 - 70
    top = nice_max(max([v for r in rows for v in r[1:] if v] + [0]))[0]
    for i, (label, a, b) in enumerate(rows):
        y = d.height - 14 - i * rh
        _text(d, lw - 4, y, _fit(label, lw - 6, 7), font, 7, "end")
        for j, v in enumerate((a, b)):
            if v is None:
                continue
            d.add(Rect(lw, y - 3 - j * 8, pw * v / top, 7, fillColor=HexColor(colors[j]), strokeColor=None))
            _text(d, lw + pw * v / top + 3, y - 2 - j * 8, texts(v), font, 6.5)
    return d


def hstacked(w: float, rows: list[tuple[str, list[tuple[float, str]]]], font: str) -> Drawing:
    rh = 15
    d = Drawing(w, len(rows) * rh + 14)
    lw, pw = 70, w - 70 - 40
    top = max([sum(v for v, _ in segs) for _, segs in rows] + [1])
    for i, (label, segs) in enumerate(rows):
        y = d.height - 14 - i * rh
        _text(d, lw - 4, y, _fit(label, lw - 6, 7), font, 7, "end")
        x = lw
        for v, col in segs:
            if v:
                d.add(Rect(x, y - 2, pw * v / top, 9, fillColor=HexColor(col), strokeColor=None))
                x += pw * v / top
        _text(d, x + 3, y, str(int(sum(v for v, _ in segs))), font, 7)
    return d


def donut(size: float, slices: list[tuple[str, float, str]], font: str) -> Drawing:
    d = Drawing(size * 2.2, size)
    total = sum(v for _, v, _ in slices) or 1
    cx, cy, r = size / 2, size / 2, size / 2 - 2
    start = 90.0
    for _, v, col in slices:
        sweep = 360 * v / total
        if v > 0:
            d.add(Wedge(cx, cy, r, start - sweep, start, fillColor=HexColor(col), strokeColor=HexColor("#FFFFFF"), strokeWidth=0.6))
        start -= sweep
    d.add(Circle(cx, cy, r * 0.55, fillColor=HexColor("#FFFFFF"), strokeColor=None))
    for i, (label, v, col) in enumerate(slices[:8]):
        y = size - 12 - i * 11
        d.add(Rect(size + 6, y, 7, 7, fillColor=HexColor(col), strokeColor=None))
        _text(d, size + 18, y, f"{_fit(label, size, 7)}  {100 * v / total:.0f}%", font, 7)
    return d