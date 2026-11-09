"""Standardisation rules shared by the parser and the entry service"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
NULLISH = {"-", "--", "na", "n/a", "nil", "none", "null", "."}
_PATIENT_TITLES = re.compile(r"^\s*(?:(?:mrs|mr|ms|master|miss|mstr)\b\.?\s*|baby\b\.?(?!\s+of\b)\s*)+", re.I)
_DOCTOR_PREFIX = re.compile(r"^\s*(?:dr|doctor)\b\.?\s*", re.I)
_NORMALIZE_TITLES = {"mr", "mrs", "ms", "master", "miss", "baby", "dr", "mstr"}
_AGE_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<u>year\(s\)|years|year|yrs|yr|y|month\(s\)|months|month|mths|mth|mos|mo|m|day\(s\)|days|day|d)?"
    r"(?![A-Za-z])",
    re.I,
)
_GENDER_RE = re.compile(r"(?<![A-Za-z])(female|male|transgender|other|f|m)(?![A-Za-z])", re.I)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_text(value: object) -> str:
    if value is None:
        return ""
    s = str(value).translate(_ZERO_WIDTH).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _title_token(token: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", token)
    if 0 < len(letters) <= 2:  # initials such as "MS", "R.K."
        return token.upper()
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), token)


def standardize_name(raw: object) -> str | None:
    s = clean_text(raw)
    if not s or s.casefold() in NULLISH:
        return None
    s = _PATIENT_TITLES.sub("", s).strip(" ,.")
    if not s:
        return None
    return " ".join(_title_token(t) for t in s.split())


def normalize_name(name: object) -> str:
    """Lower-case, no titles, no punctuation, single spaces (used for duplicate checks)."""
    s = clean_text(name).casefold()
    s = "".join(ch if (ch.isalnum() or unicodedata.category(ch).startswith("M")) else " " for ch in s)
    tokens = s.split()
    while len(tokens) > 1 and tokens[0] in _NORMALIZE_TITLES:
        tokens.pop(0)
    return " ".join(tokens)


def standardize_doctor(raw: object) -> str | None:
    s = clean_text(raw)
    if not s or s.casefold() in NULLISH:
        return None
    while True:
        stripped = _DOCTOR_PREFIX.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    s = s.strip(" .,-")
    if not s:
        return None
    if s.casefold() == "self":
        return "Self"
    return "Dr. " + " ".join(_title_token(t) for t in s.split())


def standardize_gender(raw: object) -> str | None:
    s = clean_text(raw).casefold().strip(".")
    if s in ("male", "m"):
        return "Male"
    if s in ("female", "f"):
        return "Female"
    if s in ("other", "o", "transgender", "third gender"):
        return "Other"
    return None


def standardize_phone(raw: object) -> str | None:
    """Canonical form `+<country><number>`; bare 10-digit numbers are treated as Indian mobiles."""
    s = clean_text(raw)
    if not s or s.casefold() in NULLISH:
        return None
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if plus:
        ok = 8 <= len(digits) <= 15
        out = "+" + digits
    elif len(digits) == 10:
        ok, out = True, "+91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        ok, out = True, "+91" + digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        ok, out = True, "+" + digits
    else:
        ok, out = 8 <= len(digits) <= 15, "+" + digits
    if not ok:
        raise ValueError("the phone number looks invalid")
    return out


def standardize_email(raw: object) -> str | None:
    s = clean_text(raw).casefold()
    if not s or s in NULLISH:
        return None
    if not _EMAIL_RE.match(s) or len(s) > 120:
        raise ValueError("the email address looks invalid")
    return s


def _unit(u: str) -> str:
    return {"y": "Y", "m": "M", "d": "D"}[u[0].casefold()]


def parse_age_gender(text: str) -> tuple[int | None, str | None, str | None, float, float]:
    """'45 Years / Male' -> (45, 'Y', 'Male', age_confidence, gender_confidence)."""
    s = clean_text(text)
    age = unit = gender = None
    age_conf = gender_conf = 0.0
    rest = s
    m = _AGE_RE.search(s)
    if m:
        n = float(m.group("n"))
        raw_unit = m.group("u")
        unit = _unit(raw_unit) if raw_unit else "Y"
        age_conf = 1.0 if raw_unit else 0.7  # no unit printed: assume years, ask staff to verify
        if n != int(n):  # 1.5 years -> 18 months
            n, unit = (round(n * 12), "M") if unit == "Y" else (round(n * 30), "D") if unit == "M" else (round(n), "D")
        age = int(n)
        if (unit == "Y" and age > 120) or (unit == "M" and age > 1440):
            age_conf = min(age_conf, 0.3)
        rest = s[: m.start()] + " " + s[m.end() :]
    g = _GENDER_RE.search(rest)
    if g:
        gender, gender_conf = standardize_gender(g.group(1)), 1.0
    return age, (unit if age is not None else None), gender, age_conf, gender_conf


def age_years(age: int | float | None, unit: str | None) -> float | None:
    if age is None:
        return None
    return {"M": age / 12, "D": age / 365}.get(unit or "Y", float(age))


def _date_only_formats(formats: list[str]) -> list[str]:
    out = []
    for f in formats:
        idx = min((f.find(d) for d in ("%H", "%I") if d in f), default=-1)
        if idx > 0:
            out.append(f[:idx].rstrip(" ,-T:"))
    return out


def parse_datetime(value: object, formats: list[str]) -> datetime | None:
    v = clean_text(value)
    if not v:
        return None
    candidates = [v] + ([v.rstrip(".")] if v.endswith(".") else [])
    for cand in candidates:
        for fmt in formats:
            for f in {fmt, fmt.rstrip(".")}:
                try:
                    return datetime.strptime(cand, f)
                except ValueError:
                    continue
    return None


def parse_date(value: object, formats: list[str]) -> date | None:
    dt = parse_datetime(value, formats)
    if dt:
        return dt.date()
    v = clean_text(value)
    for f in _date_only_formats(formats):
        try:
            return datetime.strptime(v, f).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def iso_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def clean_test_list(items: object, *, max_items: int = 60) -> list[str]:
    """Trim, collapse spaces, drop duplicates (case-insensitive), keep the report's own wording and order."""
    if items is None:
        return []
    if isinstance(items, str):
        items = re.split(r"[;\n]+", items)
    out, seen = [], set()
    for it in items:
        t = clean_text(it)[:200]
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out[:max_items]