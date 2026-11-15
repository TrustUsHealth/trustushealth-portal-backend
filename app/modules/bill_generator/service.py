"""bill_generator glue: lab scope, rate limits, Spectrum and Metropolis flows, email, events.
Business rules live in `app/domain` (shared by every module); nothing here is stored."""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import PartnerCfg, PortalConfig
from app.core.deps import CurrentUser
from app.core.events import EventBus
from app.core.mailer import EmailAttachment, Mailer, OutboundEmail
from app.core.rbac import decide_access, grants_for, visibility_map
from app.domain import excel_register, money
from app.domain.bills.errors import BillError
from app.domain.bills.registry import bill_module
from app.domain.customers import service as customers
from app.domain.entries import repository as entry_repo
from app.domain.entries import service as entry_service
from app.domain.entries.dedupe import find_matches
from app.domain.entries.model import EntryError, clean_fields, empty
from app.domain.parsing import standardize as std
from app.modules.bill_generator.schemas import EntryBillOut

log = logging.getLogger(__name__)
MODULE = "bill_generator"


async def on_startup(app) -> None:
    db, cfg = app.state.db, app.state.config
    await entry_repo.ensure_indexes(db)
    await customers.ensure_indexes(db)
    await excel_register.ensure_indexes(db)
    await entry_service.ensure_counter(db, cfg)


# ───────────────────────────── access ─────────────────────────────
@dataclass
class Scope:
    user: CurrentUser
    labs: set[str]

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


async def allowed_labs(db: AsyncDatabase, cfg: PortalConfig, user: CurrentUser) -> set[str]:
    """Labs this user may work with: all for Admin or a module-wide grant, else only granted labs."""
    labs = [p.key for p in cfg.labs_for(MODULE)]
    if user.role == "admin":
        return set(labs)
    mod = cfg.module(MODULE)
    visible = (await visibility_map(db, cfg))[MODULE]
    granted = {s for s, st in (await grants_for(db, user.id)).get(MODULE, {}).items() if st == "granted"}
    return {k for k in labs if decide_access(mod, user.role, visible, granted, k)}


# ───────────────────────────── per-user rate limit (render 20/min, email 10/min) ─────────────────────────────
class _Window:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, seconds: float = 60.0) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] >= seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


_limiter = _Window()
RENDER_PER_MINUTE = 20
EMAIL_PER_MINUTE = 10


def check_rate(kind: str, user_id: str, limit: int) -> None:
    if not _limiter.hit(f"{kind}:{user_id}", limit):
        raise BillError(429, "rate_limited", "Too many requests. Please wait a minute and try again.")


# ───────────────────────────── shaping ─────────────────────────────
def entry_out(doc: dict[str, Any], cfg: PortalConfig, *, admin: bool) -> EntryBillOut:
    from app.core.db import as_utc

    p = cfg.partners.get(doc["partner_key"])
    return EntryBillOut(
        id=str(doc["_id"]),
        bill_no=doc.get("bill_no"),
        bill_no_original=doc.get("bill_no_original"),
        status=doc.get("status", "draft"),
        version=doc.get("version", 1),
        partner_key=doc["partner_key"],
        partner_name=p.name if p else doc["partner_key"],
        patient_name=doc["patient_name"],
        age=doc.get("age"),
        age_unit=doc.get("age_unit"),
        gender=doc.get("gender"),
        lab_pid=doc.get("lab_pid"),
        referred_by=doc.get("referred_by"),
        registration_date=date.fromisoformat(doc["registration_date"]),
        tests=doc.get("tests", []),
        test_amounts=[money.to_decimal(a) for a in doc.get("test_amounts", [])],
        gross_amount=money.opt(doc.get("gross_amount")),
        discount_amount=money.opt(doc.get("discount_amount")),
        net_amount=money.opt(doc.get("net_amount")),
        lab_cost=money.opt(doc.get("lab_cost")),
        margin=money.opt(doc.get("margin")) if admin else None,  # admin_only field (fields.yaml)
        client_name=doc.get("client_name"),
        updated_at=as_utc(doc["updated_at"]),
    )


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    """The few fields staff need to recognise an entry in a decision list."""
    return {
        "id": str(doc["_id"]),
        "bill_no": doc.get("bill_no"),
        "patient_name": doc.get("patient_name"),
        "registration_date": doc.get("registration_date"),
    }


