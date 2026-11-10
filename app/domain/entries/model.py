"""Entry field rules shared by every module: cleaning, derived values, validation."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import PortalConfig
from app.domain.parsing import standardize as std

# The only user-editable fields of an entry (section 8). bill_no, net_amount, margin are never client-supplied.
EDITABLE = [
    "partner_key",
    "patient_name",
    "registration_date",
    "age",
    "age_unit",
    "gender",
    "lab_pid",
    "referred_by",
    "collection_time",
    "reporting_time",
    "tests",
    "phone",
    "email",
    "lab_cost",
    "gross_amount",
    "discount_amount",
]


class EntryError(Exception):
    def __init__(self, status: int, detail: str | dict) -> None:
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or (isinstance(v, str) and not v.strip())


def month_of(day: str) -> str:
    return day[:7]


# ───────────────────────────── cleaning ─────────────────────────────
def _text(v: Any, limit: int) -> str | None:
    s = std.clean_text(v)
    return s[:limit] if s else None


def _to_date(v: Any) -> str | None:
    if empty(v):
        return None
    try:
        d = v.date() if isinstance(v, datetime) else v if isinstance(v, date) else date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        raise ValueError("use the format YYYY-MM-DD") from None
    if not (2000 <= d.year <= date.today().year + 1):
        raise ValueError("the date looks wrong")
    return d.isoformat()


def _to_datetime(v: Any, tz: ZoneInfo) -> str | None:
    if empty(v):
        return None
    try:
        dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("use the format YYYY-MM-DDTHH:MM") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return std.iso_datetime(dt)


def _money(v: Any) -> float | None:
    if empty(v):
        return None
    try:
        d = Decimal(str(v).replace(",", "").strip())
        if not d.is_finite() or d < 0 or d > Decimal("10000000"):
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError("enter a valid amount (0 or more)") from None
    return float(d.quantize(Decimal("0.01")))


def _clean_one(key: str, v: Any, tz: ZoneInfo) -> Any:
    if key == "tests":
        return std.clean_test_list(v) or None
    if empty(v):
        return None
    if key == "partner_key":
        return str(v).strip().casefold()
    if key == "patient_name":
        s = std.standardize_name(v)
        if not s or len(s) < 2:
            raise ValueError("enter the patient's name")
        return s[:120]
    if key == "registration_date":
        return _to_date(v)
    if key in ("collection_time", "reporting_time"):
        return _to_datetime(v, tz)
    if key == "age":
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            raise ValueError("enter a whole number") from None
        if not 0 <= n <= 130:
            raise ValueError("the age looks wrong")
        return n
    if key == "age_unit":
        u = str(v).strip().upper()[:1]
        if u not in ("Y", "M", "D"):
            raise ValueError("choose Years, Months or Days")
        return u
    if key == "gender":
        g = std.standardize_gender(v)
        if g is None:
            raise ValueError("choose Male, Female or Other")
        return g
    if key == "lab_pid":
        s = re.sub(r"\s+", "", str(v))
        return s[:40] or None
    if key == "referred_by":
        return std.standardize_doctor(v)
    if key == "phone":
        return std.standardize_phone(v)
    if key == "email":
        return std.standardize_email(v)
    if key in ("lab_cost", "gross_amount", "discount_amount"):
        return _money(v)
    return _text(v, 200)


def clean_fields(raw: dict[str, Any], cfg: PortalConfig, *, defaults: bool) -> dict[str, Any]:
    """Validate and standardise the fields present in `raw`. Absent keys stay absent; empty values become None."""
    tz = ZoneInfo(cfg.app.timezone)
    labels = {f.key: f.label for f in cfg.fields}
    out: dict[str, Any] = {}
    for key in EDITABLE:
        if key in raw:
            try:
                out[key] = _clean_one(key, raw[key], tz)
            except ValueError as exc:
                raise EntryError(422, f"{labels.get(key, key)}: {exc}") from None
    if defaults:
        for f in cfg.fields:
            if f.default is not None and not f.computed and out.get(f.key) is None:
                out[f.key] = float(f.default) if f.type == "money" else f.default
    return out


# ───────────────────────────── derived values & final checks ─────────────────────────────
def derive(doc: dict[str, Any]) -> dict[str, Any]:
    """Values computed from the entry. None means 'remove this field'."""
    d: dict[str, Any] = {"name_norm": std.normalize_name(doc.get("patient_name") or "")}
    gross, disc, cost = doc.get("gross_amount"), doc.get("discount_amount") or 0.0, doc.get("lab_cost")
    net = float(Decimal(str(gross)) - Decimal(str(disc))) if gross is not None else None
    d["net_amount"] = round(net, 2) if net is not None else None
    d["margin"] = (
        float(Decimal(str(net)) - Decimal(str(cost))) if net is not None and cost is not None else None
    )
    d["status"] = "complete" if doc.get("patient_name") and doc.get("registration_date") and net is not None else "draft"
    pid, day, partner = doc.get("lab_pid"), doc.get("registration_date"), doc.get("partner_key")
    # unique on (partner_key, lab_pid, registration_date) where lab_pid exists. Implemented as one derived
    # key with a sparse unique index, which gives the same guarantee on MongoDB and in the in-memory test database.
    d["dedupe_key"] = f"{partner}|{pid.casefold()}|{day}" if pid and day and partner else None
    if doc.get("age") is not None and not doc.get("age_unit"):
        d["age_unit"] = "Y"
    elif doc.get("age") is None and doc.get("age_unit"):
        d["age_unit"] = None
    return d


def check_final(cfg: PortalConfig, module: str, doc: dict[str, Any]) -> None:
    """Required fields for the calling module (fields.yaml `required_in`) and amount sanity."""
    missing = [f.label for f in cfg.fields if module in f.required_in and not f.auto and empty(doc.get(f.key))]
    if missing:
        raise EntryError(422, "Please fill: " + ", ".join(missing) + ".")
    g, disc = doc.get("gross_amount"), doc.get("discount_amount")
    if g is not None and disc is not None and disc > g:
        raise EntryError(422, "The discount cannot be more than the gross amount.")


def same_value(key: str, a: Any, b: Any) -> bool:
    if key == "patient_name":
        return std.normalize_name(a) == std.normalize_name(b)
    if key == "tests":
        return {t.casefold() for t in a} == {t.casefold() for t in b}
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) < 1e-9
    if isinstance(a, str) and isinstance(b, str):
        return std.clean_text(a).casefold() == std.clean_text(b).casefold()
    return a == b