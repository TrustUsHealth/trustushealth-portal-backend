"""`/api/v1/patient_registry`: entries (list, parse, create/merge, edit), customers, Excel register, monthly job."""

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.core import audit
from app.core.config import PartnerCfg, PortalConfig
from app.core.deps import CurrentUser, get_config, get_db, get_settings, require_role
from app.core.filestore import FileStore, FileStoreError, get_filestore
from app.core.mailer import EmailDeliveryError
from app.core.rbac import require_access
from app.core.security import rate_limit, safe_equal
from app.domain import excel_register
from app.domain.customers import service as customers
from app.domain.entries import repository as entry_repo
from app.domain.entries import service as entry_service
from app.domain.entries.model import EntryError
from app.domain.parsing import UploadError
from app.domain.reports import service as reports
from app.modules.patient_registry import service
from app.modules.patient_registry.schemas import (
    CustomerDetailOut,
    CustomerListOut,
    CustomerOut,
    DuplicateDecisionOut,
    EntryCreateIn,
    EntryDetailOut,
    EntryListOut,
    EntryOut,
    EntryPatchIn,
    FieldOut,
    JobOut,
    ParseOut,
    SaveOut,
    SendOut,
    WorkbookOut,
)
from app.modules.patient_registry.service import MODULE

log = logging.getLogger(__name__)
router = APIRouter(tags=["patient_registry"])

_module_access = Depends(require_access(MODULE))
_excel_access = Depends(require_access(MODULE, "excel_register"))
_admin = Depends(require_role("admin"))
_parse_limit = Depends(rate_limit("pr_parse", 30, 60))
_job_limit = Depends(rate_limit("pr_job", 10, 60))


async def lab_scope(
    user: CurrentUser = _module_access, db=Depends(get_db), cfg: PortalConfig = Depends(get_config)
) -> service.Scope:
    labs = await service.allowed_labs(db, cfg, user)
    if not labs:
        raise HTTPException(status_code=403, detail="You do not have access to any lab in this module.")
    return service.Scope(user=user, labs=labs)


def _fail(exc: EntryError | UploadError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _lab(cfg: PortalConfig, scope: service.Scope, key: str) -> PartnerCfg:
    p = cfg.partners.get((key or "").casefold())
    if p is None or MODULE not in p.enabled_in:
        raise HTTPException(status_code=422, detail="Please choose a valid laboratory.")
    if p.key not in scope.labs:
        raise HTTPException(status_code=403, detail="You do not have access to this lab.")
    return p


async def _entry_or_404(db, scope: service.Scope, entry_id: str) -> dict:
    doc = await entry_repo.get_entry(db, entry_id)
    if doc is None or doc["partner_key"] not in scope.labs:  # never reveal entries of labs you cannot open
        raise HTTPException(status_code=404, detail="Entry not found.")
    return doc


# ───────────────────────────── field list (drives the form from fields.yaml) ─────────────────────────────
@router.get("/fields", response_model=list[FieldOut])
async def list_fields(scope: service.Scope = Depends(lab_scope), cfg: PortalConfig = Depends(get_config)):
    return [
        FieldOut(
            key=f.key,
            label=f.label,
            type=f.type,
            options=f.options,
            required=MODULE in f.required_in,
            default=f.default,
            auto=f.auto,
            computed=f.computed,
            internal=f.internal,
        )
        for f in cfg.fields
        if scope.is_admin or not f.admin_only
    ]


# ───────────────────────────── entries ─────────────────────────────
@router.get("/entries", response_model=EntryListOut)
async def list_entries(
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    q: Annotated[str | None, Query(max_length=80)] = None,
    partner_key: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[str | None, Query(pattern="^(draft|complete)$")] = None,
    referred_by: Annotated[str | None, Query(max_length=80)] = None,
    date_from: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    date_to: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    bill_no: Annotated[str | None, Query(max_length=12)] = None,
    lab_pid: Annotated[str | None, Query(max_length=40)] = None,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> EntryListOut:
    docs, total = await entry_repo.search(
        db,
        cfg,
        labs=scope.labs,
        q=q,
        partner_key=partner_key,
        status=status,
        referred_by=referred_by,
        date_from=date_from,
        date_to=date_to,
        bill_no=bill_no,
        lab_pid=lab_pid,
        page=page,
        page_size=page_size,
    )
    return EntryListOut(items=[service.summary_out(d) for d in docs], total=total, page=page, page_size=page_size)


@router.post("/entries/parse", response_model=ParseOut, dependencies=[_parse_limit])
async def parse_entry_report(
    request: Request,
    partner_key: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    store: FileStore = Depends(get_filestore),
) -> ParseOut:
    """Lab first, then upload. Reads the report with that lab's rules and stores it; the form is pre-filled from
    `fields` (low-confidence ones listed in `needs_verify`). Nothing is saved as an entry yet."""
    partner = _lab(cfg, scope, partner_key)
    if partner.parser == "manual":
        raise HTTPException(status_code=400, detail=f"{partner.name} reports are entered manually.")
    max_bytes = cfg.app.upload.max_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)  # never read more than the limit
    try:
        out = await service.parse_and_store(
            db, cfg, store, partner=partner, filename=file.filename or "report", data=data, actor_id=scope.user.id
        )
    except UploadError as exc:
        raise _fail(exc) from None
    except FileStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    await audit.record(
        request, "report.upload", actor=scope.user, module=MODULE, submodule=partner.key, target=out.report_id
    )
    return out


@router.post(
    "/entries",
    response_model=SaveOut,
    status_code=201,
    responses={200: {"model": SaveOut}, 409: {"model": DuplicateDecisionOut}},
)
async def create_entry(
    body: EntryCreateIn,
    request: Request,
    response: Response,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
):
    """Save a new entry. The duplicate check runs first: an exact match is updated instead (201 -> 200), a possible
    match or field conflict returns 409 with what staff must decide."""
    partner = _lab(cfg, scope, body.partner_key)
    raw = service.input_fields(body.model_dump(mode="json", exclude_none=True))
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
        )
    except EntryError as exc:
        raise _fail(exc) from None
    if res.decision is not None:
        return JSONResponse(status_code=409, content=service.decision_out(res.decision).model_dump(mode="json"))

    entry = res.entry
    link = "none"
    if body.report_id:
        link = await reports.link_entry(db, body.report_id, str(entry["_id"]), entry["partner_key"])
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
        report_link=link,  # type: ignore[arg-type]
    )


