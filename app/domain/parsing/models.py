"""Plain data holders for the parsing engine."""

from __future__ import annotations

from dataclasses import dataclass, field

_BOLD_HINTS = ("bold", "black", "heavy", "demi")


def is_bold(font: str) -> bool:
    f = (font or "").casefold()
    return any(h in f for h in _BOLD_HINTS)


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    font: str = ""
    size: float = 0.0

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Line:
    words: list[Word]

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def max_gap(self) -> float:
        return max((b.x0 - a.x1 for a, b in zip(self.words, self.words[1:], strict=False)), default=0.0)

    def bold_ratio(self) -> float:
        return sum(1 for w in self.words if is_bold(w.font)) / len(self.words)


@dataclass
class Page:
    number: int
    width: float
    height: float
    words: list[Word]


@dataclass
class PdfDoc:
    pages: list[Page]
    source: str = "pdf"  # pdf (pdfplumber) | pymupdf (fallback)
    truncated: bool = False


@dataclass
class DocxBlock:
    kind: str  # "p" paragraph | "t" table
    text: str = ""
    bold: bool = False
    centered: bool = False
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class DocxDoc:
    blocks: list[DocxBlock]