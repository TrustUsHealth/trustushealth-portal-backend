"""Yearly Excel register. MongoDB is the source of truth: on any entry change the affected month sheet is
regenerated from the database, so rows are never stale or duplicated. The workbook lives in `excel_workbooks`."""

from __future__ import annotations

import asyncio
import io
import logging
import weakref
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import PortalConfig, Settings
from app.core.db import utcnow
from app.core.filestore import NO_FILE_DATA, MongoFileStore
from app.core.mailer import EmailAttachment, EmailDeliveryError, Mailer, OutboundEmail

log = logging.getLogger(__name__)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
COL = "excel_workbooks"
ENTRIES = "entries"
_store = MongoFileStore()
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db[COL].create_index("year", unique=True)


def filename_for(cfg: PortalConfig, year: int) -> str:
    return cfg.excel.workbook_name.format(year=year)


# ───────────────────────────── sheet writing (sync, runs in a thread) ─────────────────────────────
def _cell(cfg: PortalConfig, e: dict[str, Any], field: str) -> tuple[Any, str | None]:
    ftype = next((f.type for f in cfg.fields if f.key == field), "text")
    v = e.get(field)
    if field == "tests":
        return cfg.excel.tests_separator.join(e.get("tests") or []), None
    if field == "partner_key":
        p = cfg.partners.get(v or "")
        return (p.name if p else v), None
    if field == "age":
        if v is None:
            return None, None
        return (v if (e.get("age_unit") or "Y") == "Y" else f"{v} {e['age_unit']}"), None
    if v is None:
        return None, None
    if ftype == "date":
        return date.fromisoformat(v), "DD/MM/YYYY"
    if ftype == "datetime":
        return datetime.fromisoformat(v), "DD/MM/YYYY HH:MM"
    if ftype == "money":
        return float(v), "#,##0.00"
    return v, None


def _fill_sheet(ws, cfg: PortalConfig, entries: list[dict[str, Any]]) -> None:
    headers = [c.header for c in cfg.excel.columns] + ["Entry ID"]
    ws.append(headers)
    widths = [len(h) for h in headers]
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r, e in enumerate(entries, start=2):
        values = [_cell(cfg, e, c.field) for c in cfg.excel.columns] + [(str(e["_id"]), None)]
        for c, (v, fmt) in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            if isinstance(v, str) and v.startswith("="):
                cell.data_type = "s"  # never let patient text become a formula
            if fmt:
                cell.number_format = fmt
            widths[c - 1] = max(widths[c - 1], min(len(str(v)) if v is not None else 0, 50))
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w + 2
    ws.column_dimensions[get_column_letter(len(headers))].hidden = True  # hidden Entry ID ties row to entry
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers) - 1)}{max(len(entries) + 1, 1)}"


def _render(
    cfg: PortalConfig, base: bytes | None, data: dict[int, list[dict[str, Any]]], months: set[int] | None
) -> bytes:
    wb = None
    if base is not None and months is not None:
        try:
            wb = load_workbook(io.BytesIO(base))
        except Exception:  # noqa: BLE001 - damaged workbook: rebuild everything
            wb = None
    if wb is None:
        wb, months = Workbook(), None
        wb.remove(wb.active)
        for name in MONTHS:
            wb.create_sheet(name)
    targets = range(1, 13) if months is None else sorted(months)
    for m in targets:
        name = MONTHS[m - 1]
        if name in wb.sheetnames:
            idx = wb.sheetnames.index(name)
            wb.remove(wb[name])
            ws = wb.create_sheet(name, idx)
        else:
            ws = wb.create_sheet(name)
        _fill_sheet(ws, cfg, data.get(m, []))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ───────────────────────────── database side ─────────────────────────────
async def _month_entries(db: AsyncDatabase, year: int, month: int) -> list[dict[str, Any]]:
    start = f"{year}-{month:02d}-01"
    end = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"
    cursor = (
        db[ENTRIES]
        .find({"status": "complete", "registration_date": {"$gte": start, "$lt": end}})
        .sort([("registration_date", 1), ("bill_no", 1)])
    )
    return await cursor.to_list(length=None)