def decision_body(d: entry_service.Decision) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(
        {
            "outcome": d.kind,
            "message": d.message,
            "match": summary(d.match) if d.match else None,
            "possible_matches": [summary(e) for e in d.possible],
            "conflicts": d.conflicts,
            "can_force_new": d.can_force_new,
        }
    )


async def publish_saved(
    bus: EventBus, db: AsyncDatabase, cfg: PortalConfig, *, entry: dict[str, Any], months: set[str], action: str, actor_id: str
) -> None:
    await bus.publish(
        "entry.saved",
        {
            "entry_id": str(entry["_id"]),
            "bill_no": entry.get("bill_no"),
            "status": entry.get("status"),
            "months": sorted(months),
            "action": action,
            "actor_id": actor_id,
            "db": db,
            "cfg": cfg,
        },
    )


async def publish_generated(
    bus: EventBus, *, entry: dict[str, Any], delivery: str, actor_id: str
) -> None:
    """`bill.generated`, for audit. Carries ids only, never patient values."""
    await bus.publish(
        "bill.generated",
        {
            "entry_id": str(entry["_id"]),
            "bill_no": entry.get("bill_no"),
            "partner_key": entry["partner_key"],
            "delivery": delivery,
            "actor_id": actor_id,
        },
    )


# ───────────────────────────── input parsing ─────────────────────────────
def parse_money(raw: str | None, label: str) -> Decimal | None:
    if raw is None or not raw.strip():
        return None
    try:
        d = money.to_decimal(raw)
        if d < 0 or d > money.MAX_AMOUNT:
            raise InvalidOperation
    except (InvalidOperation, ValueError, ArithmeticError):
        raise BillError(422, "bad_amount", f"{label}: enter a valid amount (0 or more).") from None
    return d


def parse_amounts(raw: str) -> list[Decimal]:
    """The Metropolis amounts arrive as a JSON list, for example '["450.00"]'."""
    try:
        values = json.loads(raw)
    except ValueError:
        raise BillError(422, "bad_amount", "The amounts must be a list, for example [\"450.00\"].") from None
    if not isinstance(values, list) or not values or len(values) > 60:
        raise BillError(422, "bad_amount", "Enter one amount for every test row.")
    out: list[Decimal] = []
    for v in values:
        amount = parse_money(str(v), "Amount")
        if amount is None:
            raise BillError(422, "bad_amount", "Enter an amount for every test row.")
        out.append(amount)
    return out


# ───────────────────────────── Spectrum ─────────────────────────────
async def spectrum_pdf(
    partner: PartnerCfg,
    entry: dict[str, Any],
    *,
    include_client: bool,
    discount_amount: Decimal | None = None,
) -> tuple[bytes, str]:
    from app.domain.bills.data import build_bill_data

    data = build_bill_data(entry, include_client=include_client, discount_amount=discount_amount)
    mod = bill_module(partner)
    pdf = await asyncio.to_thread(mod.render, data, include_client=include_client, bill_cfg=partner.bill)
    return pdf, data.filename


# ───────────────────────────── Metropolis ─────────────────────────────
def _identity(partner_key: str, lab) -> dict[str, Any]:
    raw = {
        "partner_key": partner_key,
        "patient_name": lab.patient_name,
        "age": lab.age,
        "age_unit": lab.age_unit,
        "gender": lab.gender,
        "lab_pid": lab.patient_no,
        "referred_by": lab.referred_by,
        "registration_date": lab.bill_date.isoformat(),
    }
    return {k: v for k, v in raw.items() if v is not None}


