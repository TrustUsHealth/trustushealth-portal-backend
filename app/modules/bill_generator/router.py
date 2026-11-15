"""`/api/v1/bill_generator`: Spectrum bills from fixed layouts, Metropolis bills edited in place. Nothing is stored."""

import asyncio
import logging
from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.core import audit
from app.core.config import PartnerCfg, PortalConfig
from app.core.deps import CurrentUser, get_config, get_current_user, get_db
from app.core.mailer import EmailDeliveryError
from app.core.rbac import require_access
from app.domain.bills.errors import BillError
from app.domain.entries import repository as entry_repo
from app.domain.entries import service as entry_service
from app.domain.entries.dedupe import find_matches
from app.domain.entries.model import EDITABLE, EntryError
from app.domain.parsing import ParseError, UnreadableFile, UploadError, parse_report, safe_filename, validate_upload
from app.modules.bill_generator import service
from app.modules.bill_generator.schemas import (
    BillDataPatchIn,
    Delivery,
    EntryBillOut,
    LabInspectOut,
    LabOut,
    ManualEntryIn,
    ParsedOut,
    SaveOut,
    SentOut,
    SpectrumRenderIn,
)
from app.modules.bill_generator.service import MODULE

log = logging.getLogger(__name__)
router = APIRouter(tags=["bill_generator"])

_module_access = Depends(require_access(MODULE))


async def lab_scope(
    user: CurrentUser = _module_access, db=Depends(get_db), cfg: PortalConfig = Depends(get_config)
) -> service.Scope:
    labs = await service.allowed_labs(db, cfg, user)
    if not labs:
        raise HTTPException(status_code=403, detail="You do not have access to any lab in this module.")
    return service.Scope(user=user, labs=labs)


async def render_limit(user: CurrentUser = Depends(get_current_user)) -> None:
    try:
        service.check_rate("render", user.id, service.RENDER_PER_MINUTE)
    except BillError as exc:
        raise _fail(exc) from None


def _fail(exc: BillError | EntryError | UploadError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _partner(cfg: PortalConfig, scope: service.Scope, key: str, mode: str) -> PartnerCfg:
    p = cfg.partners.get((key or "").casefold())
    if p is None or MODULE not in p.enabled_in or p.bill is None or p.bill.mode != mode:
        raise HTTPException(status_code=422, detail="Please choose a valid laboratory.")
    if p.key not in scope.labs:
        raise HTTPException(status_code=403, detail="You do not have access to this lab.")
    return p


async def _entry_or_404(db, scope: service.Scope, entry_id: str) -> dict:
    doc = await entry_repo.get_entry(db, entry_id)
    if doc is None or doc["partner_key"] not in scope.labs:  # never reveal entries of labs you cannot open
        raise HTTPException(status_code=404, detail="Entry not found.")
    return doc


async def _read_upload(file: UploadFile, cfg: PortalConfig) -> bytes:
    max_bytes = cfg.app.upload.max_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)  # never read more than the limit
    try:
        validate_upload(safe_filename(file.filename, "bill.pdf"), data, ["pdf"], max_bytes)
    except UploadError as exc:
        raise _fail(exc) from None
    return data


def _pdf(pdf: bytes, filename: str, *, inline: bool) -> Response:
    disposition = "inline" if inline else "attachment"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _deliver(
    request: Request,
    scope: service.Scope,
    cfg: PortalConfig,
    *,
    entry: dict,
    pdf: bytes,
    filename: str,
    delivery: Delivery,
) -> Response:
    """Preview / download return the PDF; email goes only to the signed-in user. Nothing is kept afterwards."""
    user = scope.user
    if delivery == "email":
        try:
            service.check_rate("email", user.id, service.EMAIL_PER_MINUTE)
            await service.send_bill_email(
                request.app.state.mailer,
                cfg,
                to=user.email,
                patient_name=entry["patient_name"],
                test_date=date.fromisoformat(
                    (entry.get("collection_time") or entry["registration_date"])[:10]
                ),
                lab_name=cfg.partners[entry["partner_key"]].name,
                bill_no=entry.get("bill_no"),
                pdf=pdf,
                filename=filename,
            )
        except BillError as exc:
            raise _fail(exc) from None
        except EmailDeliveryError:
            raise HTTPException(status_code=502, detail="The email could not be sent. Please try again later.") from None
    await audit.record(
        request,
        "bill.email" if delivery == "email" else "bill.generate",
        actor=user,
        module=MODULE,
        submodule=entry["partner_key"],
        target=entry.get("bill_no"),
    )
    await service.publish_generated(request.app.state.events, entry=entry, delivery=delivery, actor_id=user.id)
    if delivery == "email":
        return JSONResponse(SentOut().model_dump())
    return _pdf(pdf, filename, inline=delivery == "preview")