async def _save_year(db: AsyncDatabase, cfg: PortalConfig, year: int, months: set[int] | None) -> None:
    """Regenerate `months` (or the whole year) from the database and save with optimistic versioning."""
    async with _lock():
        for _ in range(6):
            doc = await db[COL].find_one({"year": year})
            want = months if (doc is not None and months is not None) else None
            data = {m: await _month_entries(db, year, m) for m in (range(1, 13) if want is None else want)}
            base = bytes(doc["file"]["data"]) if doc is not None and want is not None else None
            content = await asyncio.to_thread(_render, cfg, base, data, want)
            fdoc = await _store.pack(name=filename_for(cfg, year), content_type=XLSX, data=content)
            now = utcnow()
            if doc is None:
                try:
                    await db[COL].insert_one(
                        {
                            "year": year,
                            "version": 1,
                            "file": fdoc,
                            "last_sent_month": None,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    return
                except DuplicateKeyError:
                    continue
            res = await db[COL].update_one(
                {"_id": doc["_id"], "version": doc["version"]},
                {"$set": {"file": fdoc, "updated_at": now}, "$inc": {"version": 1}},
            )
            if res.matched_count:
                return
        raise RuntimeError("The Excel register is busy. Please try again.")


async def rebuild_months(db: AsyncDatabase, cfg: PortalConfig, month_keys: Iterable[str]) -> None:
    """Called on `entry.saved` with the affected 'YYYY-MM' keys (old and new month when a date moved)."""
    by_year: dict[int, set[int]] = {}
    for mk in set(month_keys):
        by_year.setdefault(int(mk[:4]), set()).add(int(mk[5:7]))
    for year, months in sorted(by_year.items()):
        await _save_year(db, cfg, year, months)


def _meta(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "year": doc["year"],
        "filename": doc["file"]["name"],
        "version": doc["version"],
        "size": doc["file"]["size"],
        "last_sent_month": doc.get("last_sent_month"),
        "updated_at": doc.get("updated_at"),
    }


async def get_workbook(
    db: AsyncDatabase, cfg: PortalConfig, year: int, *, refresh: bool = True
) -> tuple[dict[str, Any], bytes]:
    """Download source. `refresh` regenerates every sheet from MongoDB first, so the file is always exact."""
    if refresh or await db[COL].find_one({"year": year}, {"_id": 1}) is None:
        await _save_year(db, cfg, year, None)
    doc = await db[COL].find_one({"year": year})
    return _meta(doc), bytes(doc["file"]["data"])


async def list_workbooks(db: AsyncDatabase) -> list[dict[str, Any]]:
    docs = await db[COL].find({}, NO_FILE_DATA).sort("year", -1).to_list(length=50)
    return [_meta(d) for d in docs]


# ───────────────────────────── sending ─────────────────────────────
def recipients(cfg: PortalConfig, settings: Settings) -> list[str]:
    env_name = (cfg.excel.recipients_env or "").lower()
    raw = getattr(settings, env_name, None) if env_name else None
    if raw:
        return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
    return [str(a) for a in cfg.excel.recipients]


def _mail(cfg: PortalConfig, to: str, year: int, label: str, content: bytes) -> OutboundEmail:
    brand = cfg.app.brand.name
    return OutboundEmail(
        to=to,
        subject=f"{brand} patient registry: {label}",
        text=f"The patient registry workbook for {label} is attached.\n\n{brand}",
        attachments=(EmailAttachment(filename_for(cfg, year), content, XLSX),),
    )


async def send_now(db: AsyncDatabase, cfg: PortalConfig, settings: Settings, mailer: Mailer, year: int) -> list[str]:
    """Admin 'Send now': fresh workbook to every recipient. Raises EmailDeliveryError if any send fails."""
    to = recipients(cfg, settings)
    if not to:
        return []
    _, content = await get_workbook(db, cfg, year, refresh=True)
    for addr in to:
        await mailer.send_email(_mail(cfg, addr, year, f"year {year} (as of today)", content))
    return to


async def monthly_send(
    db: AsyncDatabase, cfg: PortalConfig, settings: Settings, mailer: Mailer, *, now: datetime | None = None
) -> dict[str, Any]:
    """1st-of-month job. Idempotent: a lease plus per-recipient bookkeeping make retries safe."""
    now = now or datetime.now(ZoneInfo(cfg.app.timezone))
    prev = now.replace(day=1) - timedelta(days=1)
    month_key, year = f"{prev.year}-{prev.month:02d}", prev.year
    to = recipients(cfg, settings)
    if not to:
        return {"status": "no_recipients", "month": month_key, "sent": 0}
    await _save_year(db, cfg, year, None)
    now_utc = utcnow()
    claim = await db[COL].update_one(
        {
            "year": year,
            "last_sent_month": {"$ne": month_key},
            "$or": [{"lease_until": None}, {"lease_until": {"$lt": now_utc}}],
        },
        {"$set": {"lease_until": now_utc + timedelta(minutes=10)}},
    )
    doc = await db[COL].find_one({"year": year})
    if claim.matched_count == 0:
        done = doc.get("last_sent_month") == month_key
        return {"status": "already_sent" if done else "busy", "month": month_key, "sent": 0}

    already = set(doc.get("sent_to", [])) if doc.get("sent_month") == month_key else set()
    content = bytes(doc["file"]["data"])
    label = f"{MONTHS[prev.month - 1]} {prev.year}"
    sent, failed = 0, 0
    for addr in [a for a in to if a not in already]:
        try:
            await mailer.send_email(_mail(cfg, addr, year, label, content))
        except EmailDeliveryError:
            failed += 1
            continue
        sent += 1
        await db[COL].update_one(
            {"_id": doc["_id"]}, {"$set": {"sent_month": month_key}, "$addToSet": {"sent_to": addr}}
        )
    if failed:
        await db[COL].update_one({"_id": doc["_id"]}, {"$unset": {"lease_until": ""}})
        return {"status": "partial", "month": month_key, "sent": sent, "failed": failed}
    await db[COL].update_one(
        {"_id": doc["_id"]}, {"$set": {"last_sent_month": month_key}, "$unset": {"lease_until": ""}}
    )
    return {"status": "sent", "month": month_key, "sent": sent}
