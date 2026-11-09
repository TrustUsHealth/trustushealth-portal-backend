"""Generic, YAML-driven report parser. No lab-specific Python.

PDF: words with positions are grouped into lines; labels (from the partner YAML) are matched by text, never by line
order, so two-column headers work. Values stop at the next label or at a wide gap (column break).
Tests: `page_heading` (centred bold heading per page) or `investigation_column` (see `_scan_rows`: a test is a row
with the NABL icon, or a bold row followed by a specimen line, or a bold profile title; components inside a profile
and text blocks are never tests).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from app.core.config import HeaderField, PartnerCfg, PortalConfig
from app.domain.parsing import standardize as std
from app.domain.parsing.models import DocxDoc, Line, Page, Word, is_bold, is_italic
from app.domain.parsing.readers import detect_kind, read_docx, read_pdf

log = logging.getLogger(__name__)

HEADER_FRACTION = 0.40  # patient-details block = top 40% of the page
FOOTER_FRACTION = 0.86  # signatures, accreditation logos and the lab footer sit below this line
LINE_TOL = 3.5
VALUE_GAP_FIRST = 24.0  # colon -> first value word
VALUE_GAP = 18.0  # between value words; a wider gap is a column break
COLUMN_GAP = 18.0
NAME_GAP = 4.5  # words closer than this belong to the same name; the value column is always further away
ICON_MAX = 28.0  # accreditation seal is a small image, in points
ICON_MAX_DY = 9.0  # vertical distance between the seal and the row it belongs to
VERIFY_BELOW = 0.8
SCORED = {"patient_name", "registration_date", "age", "gender", "lab_pid", "tests"}
COMPOSITE = {"age_gender": ("age", "age_unit", "gender")}
# Lines starting like this begin a text block (notes, interpretation...). Nothing inside it is a test.
STOP_PREFIXES = (
    "interpretation",
    "test interpretation",
    "medical remarks",
    "remark",
    "note",
    "comment",
    "disclaimer",
    "associated",
    "clinical",
    "reference",
    "pre-test",
    "end of report",
    "-- end",
    "** end",
    "*** end",
    "**",
    "kindly",
    "page",
    "method",
)
# First part of the grey "(Specimen, Method)" line printed under a real test name.
SPECIMENS = (
    "serum",
    "plasma",
    "urine",
    "blood",
    "whole blood",
    "edta",
    "fluoride",
    "citrate",
    "heparin",
    "csf",
    "cerebrospinal fluid",
    "stool",
    "swab",
    "sputum",
    "saliva",
    "semen",
    "body fluid",
    "pleural fluid",
    "ascitic fluid",
    "synovial fluid",
    "tissue",
    "biopsy",
    "smear",
    "pus",
    "bone marrow",
    "cord blood",
    "dried blood spot",
    "nasopharyngeal",
    "throat",
    "nasal",
    "hair",
    "nail",
)
_END_RE = re.compile(r"^\W*end of report", re.I)
_LIST_RE = re.compile(r"^\s*(?:\d+[.)]|[\u2022\u00b7\u25aa\u25cf])\s*")


class ParseError(Exception):
    """The lab cannot be read automatically (for example `parser: manual`)."""


@dataclass
class ParseResult:
    partner_key: str
    source: str = "pdf"
    page_count: int = 0
    fields: dict[str, Any] = field(default_factory=dict)
    field_confidence: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    needs_verify: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    missing_labels: list[str] = field(default_factory=list)  # configured labels not found (CI messages)
    raw_fields: dict[str, str] = field(default_factory=dict)  # audit only, never searchable
    flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return (
            self.confidence < VERIFY_BELOW
            or bool(self.needs_verify)
            or bool({"mixed_patients", "scanned"} & set(self.flags))
        )

    def stored(self) -> dict[str, Any]:
        """The `parse` sub-document saved with a report."""
        return {
            "source": self.source,
            "confidence": self.confidence,
            "field_confidence": self.field_confidence,
            "needs_verify": self.needs_verify,
            "missing_fields": self.missing_fields,
            "flags": self.flags,
            "warnings": self.warnings,
            "raw_fields": self.raw_fields,
        }


# ───────────────────────────── helpers ─────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def _label_map(partner: PartnerCfg) -> dict[str, str]:
    """normalised label -> original label, for header labels and audit-only extra labels."""
    out = {_norm(hf.label): hf.label for hf in partner.header.values() if hf.label}
    out.update({_norm(lab): lab for lab in partner.extra_labels})
    return out


def _targets(key: str) -> tuple[str, ...]:
    return COMPOSITE.get(key, (key,))


def _ftype(key: str, hf: HeaderField, fdefs: dict) -> str:
    if hf.type != "text":
        return hf.type
    fd = fdefs.get(key)
    return fd.type if fd is not None and fd.type in ("date", "datetime") else "text"


def _clean_test_name(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" \t:-")


# ───────────────────────────── header block (PDF) ─────────────────────────────
@dataclass
class _Hit:
    label: str  # normalised
    colon: Word
    words: list[Word]
    line_index: int

    @property
    def value(self) -> str:
        return " ".join(w.text for w in self.words).strip()


@dataclass
class _View:
    page: Page
    lines: list[Line]
    hits: list[_Hit]
    header_end: float
    bold_present: bool


def _split_colons(words: list[Word]) -> list[Word]:
    """Make every label colon its own word: 'Name:' -> 'Name' ':' and ':MR.' -> ':' 'MR.'."""
    out: list[Word] = []
    for w in words:
        t = w.text
        if t == ":" or len(t) < 2 or not (t.startswith(":") or t.endswith(":")):
            out.append(w)
        elif t.endswith(":") and not t.startswith(":"):
            cut = w.x0 + (w.x1 - w.x0) * (len(t) - 1) / len(t)
            out += [replace(w, text=t[:-1], x1=cut), replace(w, text=":", x0=cut)]
        else:
            cut = w.x0 + (w.x1 - w.x0) / len(t)
            out += [replace(w, text=":", x1=cut), replace(w, text=t[1:], x0=cut)]
    return out


def _cluster(words: list[Word]) -> list[Line]:
    groups: list[list[Word]] = []
    refs: list[float] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        if groups and abs(w.cy - refs[-1]) <= LINE_TOL:
            groups[-1].append(w)
        else:
            groups.append([w])
            refs.append(w.cy)
    return [Line(sorted(g, key=lambda w: w.x0)) for g in groups]


def _match_label(ws: list[Word], i: int, labels: dict[str, str]) -> tuple[str, int] | None:
    best: tuple[str, int] | None = None
    for lab in labels:
        acc = ""
        for k in range(i, min(len(ws), i + 8)):
            acc += _norm(ws[k].text)
            if acc == lab:
                if best is None or k + 1 > best[1]:
                    best = (lab, k + 1)
                break
            if not lab.startswith(acc):
                break
    return best


def _scan_line(line: Line, labels: dict[str, str], line_index: int) -> list[_Hit]:
    ws = line.words
    starts: list[tuple[int, str, int]] = []
    i = 0
    while i < len(ws):
        m = _match_label(ws, i, labels)
        if m and m[1] < len(ws) and ws[m[1]].text == ":":  # a label counts only when a colon follows
            starts.append((i, m[0], m[1]))
            i = m[1] + 1
        else:
            i += 1
    hits: list[_Hit] = []
    for n, (_, lab, ci) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(ws)
        colon, value, prev_x, first = ws[ci], [], ws[ci].x1, True
        for w in ws[ci + 1 : end]:
            if w.x0 - prev_x > (VALUE_GAP_FIRST if first else VALUE_GAP):
                break
            value.append(w)
            prev_x, first = w.x1, False
        hits.append(_Hit(lab, colon, value, line_index))
    return hits


def _build_view(page: Page, labels: dict[str, str]) -> _View:
    words = _split_colons(page.words)
    lines = _cluster(words)
    zone = page.height * HEADER_FRACTION
    hits: list[_Hit] = []
    for idx, ln in enumerate(lines):
        if ln.cy > zone:
            break
        hits.extend(_scan_line(ln, labels, idx))
    header_end = max((lines[h.line_index].bottom for h in hits), default=0.0)
    return _View(page, lines, hits, header_end, any(is_bold(w.font) for w in words))


def _first_hits(view: _View) -> dict[str, _Hit]:
    out: dict[str, _Hit] = {}
    for h in view.hits:
        out.setdefault(h.label, h)
    return out


def _value_below(view: _View, hit: _Hit) -> str:
    """Some values (e.g. a barcode's number) sit on the line under the label. Used for audit-only labels."""
    nxt_i = hit.line_index + 1
    if nxt_i >= len(view.lines):
        return ""
    cur, nxt = view.lines[hit.line_index], view.lines[nxt_i]
    if nxt.cy - cur.cy > 28 or any(h.line_index == nxt_i for h in view.hits):
        return ""
    picked = [w for w in nxt.words if w.x0 >= hit.colon.x0 - 6]
    if not picked or len(picked) > 3 or any(w.text == ":" for w in picked):
        return ""
    return " ".join(w.text for w in picked)


# ───────────────────────────── standardising the header values ─────────────────────────────
def _text_value(key: str, rv: str) -> tuple[str | None, float]:
    rv = std.clean_text(rv)
    if key == "patient_name":
        v = std.standardize_name(rv)
        return v, (1.0 if v and len(v) >= 3 and not re.search(r"\d", v) else 0.6)
    if key == "referred_by":
        return std.standardize_doctor(rv), 1.0
    if key == "gender":
        v = std.standardize_gender(rv)
        return v, (1.0 if v else 0.0)
    if key in ("phone", "email"):
        try:
            return (std.standardize_phone(rv) if key == "phone" else std.standardize_email(rv)), 1.0
        except ValueError:
            return None, 0.0
    if key == "lab_pid":
        return (re.sub(r"\s+", "", rv) or None), 1.0
    return (rv or None), 1.0


def _read_header(
    partner: PartnerCfg, cfg: PortalConfig, raw: dict[str, str]
) -> tuple[dict[str, Any], dict[str, float], list[str], list[str]]:
    fdefs = {f.key: f for f in cfg.fields}
    fields: dict[str, Any] = {}
    conf: dict[str, float] = {}
    warnings: list[str] = []
    missing_labels: list[str] = []

    def put(key: str, value: Any, c: float, rv: str | None) -> None:
        if value not in (None, ""):
            fields[key], conf[key] = value, c
        elif (rv and rv.strip()) or key in SCORED:
            conf[key] = 0.0

    for key, hf in partner.header.items():
        if hf.from_:
            continue
        rv = raw.get(_norm(hf.label or ""))
        if rv is None:
            missing_labels.append(hf.label or key)
            for t in _targets(key):
                put(t, None, 0.0, None)
            continue
        ftype = _ftype(key, hf, fdefs)
        unreadable = False
        if key == "age_gender":
            age, unit, gender, ac, gc = std.parse_age_gender(rv)
            put("age", age, ac, rv)
            put("age_unit", unit, ac, rv)
            put("gender", gender, gc, rv)
            unreadable = bool(rv.strip()) and age is None and gender is None
        elif ftype == "datetime":
            dt = std.parse_datetime(rv, partner.datetime_formats)
            put(key, std.iso_datetime(dt) if dt else None, 1.0, rv)
            unreadable = bool(rv.strip()) and dt is None
        elif ftype == "date":
            d = std.parse_date(rv, partner.datetime_formats)
            put(key, d.isoformat() if d else None, 1.0, rv)
            unreadable = bool(rv.strip()) and d is None
        else:
            val, c = _text_value(key, rv)
            put(key, val, c, rv)
            unreadable = bool(rv.strip()) and val is None and key in ("phone", "email", "gender")
        if unreadable:
            warnings.append(f"Could not read the value next to '{hf.label}'.")

    for key, hf in partner.header.items():  # derived entries: registration_date from collection_time
        if not hf.from_:
            continue
        src = fields.get(hf.from_)
        val = src[:10] if (src and _ftype(key, hf, fdefs) == "date") else src
        put(key, val, conf.get(hf.from_, 0.0), src or "")
    return fields, conf, warnings, missing_labels


# ───────────────────────────── tests: page_heading (Spectrum; unchanged) ─────────────────────────────
def _is_heading(ln: Line, v: _View) -> bool:
    page, text = v.page, ln.text.strip()
    if len(text) < 3 or not re.search(r"[A-Za-z]", text) or _END_RE.match(text):
        return False
    if any(w.text == ":" for w in ln.words) or ln.max_gap() > 25 or ln.width > 0.9 * page.width:
        return False
    if abs(ln.cx - page.width / 2) > 0.10 * page.width:
        return False
    return not (v.bold_present and ln.bold_ratio() < 0.6)


def _heading_for(v: _View) -> str | None:
    for idx, ln in enumerate(v.lines):
        if ln.cy <= v.header_end or not _is_heading(ln, v):
            continue
        parts = [ln]
        for nxt in v.lines[idx + 1 :]:  # a long heading may wrap onto a second centred line
            prev = parts[-1]
            if _is_heading(nxt, v) and nxt.top - prev.bottom <= 1.2 * (prev.bottom - prev.top):
                parts.append(nxt)
            else:
                break
        return _clean_test_name(" ".join(p.text for p in parts))
    return None


# ───────────────────────────── tests: investigation_column (Metropolis) ─────────────────────────────
@dataclass
class _Columns:
    name: tuple[float, float]
    value: tuple[float, float] | None


@dataclass
class _Scan:
    """State carried from page to page while reading the Investigation table."""

    names: list[str] = field(default_factory=list)
    in_profile: bool = False  # inside a profile (panel): rows below it are components, not tests
    in_text: bool = False  # inside a notes / interpretation block
    fallback: str | None = None  # first valued row, used only if nothing else qualifies


def _columns(line: Line, anchor: Word, page: Page) -> _Columns:
    groups: list[list[Word]] = [[line.words[0]]]
    for w in line.words[1:]:
        if w.x0 - groups[-1][-1].x1 > COLUMN_GAP:
            groups.append([w])
        else:
            groups[-1].append(w)
    gi = next(i for i, g in enumerate(groups) if anchor in g)
    left = groups[gi][0].x0 - 4
    if gi + 1 >= len(groups):
        return _Columns((left, page.width / 2), None)
    v0 = groups[gi + 1][0].x0
    v1 = groups[gi + 2][0].x0 - 2 if gi + 2 < len(groups) else groups[gi + 1][-1].x1 + 60
    return _Columns((left, v0 - 3), (v0 - 4, v1))


def _is_stop(low: str) -> bool:
    return low.startswith(STOP_PREFIXES)


def _is_specimen(text: str) -> bool:
    """'(Serum, NM-BAPTA)' -> True; '(Legals Test)' or '(Large Round Epithelial Cells)' -> False."""
    head = re.split(r"[,)]", text.strip().lstrip("(").strip().casefold(), maxsplit=1)[0].strip()
    return any(head == s or head.startswith(s + " ") for s in SPECIMENS)


def _icon_line_indexes(v: _View, start: int, name_left: float) -> set[int]:
    """Lines that have the accreditation seal (a small image) just left of the name column."""
    out: set[int] = set()
    for im in v.page.images:
        w, h = im.x1 - im.x0, im.bottom - im.top
        if not (3 <= w <= ICON_MAX and 3 <= h <= ICON_MAX) or im.x1 > name_left + 3 or im.x0 < 0:
            continue
        cy = (im.top + im.bottom) / 2
        best, best_d = None, ICON_MAX_DY + 0.001
        for i in range(start, len(v.lines)):
            ln = v.lines[i]
            d = abs(ln.cy - cy)
            if d < best_d and ln.x0 >= name_left - 1:
                best, best_d = i, d
        if best is not None:
            if v.lines[best].text.lstrip().startswith("(") and best > start:
                best -= 1  # the seal is centred between the name and its grey line: it belongs to the name
            out.add(best)
    return out


def _scan_rows(v: _View, start: int, cols: _Columns, st: _Scan) -> None:
    """Decide which rows of the Investigation table are real tests.

    A test is: (1) a row with the accreditation seal; (2) a bold row with a value whose next grey line starts with
    a specimen (Serum, Urine...); (3) a bold, non-italic, valueless title that opens a profile. Components inside a
    profile, italic section headings, notes and footers are never tests. A profile ends at the next seal or at a
    specimen row at the top of a page (each Metropolis test starts a new page).
    """
    strict = v.bold_present
    nb, vb = cols.name, cols.value
    vb_right = vb[1] if vb else v.page.width * 0.6
    icons = _icon_line_indexes(v, start, nb[0])
    cutoff = v.page.height * FOOTER_FRACTION
    lines = v.lines
    first_row = True
    last_ln: Line | None = None  # last accepted test line, to join a wrapped name

    for i in range(start, len(lines)):
        ln = lines[i]
        if ln.cy > cutoff:
            break
        if _is_stop(ln.text.strip().casefold()) or _LIST_RE.match(ln.text):
            st.in_text, last_ln = True, None
            continue
        ws = ln.words
        s = next((k for k, w in enumerate(ws) if nb[0] <= w.x0 < nb[1]), None)
        if s is None:
            continue
        cluster = [ws[s]]
        for w in ws[s + 1 :]:
            if w.x0 - cluster[-1].x1 < NAME_GAP:
                cluster.append(w)
            else:
                break
        rest = ws[s + len(cluster) :]
        name = _clean_test_name(" ".join(w.text for w in cluster))
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        if name.startswith("("):  # grey method / specimen line
            last_ln = None
            continue

        is_first, first_row = first_row, False
        has_value = bool(vb) and any(vb[0] <= w.x0 < vb[1] for w in rest)
        bold = all(is_bold(w.font) for w in cluster) if strict else True
        italic = any(is_italic(w.font) for w in cluster)
        icon = i in icons

        if has_value:  # a row with a result
            if not (bold or icon):
                last_ln = None
                continue
            st.in_text = False
            if st.fallback is None:
                st.fallback = name
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            specimen_next = (
                nxt is not None
                and nxt.text.lstrip().startswith("(")
                and _is_specimen(nxt.text)
                and nxt.cy - ln.cy <= 30
            )
            if icon or (specimen_next and (not st.in_profile or is_first)):
                st.names.append(name)
                st.in_profile, last_ln = False, ln
            else:
                last_ln = None  # a component of the current profile
            continue

        # no result on this line: a profile title, a wrapped name, a section heading or plain text
        if rest or any(w.text == ":" for w in ws) or cluster[-1].x1 > vb_right:
            last_ln = None
            continue
        if icon:
            st.names.append(name)
            st.in_profile, st.in_text, last_ln = True, False, ln
            continue
        adjacent = last_ln is not None and ln.top - last_ln.bottom <= 1.5 * (last_ln.bottom - last_ln.top)
        if adjacent and strict and bold and not italic and st.names:
            st.names[-1] = _clean_test_name(st.names[-1] + " " + name)
            last_ln = ln
        elif strict and bold and not italic and not st.in_profile and not st.in_text:
            st.names.append(name)
            st.in_profile, last_ln = True, ln
        else:
            last_ln = None


def _investigation_tests(views: list[_View]) -> tuple[list[str], bool]:
    """Returns (test names, used_fallback)."""
    st = _Scan()
    cols: _Columns | None = None
    for v in views:
        anchor_idx, anchor = None, None
        for i, ln in enumerate(v.lines):
            if ln.cy <= v.header_end:
                continue
            anchor = next((w for w in ln.words if _norm(w.text).strip(":") == "investigation"), None)
            if anchor:
                anchor_idx = i
                break
        if anchor_idx is not None and anchor is not None:
            cols = _columns(v.lines[anchor_idx], anchor, v.page)
            start = anchor_idx + 1
        elif cols is not None:  # continuation page: reuse the columns, skip the repeated patient block
            start = next((i for i, ln in enumerate(v.lines) if ln.cy > v.header_end), len(v.lines))
        else:
            continue
        _scan_rows(v, start, cols, st)
    if not st.names and st.fallback:
        return [st.fallback], True
    return st.names, False


# ───────────────────────────── assembling the result ─────────────────────────────
def _finish(res: ParseResult, partner: PartnerCfg) -> ParseResult:
    conf = res.field_confidence
    res.confidence = round(sum(conf.values()) / len(conf), 3) if conf else 0.0
    res.needs_verify = sorted(k for k, c in conf.items() if c < VERIFY_BELOW)
    expected = {t for key in partner.header for t in _targets(key)} | {"tests"}
    res.missing_fields = sorted(k for k in (expected & SCORED) if k not in res.fields)
    return res


def _add_tests(res: ParseResult, names: list[str], confidence: float = 1.0) -> None:
    tests = std.clean_test_list(names)
    if tests:
        res.fields["tests"] = tests
        res.field_confidence["tests"] = confidence
    else:
        res.field_confidence["tests"] = 0.0
        res.warnings.append("No test names were found. Please add them.")


def _parse_pdf(doc, partner: PartnerCfg, cfg: PortalConfig, labels: dict[str, str]) -> ParseResult:
    res = ParseResult(partner_key=partner.key, source=doc.source, page_count=len(doc.pages))
    if doc.truncated:
        res.flags.append("truncated")
        res.warnings.append("Only the first pages of this long file were read.")
    if not any(p.words for p in doc.pages):
        res.flags.append("scanned")
        res.warnings.append("No readable text was found (scanned image?). Please enter the details manually.")
        return res

    views = [_build_view(p, labels) for p in doc.pages]
    head = next((v for v in views if v.hits), None)
    extras = {_norm(lab) for lab in partner.extra_labels}
    raw_by_norm: dict[str, str] = {}
    if head is None:
        res.warnings.append("The patient details block was not found. Please check the lab selected.")
    else:
        for norm, hit in _first_hits(head).items():
            value = hit.value or (_value_below(head, hit) if norm in extras else "")
            raw_by_norm[norm] = value
            res.raw_fields[labels[norm]] = value

    res.fields, res.field_confidence, warns, res.missing_labels = _read_header(partner, cfg, raw_by_norm)
    res.warnings += warns
    res.fields["partner_key"] = partner.key

    src = partner.tests.source if partner.tests else ""
    if src == "page_heading":
        names = [h for h in (_heading_for(v) for v in views) if h]
        _add_tests(res, names)
    else:
        names, used_fallback = _investigation_tests(views)
        if used_fallback:
            res.warnings.append("The test names could not be told apart reliably. Please verify the tests.")
        _add_tests(res, names, confidence=0.6 if used_fallback else 1.0)

    idents = _page_idents(views, partner)
    if len(idents) > 1 and len({i for i in idents}) > 1:
        res.flags.append("mixed_patients")
        res.warnings.append("Different patients seem to be in this file. Please check before saving.")
    return _finish(res, partner)


def _page_idents(views: list[_View], partner: PartnerCfg) -> list[tuple[str, str]]:
    def label(key: str) -> str | None:
        hf = partner.header.get(key)
        return _norm(hf.label) if hf and hf.label else None

    n_lab, p_lab = label("patient_name"), label("lab_pid")
    out: list[tuple[str, str]] = []
    for v in views:
        hits = _first_hits(v)
        name = hits[n_lab].value if n_lab and n_lab in hits else ""
        pid = hits[p_lab].value if p_lab and p_lab in hits else ""
        if name or pid:
            out.append((std.normalize_name(std.standardize_name(name) or ""), _norm(pid)))
    return out


# ───────────────────────────── Word documents (simpler, text-based rules) ─────────────────────────────
def _row_line(cells: list[str], labels: dict[str, str]) -> str:
    out: list[str] = []
    for i, c in enumerate(cells):
        out.append(c)
        if _norm(c.rstrip(": ")) in labels and not c.rstrip().endswith(":") and i + 1 < len(cells):
            out.append(":")
    return " ".join(out)


def _label_regex(labels: dict[str, str]) -> re.Pattern[str]:
    alts = sorted((re.escape(lab).replace(r"\ ", r"\s*") for lab in labels.values()), key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?P<label>" + "|".join(alts) + r")\s*:", re.I)


def _parse_docx(doc: DocxDoc, partner: PartnerCfg, cfg: PortalConfig, labels: dict[str, str]) -> ParseResult:
    res = ParseResult(partner_key=partner.key, source="docx", page_count=1, flags=["docx"])
    lines: list[tuple[int, str]] = []
    for bi, b in enumerate(doc.blocks):
        if b.kind == "p":
            lines.append((bi, b.text))
        else:
            lines += [(bi, _row_line(row, labels)) for row in b.rows]
    pat = _label_regex(labels)
    raw_by_norm: dict[str, str] = {}
    last_block = -1
    for bi, text in lines[:80]:
        ms = list(pat.finditer(text))
        for n, m in enumerate(ms):
            norm = _norm(m.group("label"))
            end = ms[n + 1].start() if n + 1 < len(ms) else len(text)
            if norm not in raw_by_norm:
                raw_by_norm[norm] = text[m.end() : end].strip(" |\t")
                res.raw_fields[labels.get(norm, norm)] = raw_by_norm[norm]
            last_block = max(last_block, bi)

    res.fields, res.field_confidence, warns, res.missing_labels = _read_header(partner, cfg, raw_by_norm)
    res.warnings += warns
    res.fields["partner_key"] = partner.key

    names: list[str] = []
    if partner.tests and partner.tests.source == "page_heading":
        for b in doc.blocks[last_block + 1 :]:
            if b.kind == "p" and b.bold and ":" not in b.text and len(b.text) >= 3 and not _END_RE.match(b.text):
                names.append(_clean_test_name(b.text))
                break
    else:
        for b in doc.blocks:
            if b.kind != "t":
                continue
            header = [c.casefold().strip(": ") for c in b.rows[0]]
            if "investigation" not in header:
                continue
            col = header.index("investigation")
            for row in b.rows[1:]:
                name = row[col] if len(row) > col else ""
                if name and not name.startswith("(") and sum(1 for c in row if c) >= 2:
                    names.append(_clean_test_name(name))
    _add_tests(res, names)
    _finish(res, partner)
    res.confidence = round(res.confidence * 0.9, 3)  # simpler rules than PDF: always ask staff to look
    res.warnings.append("Word files are read with simpler rules. Please verify the details.")
    return res


# ───────────────────────────── public entry point ─────────────────────────────
def parse_report(data: bytes, filename: str, partner: PartnerCfg, cfg: PortalConfig) -> ParseResult:
    """Parse one report with the chosen lab's YAML rules. CPU-bound: call through `asyncio.to_thread`."""
    if partner.parser != "generic":
        raise ParseError(f"{partner.name} reports are entered manually.")
    labels = _label_map(partner)
    if detect_kind(filename, data) == "pdf":
        return _parse_pdf(read_pdf(data), partner, cfg, labels)
    return _parse_docx(read_docx(data), partner, cfg, labels)