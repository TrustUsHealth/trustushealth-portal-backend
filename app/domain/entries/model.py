"""Entry field rules shared by every module: cleaning, derived values, validation."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import PortalConfig
from app.domain import money
from app.domain.parsing import standardize as std

# The only user-editable fields of an entry (section 8). bill_no, bill_no_original, net_amount, margin are never
# client-supplied; gross_amount is the sum of test_amounts whenever those exist.
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
    "test_amounts",
    "client_name",
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


def _money(v: Any) -> Decimal | None:
    """Exact amount with two places, 0 to 10,000,000. Never float."""
    if empty(v):
        return None
    try:
        d = money.to_decimal(v)
        if d < 0 or d > money.MAX_AMOUNT:
            raise InvalidOperation
    except (InvalidOperation, ValueError, ArithmeticError):
        raise ValueError("enter a valid amount (0 or more)") from None
    return d


def _money_list(v: Any) -> list[Decimal] | None:
    if v is None or (isinstance(v, list | tuple) and len(v) == 0):
        return None
    if not isinstance(v, list | tuple):
        raise ValueError("enter one amount for every test")
    out: list[Decimal] = []
    for item in v:
        amount = _money(item)
        if amount is None:
            raise ValueError("enter an amount for every test")
        out.append(amount)
    return out


def _clean_one(key: str, v: Any, tz: ZoneInfo) -> Any:
    if key == "tests":
        return std.clean_test_list(v) or None
    if key == "test_amounts":
        return _money_list(v)
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
    if key == "client_name":
        return _text(v, 120)
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
    if out.get("test_amounts"):  # gross_amount is always the sum of the per-test amounts
        out["gross_amount"] = money.total(out["test_amounts"])
    if defaults:
        for f in cfg.fields:
            if f.default is not None and not f.computed and out.get(f.key) is None:
                out[f.key] = money.to_decimal(f.default) if f.type == "money" else f.default
    return out


# ───────────────────────────── derived values & final checks ─────────────────────────────
def derive(doc: dict[str, Any]) -> dict[str, Any]:
    """Values computed from the entry. None means 'remove this field'."""
    d: dict[str, Any] = {"name_norm": std.normalize_name(doc.get("patient_name") or "")}
    amounts = doc.get("test_amounts")
    gross = money.total(amounts) if amounts else money.opt(doc.get("gross_amount"))
    disc = money.opt(doc.get("discount_amount")) or money.ZERO
    cost = money.opt(doc.get("lab_cost"))
    net = gross - disc if gross is not None else None
    if amounts:
        d["gross_amount"] = gross
    d["net_amount"] = net
    d["margin"] = net - cost if net is not None and cost is not None else None
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
    amounts, tests = doc.get("test_amounts"), doc.get("tests")
    if amounts and tests and len(amounts) != len(tests):
        raise EntryError(422, "Enter one amount for every test (the amounts and the tests must match).")
    g, disc = doc.get("gross_amount"), doc.get("discount_amount")
    if g is not None and disc is not None and money.to_decimal(disc) > money.to_decimal(g):
        raise EntryError(422, "The discount cannot be more than the gross amount.")


def same_value(key: str, a: Any, b: Any) -> bool:
    if key == "patient_name":
        return std.normalize_name(a) == std.normalize_name(b)
    if key == "tests":
        return {t.casefold() for t in a} == {t.casefold() for t in b}
    if key == "test_amounts":
        return [money.to_decimal(x) for x in a] == [money.to_decimal(x) for x in b]
    numeric = (int, float, Decimal)
    if isinstance(a, numeric) and isinstance(b, numeric) and not isinstance(a, bool) and not isinstance(b, bool):
        return money.to_decimal(a) == money.to_decimal(b)
    if isinstance(a, str) and isinstance(b, str):
        return std.clean_text(a).casefold() == std.clean_text(b).casefold()
    return a == b