async def _resolve(
    db: AsyncDatabase, cfg: PortalConfig, identity: dict[str, Any], *, entry_id: str | None
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    """Read-only duplicate check. Returns (entry, kind, possible) where kind is exact | possible | none."""
    if entry_id:
        entry = await entry_repo.get_entry(db, entry_id)
        if entry is None:
            raise BillError(404, "entry_not_found", "The chosen entry was not found.")
        if entry["partner_key"] != identity["partner_key"]:
            raise BillError(400, "wrong_lab", "The chosen entry belongs to another laboratory.")
        return entry, "exact", []
    cleaned = {k: v for k, v in clean_fields(identity, cfg, defaults=False).items() if not empty(v)}
    m = await find_matches(db, cfg, cleaned)
    if m.exact:
        return m.exact, "exact", []
    if m.possible:
        return None, "possible", m.possible
    return None, "none", []


def _suggest_amounts(entry: dict[str, Any] | None, lab) -> list[Decimal]:
    """Entry amounts where test names match, otherwise the amounts the lab printed."""
    known: dict[str, Decimal] = {}
    if entry:
        for name, amount in zip(entry.get("tests") or [], entry.get("test_amounts") or [], strict=False):
            known[name.casefold()] = money.to_decimal(amount)
    return [known.get(row.name.casefold(), row.amount) for row in lab.rows]


async def inspect_lab_bill(db: AsyncDatabase, cfg: PortalConfig, partner: PartnerCfg, *, data: bytes) -> dict[str, Any]:
    mod = bill_module(partner)
    lab = await asyncio.to_thread(mod.read_bill, data)
    try:
        entry, kind, possible = await _resolve(db, cfg, _identity(partner.key, lab), entry_id=None)
    except EntryError as exc:
        raise BillError(exc.status, "invalid_bill_data", str(exc.detail)) from None
    owner = await entry_service.bill_no_owner(db, lab.bill_no)
    suggested = _suggest_amounts(entry, lab)
    return {
        "lab_bill_no": lab.bill_no,
        "lab_pid": lab.patient_no,
        "bill_date": lab.bill_date,
        "patient_name": lab.patient_name,
        "age": lab.age,
        "age_unit": lab.age_unit,
        "gender": lab.gender,
        "referred_by": lab.referred_by,
        "rows": [
            {"name": r.name, "printed_amount": r.amount, "amount": s} for r, s in zip(lab.rows, suggested, strict=True)
        ],
        "printed_gross": lab.gross,
        "printed_discount": lab.discount,
        "printed_grand": lab.grand,
        "entry_id": str(entry["_id"]) if entry else None,
        "entry_gross": money.opt(entry.get("gross_amount")) if entry else None,
        "entry_discount": money.opt(entry.get("discount_amount")) if entry else None,
        "entry_lab_cost": money.opt(entry.get("lab_cost")) if entry else None,
        "match": {
            "kind": kind,
            "entries": [summary(e) for e in ([entry] if entry else possible)],
        },
        "bill_no_taken": bool(owner and (entry is None or owner["_id"] != entry["_id"])),
    }


@dataclass
class LabRender:
    pdf: bytes
    filename: str
    entry: dict[str, Any]
    before: dict[str, Any] | None
    months: set[str]
    changed: list[str] = field(default_factory=list)
    outcome: str = "unchanged"


async def render_lab_bill(
    db: AsyncDatabase,
    cfg: PortalConfig,
    partner: PartnerCfg,
    *,
    data: bytes,
    amounts: list[Decimal],
    lab_cost: Decimal | None,
    entry_id: str | None,
    force_new: bool,
    actor_id: str,
) -> LabRender:
    """Order (PRD 10.2): read -> match -> edit in memory -> verify -> save test amounts and lab bill number.
    Nothing is written before the PDF is edited and verified, and a lab number used elsewhere is refused up front."""
    from app.domain.bills.data import bill_filename

    mod = bill_module(partner)
    lab = await asyncio.to_thread(mod.read_bill, data)
    if len(amounts) != len(lab.rows):
        raise BillError(422, "amount_count", "Enter one amount for every test row of the lab bill.")
    identity = _identity(partner.key, lab)
    try:
        entry, kind, possible = await _resolve(db, cfg, identity, entry_id=entry_id)
    except EntryError as exc:
        raise BillError(exc.status, "invalid_bill_data", str(exc.detail)) from None
    if kind == "possible" and not force_new:
        raise BillError(
            409,
            "possible_match",
            "A similar patient is already saved for this lab and date. Choose the entry or 'different patient'.",
            possible_matches=[summary(e) for e in possible],
        )

    gross = money.total(amounts)
    entry_discount = (money.opt(entry.get("discount_amount")) if entry else None) or money.ZERO
    discount = entry_discount if entry_discount > 0 else lab.discount
    if discount > gross:
        raise BillError(422, "discount_too_large", "The discount cannot be more than the total.")
    owner = await entry_service.bill_no_owner(db, lab.bill_no)
    if owner is not None and (entry is None or owner["_id"] != entry["_id"]):
        raise BillError(409, "bill_no_taken", f"Bill No {lab.bill_no} is already used by another entry.")

    pdf = await asyncio.to_thread(mod.edit_bill, data, lab, amounts=amounts, discount=discount, bill_cfg=partner.bill)

    # save: amounts first, then the lab bill number
    values: dict[str, Any] = {"tests": [r.name for r in lab.rows], "test_amounts": amounts}
    if discount > 0:
        values["discount_amount"] = discount
    if lab_cost is not None:
        values["lab_cost"] = lab_cost
    before = entry
    changed: list[str] = []
    try:
        if entry is not None:
            before, entry, changed = await entry_service.update_entry(
                db,
                cfg,
                str(entry["_id"]),
                values,
                module=MODULE,
                actor_id=actor_id,
                expected_version=entry.get("version", 1),
                preserve_financials=True,
            )
            outcome = "merged" if changed else "unchanged"
        else:
            res = await entry_service.upsert(
                db,
                cfg,
                {**identity, **values},
                module=MODULE,
                actor_id=actor_id,
                force_new=force_new,
                preserve_financials=True,
            )
            if res.decision is not None or res.entry is None:
                raise BillError(409, "possible_match", "A similar patient was just saved. Please try again.")
            entry, changed, outcome = res.entry, res.filled, res.outcome
        entry, replaced = await entry_service.replace_bill_no(
            db, cfg, str(entry["_id"]), lab.bill_no, actor_id=actor_id
        )
    except EntryError as exc:
        if isinstance(exc.detail, dict):
            raise BillError(exc.status, exc.detail.get("code", "entry_error"), exc.detail.get("message", "")) from None
        raise BillError(exc.status, "entry_error", str(exc.detail)) from None
    if replaced:
        changed = sorted({*changed, "bill_no", "bill_no_original"})
        outcome = "merged" if outcome == "unchanged" else outcome
    return LabRender(
        pdf=pdf,
        filename=bill_filename(entry["patient_name"], lab.bill_date),
        entry=entry,
        before=before,
        months=entry_service.months_of(before, entry),
        changed=changed,
        outcome=outcome,
    )


# ───────────────────────────── email (only to the signed-in user) ─────────────────────────────
class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


async def send_bill_email(
    mailer: Mailer,
    cfg: PortalConfig,
    *,
    to: str,
    patient_name: str,
    test_date: date,
    lab_name: str,
    bill_no: str | None,
    pdf: bytes,
    filename: str,
) -> None:
    display_date = test_date.strftime("%d %B %Y")
    values = _Safe(
        patient_name=patient_name,
        test_date=display_date,
        lab_name=lab_name,
        brand=cfg.app.brand.name,
        bill_no=bill_no or "",
    )
    await mailer.send_email(
        OutboundEmail(
            to=to,
            subject=cfg.delivery.email.subject.format_map(values),
            text=cfg.delivery.email.body.format_map(values),
            header_highlights=(
                ("Patient", patient_name),
                ("Test date", display_date),
                ("Laboratory", lab_name),
            ),
            attachments=(EmailAttachment(filename=filename, content=pdf, content_type="application/pdf"),),
        )
    )


def clean_name(value: Any) -> str:
    return std.clean_text(value)