@router.get("/entries/{entry_id}", response_model=EntryDetailOut)
async def get_entry_detail(
    entry_id: str,
    request: Request,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> EntryDetailOut:
    doc = await _entry_or_404(db, scope, entry_id)
    found = await reports.reports_for_entry(db, doc)
    await audit.record(
        request, "entry.view", actor=scope.user, module=MODULE, submodule=doc["partner_key"], target=doc.get("bill_no")
    )
    return EntryDetailOut(
        **service.entry_out(doc, cfg, admin=scope.is_admin).model_dump(),
        reports=[service.report_ref_out(r) for r in found],
    )


@router.patch("/entries/{entry_id}", response_model=EntryOut)
async def update_entry_fields(
    entry_id: str,
    body: EntryPatchIn,
    request: Request,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> EntryOut:
    """Edit the same record: same row in Excel, same bill_no. Send null to clear an optional field."""
    await _entry_or_404(db, scope, entry_id)
    patch = service.input_fields(body.model_dump(mode="json", exclude_unset=True))
    try:
        before, after, changed = await entry_service.update_entry(
            db, cfg, entry_id, patch, module=MODULE, actor_id=scope.user.id, expected_version=body.expected_version
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


@router.delete("/entries/{entry_id}", status_code=204, dependencies=[_admin])
async def delete_entry_record(
    entry_id: str,
    request: Request,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> Response:
    """Admin only. The bill number stays retired (never reused)."""
    try:
        doc = await entry_service.delete(db, entry_id)
    except EntryError as exc:
        raise _fail(exc) from None
    await audit.record(
        request, "entry.delete", actor=scope.user, module=MODULE, submodule=doc["partner_key"], target=doc.get("bill_no")
    )
    await service.publish_saved(
        request.app.state.events,
        db,
        cfg,
        entry=doc,
        months=entry_service.months_of(doc),
        action="deleted",
        actor_id=scope.user.id,
    )
    return Response(status_code=204)


# ───────────────────────────── stored report file (preview / download) ─────────────────────────────
@router.get("/reports/{report_id}/file")
async def download_report_file(
    report_id: str,
    request: Request,
    download: bool = False,
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    store: FileStore = Depends(get_filestore),
) -> Response:
    meta = await reports.get_meta(db, report_id)
    if meta is None or meta["partner_key"] not in scope.labs:
        raise HTTPException(status_code=404, detail="Report not found.")
    stored = await reports.read_file(db, store, report_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Report file not found.")
    inline = stored.content_type == "application/pdf" and not download
    await audit.record(
        request,
        "report.view" if inline else "report.download",
        actor=scope.user,
        module=MODULE,
        submodule=meta["partner_key"],
        target=report_id,
    )
    disposition = "inline" if inline else "attachment"
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(stored.name)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ───────────────────────────── customers ─────────────────────────────
def _customer_out(c: dict) -> CustomerOut:
    return CustomerOut(
        id=str(c["_id"]),
        name=c["name"],
        age=c.get("age"),
        age_unit=c.get("age_unit"),
        gender=c.get("gender"),
        phone=c.get("phone"),
        email=c.get("email"),
        created_at=service.as_utc(c["created_at"]),
    )


@router.get("/customers", response_model=CustomerListOut)
async def list_customers_search(
    scope: service.Scope = Depends(lab_scope),
    db=Depends(get_db),
    q: Annotated[str | None, Query(max_length=80)] = None,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> CustomerListOut:
    docs, total = await customers.list_customers(db, q=q, page=page, page_size=page_size)
    return CustomerListOut(items=[_customer_out(c) for c in docs], total=total, page=page, page_size=page_size)


@router.get("/customers/{customer_id}", response_model=CustomerDetailOut)
async def get_customer_detail(
    customer_id: str, scope: service.Scope = Depends(lab_scope), db=Depends(get_db)
) -> CustomerDetailOut:
    c = await customers.get_customer(db, customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    cursor = (
        db[entry_repo.COL]
        .find({"customer_id": customer_id, "partner_key": {"$in": sorted(scope.labs)}})
        .sort([("registration_date", -1), ("bill_no", -1)])
        .limit(200)
    )
    return CustomerDetailOut(
        **_customer_out(c).model_dump(), entries=[service.summary_out(e) for e in await cursor.to_list(length=200)]
    )


# ───────────────────────────── Excel register ─────────────────────────────
@router.get("/excel_register", response_model=list[WorkbookOut], dependencies=[_excel_access])
async def list_workbook_years(db=Depends(get_db)) -> list[WorkbookOut]:
    return [WorkbookOut(**w) for w in await excel_register.list_workbooks(db)]


@router.get("/excel_register/download", dependencies=[_excel_access])
async def download_workbook(
    request: Request,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    user: CurrentUser = _excel_access,
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> Response:
    """'Download now': regenerated from the database first, so the file is always exact."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = year or datetime.now(ZoneInfo(cfg.app.timezone)).year
    meta, content = await excel_register.get_workbook(db, cfg, year, refresh=True)
    await audit.record(request, "excel.download", actor=user, module=MODULE, submodule="excel_register", target=str(year))
    return Response(
        content=content,
        media_type=excel_register.XLSX,
        headers={"Content-Disposition": f'attachment; filename="{meta["filename"]}"', "Cache-Control": "private, no-store"},
    )


@router.post("/excel_register/send", response_model=SendOut, dependencies=[_admin])
async def send_workbook_now(
    request: Request,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    user: CurrentUser = Depends(require_role("admin")),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    settings=Depends(get_settings),
) -> SendOut:
    """Admin 'Send now': emails the current workbook to the configured recipients."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = year or datetime.now(ZoneInfo(cfg.app.timezone)).year
    try:
        sent = await excel_register.send_now(db, cfg, settings, request.app.state.mailer, year)
    except EmailDeliveryError:
        raise HTTPException(status_code=502, detail="The email could not be sent. Please try again later.") from None
    await audit.record(request, "excel.send", actor=user, module=MODULE, submodule="excel_register", target=str(year))
    return SendOut(sent=len(sent))


async def require_job_secret(request: Request, x_job_secret: Annotated[str | None, Header()] = None) -> None:
    secret = request.app.state.settings.job_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Scheduled jobs are not configured (JOB_SECRET).")
    if not x_job_secret or not safe_equal(x_job_secret, secret):
        await audit.record(request, "job.denied", module=MODULE, outcome="denied")
        raise HTTPException(status_code=401, detail="Invalid job secret.")


@router.post("/jobs/excel_monthly", response_model=JobOut, dependencies=[_job_limit, Depends(require_job_secret)])
async def run_monthly_excel_job(
    request: Request, db=Depends(get_db), cfg: PortalConfig = Depends(get_config), settings=Depends(get_settings)
) -> JobOut:
    """Called by the GitHub Actions cron on the 1st at 06:00 IST. Safe to retry: a month is never sent twice."""
    result = await excel_register.monthly_send(db, cfg, settings, request.app.state.mailer)
    await audit.record(request, "excel.monthly_send", module=MODULE, submodule="excel_register", target=result["month"])
    if result["status"] == "partial":
        raise HTTPException(status_code=502, detail="Some recipients could not be emailed; retry the job.")
    return JobOut(**result)