# ───────────────────────────── labs and entries ─────────────────────────────
@router.get("/labs", response_model=list[LabOut])
async def list_labs(scope: service.Scope = Depends(lab_scope), cfg: PortalConfig = Depends(get_config)):
    return [
        LabOut(key=p.key, name=p.name, mode=p.bill.mode)
        for p in cfg.labs_for(MODULE)
        if p.key in scope.labs and p.bill is not None
    ]


@router.get("/entries/{entry_id}/bill", response_model=EntryBillOut)
async def get_bill_prefill(
    entry_id: str, scope: service.Scope = Depends(lab_scope), db=Depends(get_db), cfg: PortalConfig = Depends(get_config)
) -> EntryBillOut:
    """Prefill for the bill page: entry fields, test amounts, client name."""
    doc = await _entry_or_404(db, scope, entry_id)
    return service.entry_out(doc, cfg, admin=scope.is_admin)


@router.put("/entries/{entry_id}/bill-data", response_model=EntryBillOut)
async def save_bill_data(
    entry_id: str,
    body: BillDataPatchIn,
    request: Request,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> EntryBillOut:
    """Save bill test amounts and client name; existing financial fields remain unchanged."""
    await _entry_or_404(db, scope, entry_id)
    patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    try:
        before, after, changed = await entry_service.update_entry(
            db,
            cfg,
            entry_id,
            patch,
            module=MODULE,
            actor_id=scope.user.id,
            expected_version=body.expected_version,
            preserve_financials=True,
        )
    except EntryError as exc:
        raise _fail(exc) from None
    if changed:
        await audit.record(
            request,
            "entry.update",
            actor=scope.user,
            module=MODULE,
            submodule=after["partner_key"],
            target=after.get("bill_no"),
            fields_changed=changed,
        )
        await service.publish_saved(
            request.app.state.events,
            db,
            cfg,
            entry=after,
            months=entry_service.months_of(before, after),
            action="updated",
            actor_id=scope.user.id,
        )
    return service.entry_out(after, cfg, admin=scope.is_admin)


@router.post("/entries", response_model=SaveOut, status_code=201)
async def create_entry_for_bill(
    body: ManualEntryIn,
    request: Request,
    response: Response,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
):
    """Manual entry for a generated bill (Spectrum). The duplicate check runs first: an exact match is updated
    (201 -> 200); a possible match or field conflict returns 409 with what staff must decide."""
    partner = _partner(cfg, scope, body.partner_key, "generate")
    raw = body.model_dump(exclude_none=True, exclude={"target_entry_id", "force_new", "resolutions", "partner_key"})
    raw = {k: v for k, v in raw.items() if k in EDITABLE}
    raw["partner_key"] = partner.key
    try:
        res = await entry_service.upsert(
            db,
            cfg,
            raw,
            module=MODULE,
            actor_id=scope.user.id,
            target_entry_id=body.target_entry_id,
            force_new=body.force_new,
            resolutions=dict(body.resolutions),
            preserve_financials=True,
        )
    except EntryError as exc:
        raise _fail(exc) from None
    if res.decision is not None:
        return JSONResponse(status_code=409, content=service.decision_body(res.decision))
    entry = res.entry
    action = {"created": "entry.create_distinct" if body.force_new else "entry.create"}.get(res.outcome, "entry.merge")
    await audit.record(
        request,
        action,
        actor=scope.user,
        module=MODULE,
        submodule=partner.key,
        target=entry.get("bill_no"),
        fields_changed=res.filled or sorted(raw),
    )
    if res.outcome != "unchanged":
        await service.publish_saved(
            request.app.state.events, db, cfg, entry=entry, months=res.months, action=res.outcome, actor_id=scope.user.id
        )
    if res.outcome != "created":
        response.status_code = 200
    return SaveOut(
        outcome=res.outcome,  # type: ignore[arg-type]
        entry=service.entry_out(entry, cfg, admin=scope.is_admin),
        filled_fields=res.filled,
    )


# ───────────────────────────── Spectrum ─────────────────────────────
@router.post("/spectrum/parse", response_model=ParsedOut, dependencies=[Depends(render_limit)])
async def parse_spectrum_report(
    request: Request,
    file: Annotated[UploadFile, File()],
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> ParsedOut:
    """Read a Spectrum report in memory to start a bill. Nothing is stored (reports are stored by `reports`)."""
    partner = _partner(cfg, scope, "spectrum", "generate")
    max_bytes = cfg.app.upload.max_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    filename = safe_filename(file.filename)
    try:
        validate_upload(filename, data, list(cfg.app.upload.allowed), max_bytes)
        result = await asyncio.wait_for(asyncio.to_thread(parse_report, data, filename, partner, cfg), timeout=25)
    except UploadError as exc:
        raise _fail(exc) from None
    except TimeoutError:
        raise HTTPException(status_code=422, detail="Reading this file took too long. Please enter the details manually.") from None
    except UnreadableFile as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    fields = {k: v for k, v in result.fields.items() if k in EDITABLE and v not in (None, "", [])}
    kind, ids = None, []
    if fields.get("patient_name") and fields.get("registration_date"):
        m = await find_matches(db, cfg, {**fields, "partner_key": partner.key})
        if m.exact:
            kind, ids = "exact", [str(m.exact["_id"])]
        elif m.possible:
            kind, ids = "possible", [str(e["_id"]) for e in m.possible]
    await audit.record(request, "bill.parse", actor=scope.user, module=MODULE, submodule=partner.key)
    return ParsedOut(
        partner_key=partner.key,
        fields=jsonable_encoder(fields),
        duplicate_kind=kind,  # type: ignore[arg-type]
        duplicate_entry_ids=ids,
    )


@router.post("/spectrum/render", response_model=None, dependencies=[Depends(render_limit)])
async def render_spectrum_bill(
    body: SpectrumRenderIn,
    request: Request,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
):
    """Render from the entry with an optional bill-only discount override; no override is stored."""
    partner = _partner(cfg, scope, "spectrum", "generate")
    entry = await _entry_or_404(db, scope, body.entry_id)
    if entry["partner_key"] != partner.key:
        raise HTTPException(status_code=404, detail="Entry not found.")
    try:
        pdf, filename = await service.spectrum_pdf(
            partner,
            entry,
            include_client=body.include_client,
            discount_amount=body.bill_discount_amount,
        )
    except BillError as exc:
        raise _fail(exc) from None
    return await _deliver(request, scope, cfg, entry=entry, pdf=pdf, filename=filename, delivery=body.delivery)


# ───────────────────────────── Metropolis ─────────────────────────────
@router.post("/metropolis/inspect", response_model=LabInspectOut)
async def inspect_metropolis_bill(
    request: Request,
    file: Annotated[UploadFile, File()],
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> LabInspectOut:
    """Read the lab's bill in memory: lab bill no., rows, totals and the entry it matches. Nothing is stored."""
    partner = _partner(cfg, scope, "metropolis", "edit_pdf")
    data = await _read_upload(file, cfg)
    try:
        out = await service.inspect_lab_bill(db, cfg, partner, data=data)
    except BillError as exc:
        raise _fail(exc) from None
    await audit.record(request, "bill.inspect", actor=scope.user, module=MODULE, submodule=partner.key)
    return LabInspectOut(**out)


@router.post("/metropolis/render", response_model=None, dependencies=[Depends(render_limit)])
async def render_metropolis_bill(
    request: Request,
    file: Annotated[UploadFile, File()],
    amounts: Annotated[str, Form(description='JSON list, one amount per row of the lab bill, e.g. ["450.00"]')],
    delivery: Annotated[Delivery, Form()] = "preview",
    entry_id: Annotated[str | None, Form()] = None,
    force_new: Annotated[bool, Form()] = False,
    lab_cost: Annotated[str | None, Form()] = None,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
):
    """Edit the lab's own PDF in memory (only the permitted cells), verify, save test amounts and the lab bill
    number on the entry without changing its financial totals, then return the PDF or email it."""
    partner = _partner(cfg, scope, "metropolis", "edit_pdf")
    data = await _read_upload(file, cfg)
    try:
        result = await service.render_lab_bill(
            db,
            cfg,
            partner,
            data=data,
            amounts=service.parse_amounts(amounts),
            lab_cost=service.parse_money(lab_cost, "Lab cost"),
            entry_id=entry_id,
            force_new=force_new,
            actor_id=scope.user.id,
        )
    except BillError as exc:
        raise _fail(exc) from None
    except EntryError as exc:
        raise _fail(exc) from None
    if result.changed:
        await audit.record(
            request,
            "entry.update",
            actor=scope.user,
            module=MODULE,
            submodule=partner.key,
            target=result.entry.get("bill_no"),
            fields_changed=result.changed,
        )
        await service.publish_saved(
            request.app.state.events,
            db,
            cfg,
            entry=result.entry,
            months=result.months,
            action=result.outcome,
            actor_id=scope.user.id,
        )
    return await _deliver(
        request, scope, cfg, entry=result.entry, pdf=result.pdf, filename=result.filename, delivery=delivery
    )