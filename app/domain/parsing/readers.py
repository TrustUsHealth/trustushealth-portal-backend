"""Upload validation and file reading. pdfplumber first, PyMuPDF as fallback."""

from __future__ import annotations

import io
import logging
import os
import re
import zipfile

from app.domain.parsing.models import Box, DocxBlock, DocxDoc, Page, PdfDoc, Word

log = logging.getLogger(__name__)
MAX_UNZIPPED_BYTES = 60 * 1024 * 1024


class UploadError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


class UnreadableFile(Exception):
    """The file is damaged or password-protected."""


def safe_filename(name: str | None, fallback: str = "report") -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base)[:150].strip(" .")
    return base or fallback


def detect_kind(filename: str, data: bytes) -> str:
    if data[:1024].find(b"%PDF-") != -1:
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "docx"
    raise UploadError(415, "Only PDF or Word (.docx) files are accepted.")


def validate_upload(filename: str, data: bytes, allowed: list[str], max_bytes: int) -> str:
    """Size, extension and magic-byte checks. Returns 'pdf' or 'docx'."""
    if not data:
        raise UploadError(400, "The file is empty.")
    if len(data) > max_bytes:
        raise UploadError(413, f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed:
        raise UploadError(415, "Only " + " or ".join(a.upper() for a in allowed) + " files are accepted.")
    kind = detect_kind(filename, data)
    if kind != ext:
        raise UploadError(415, "The file content does not match its extension.")
    if kind == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" not in z.namelist():
                    raise UploadError(415, "This is not a valid Word document.")
                if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED_BYTES:
                    raise UploadError(413, "The Word document is too large once unpacked.")
        except zipfile.BadZipFile:
            raise UploadError(415, "This is not a valid Word document.") from None
    return kind


# ───────────────────────────── PDF ─────────────────────────────
def _read_pdfplumber(data: bytes) -> tuple[list[Page], bool]:
    import pdfplumber

    pages: list[Page] = []
    truncated = False
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for idx, p in enumerate(pdf.pages):
            raw = p.extract_words(
                x_tolerance=3,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=["fontname", "size"],
            )
            words = [
                Word(
                    text=w["text"],
                    x0=float(w["x0"]),
                    x1=float(w["x1"]),
                    top=float(w["top"]),
                    bottom=float(w["bottom"]),
                    font=str(w.get("fontname") or ""),
                    size=float(w.get("size") or 0.0),
                )
                for w in raw
                if w["text"].strip()
            ]
            images: list[Box] = []
            try:
                images = [
                    Box(float(im["x0"]), float(im["x1"]), float(im["top"]), float(im["bottom"])) for im in p.images
                ]
            except Exception:  # noqa: BLE001 - images are only a bonus signal
                images = []
            pages.append(Page(idx + 1, float(p.width), float(p.height), words, images))
    return pages, truncated


def _read_pymupdf(data: bytes) -> tuple[list[Page], bool]:
    import pymupdf

    pages: list[Page] = []
    truncated = False
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise UnreadableFile("This PDF is password-protected.")
        for idx, page in enumerate(doc):
            words = [
                Word(text=w[4], x0=float(w[0]), x1=float(w[2]), top=float(w[1]), bottom=float(w[3]))
                for w in page.get_text("words")
                if str(w[4]).strip()
            ]
            images: list[Box] = []
            try:
                images = [Box(b[0], b[2], b[1], b[3]) for b in (i["bbox"] for i in page.get_image_info())]
            except Exception:  # noqa: BLE001
                images = []
            pages.append(Page(idx + 1, float(page.rect.width), float(page.rect.height), words, images))
    return pages, truncated


def _supplement_images(data: bytes, pages: list[Page]) -> None:
    """pdfplumber found no images at all: ask PyMuPDF too (it sees images pdfplumber can miss)."""
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for idx, page in enumerate(doc):
                if idx >= len(pages):
                    break
                pages[idx].images = [Box(b[0], b[2], b[1], b[3]) for b in (i["bbox"] for i in page.get_image_info())]
    except Exception:  # noqa: BLE001
        log.debug("image supplement skipped")


def read_pdf(data: bytes) -> PdfDoc:
    pages: list[Page] | None = None
    truncated, source = False, "pdf"
    try:
        pages, truncated = _read_pdfplumber(data)
    except Exception:  # noqa: BLE001 - damaged / encrypted / unusual PDFs: try the fallback reader
        log.warning("pdfplumber could not read the file; trying PyMuPDF")
    if not pages or not any(p.words for p in pages):
        try:
            fb_pages, fb_trunc = _read_pymupdf(data)
        except UnreadableFile:
            raise
        except Exception:  # noqa: BLE001
            fb_pages, fb_trunc = [], False
        if fb_pages and (any(p.words for p in fb_pages) or not pages):
            pages, truncated, source = fb_pages, fb_trunc, "pymupdf"
    if not pages:
        raise UnreadableFile("This PDF could not be read. It may be damaged or password-protected.")
    if source == "pdf" and not any(p.images for p in pages):
        _supplement_images(data, pages)
    return PdfDoc(pages=pages, source=source, truncated=truncated)


# ───────────────────────────── Word ─────────────────────────────
def _para_bold(p) -> bool:
    runs = [r for r in p.runs if r.text.strip()]
    return bool(runs) and all(bool(r.bold) for r in runs)


def read_docx(data: bytes) -> DocxDoc:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        d = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise UnreadableFile("This Word file could not be opened.") from exc

    blocks: list[DocxBlock] = []

    def walk(element, parent) -> None:
        for child in element.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = Paragraph(child, parent)
                text = p.text.strip()
                if text:
                    blocks.append(
                        DocxBlock("p", text=text, bold=_para_bold(p), centered=p.alignment == WD_ALIGN_PARAGRAPH.CENTER)
                    )
            elif tag == "tbl":
                rows: list[list[str]] = []
                for row in Table(child, parent).rows:
                    tcs, cells = [], []
                    for c in row.cells:  # merged cells repeat: keep one
                        if any(c._tc is t for t in tcs):
                            continue
                        tcs.append(c._tc)
                        cells.append(c.text.strip())
                    if any(cells):
                        rows.append(cells)
                if rows:
                    blocks.append(DocxBlock("t", rows=rows))

    try:  # patient details are often in the page header
        header = d.sections[0].header
        walk(header._element, header)
    except Exception:  # noqa: BLE001
        log.debug("no readable Word header")
    walk(d.element.body, d)
    return DocxDoc(blocks=blocks)