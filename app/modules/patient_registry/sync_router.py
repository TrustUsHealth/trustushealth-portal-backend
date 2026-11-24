"""Spectrum sync endpoints, mounted under `/api/v1/patient_registry` by router.py.

POST /sync/spectrum/run        Admin: start a sync for a month / date range (background, returns 202)
GET  /sync/spectrum/runs       Admin: recent runs      GET /sync/spectrum/runs/{id}  one run
GET  /sync/spectrum/items      Admin: ledger rows (filter by status, e.g. failed / needs_review)
POST /jobs/spectrum_sync       External cron (X-Job-Secret): same as the daily scheduled run, safe to retry
"""

from datetime import date
from typing import Annotated

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.core import audit
from app.core.config import PortalConfig
from app.core.db import as_utc
from app.core.deps import CurrentUser, get_config, get_db, require_role
from app.core.security import rate_limit, safe_equal
from app.modules.patient_registry import spectrum_sync
from app.modules.patient_registry.schemas import JobOut  # noqa: F401  (kept for parity with the other job route)
from app.modules.patient_registry.sync_schemas import (
    SyncItemListOut,
    SyncItemOut,
    SyncRunIn,
    SyncRunOut,
    SyncRunStartedOut,
    SyncStatsOut,
)

MODULE = "patient_registry"
router = APIRouter()
_job_limit = Depends(rate_limit("pr_sync_job", 10, 60))
_run_limit = Depends(rate_limit("pr_sync_run", 10, 60))


async def require_sync_job_secret(request: Request, x_job_secret: Annotated[str | None, Header()] = None) -> None:
    secret = request.app.state.settings.job_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Scheduled jobs are not configured (JOB_SECRET).")
    if not x_job_secret or not safe_equal(x_job_secret, secret):
        await audit.record(request, "job.denied", module=MODULE, outcome="denied")
        raise HTTPException(status_code=401, detail="Invalid job secret.")


async def _start(
    request: Request,
    *,
    trigger: str,
    month: str | None,
    date_from: date | None,
    date_to: date | None,
    dry_run: bool,
    retry_failed: bool,
) -> SyncRunStartedOut:
    ctx = spectrum_sync.context_from_app(request.app)
    try:
        d_from, d_to = spectrum_sync.resolve_window(
            ctx.cfg, ctx.sync_cfg, month=month, date_from=date_from, date_to=date_to
        )
        run_id = await spectrum_sync.launch(ctx, spectrum_sync.RunParams(d_from, d_to, trigger, dry_run, retry_failed))
    except spectrum_sync.SyncInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except spectrum_sync.SyncBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except spectrum_sync.SyncUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return SyncRunStartedOut(run_id=run_id, date_from=d_from, date_to=d_to, dry_run=dry_run)


@router.post(
    "/sync/spectrum/run", response_model=SyncRunStartedOut, status_code=202, dependencies=[_run_limit]
)
async def spectrum_sync_run(
    body: SyncRunIn, request: Request, user: CurrentUser = Depends(require_role("admin"))
) -> SyncRunStartedOut:
    """Start a Spectrum sync for any month or range (for example `{"month": "2026-10"}`). Runs in the background."""
    out = await _start(
        request,
        trigger="manual",
        month=body.month,
        date_from=body.date_from,
        date_to=body.date_to,
        dry_run=body.dry_run,
        retry_failed=body.retry_failed,
    )
    await audit.record(
        request, "spectrum_sync.start", actor=user, module=MODULE, submodule="spectrum", target=out.run_id
    )
    return out


@router.post(
    "/jobs/spectrum_sync",
    response_model=SyncRunStartedOut,
    status_code=202,
    dependencies=[_job_limit, Depends(require_sync_job_secret)],
)
async def run_spectrum_sync_job(
    request: Request,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    dry_run: bool = False,
    retry_failed: bool = False,
) -> SyncRunStartedOut:
    """Daily job for an external cron (X-Job-Secret). Without `month` it reads today and the lookback days."""
    out = await _start(
        request, trigger="job", month=month, date_from=None, date_to=None, dry_run=dry_run, retry_failed=retry_failed
    )
    await audit.record(request, "spectrum_sync.job", module=MODULE, submodule="spectrum", target=out.run_id)
    return out


def _run_out(doc: dict) -> SyncRunOut:
    stats = doc.get("stats") or {}
    return SyncRunOut(
        id=str(doc["_id"]),
        trigger=doc.get("trigger", ""),
        status=doc.get("status", ""),
        dry_run=bool(doc.get("dry_run")),
        date_from=date.fromisoformat(doc["date_from"]),
        date_to=date.fromisoformat(doc["date_to"]),
        started_at=as_utc(doc["started_at"]),
        finished_at=as_utc(doc["finished_at"]) if doc.get("finished_at") else None,
        message=doc.get("message"),
        stats=SyncStatsOut(**{k: v for k, v in stats.items() if k in SyncStatsOut.model_fields}),
    )


@router.get("/sync/spectrum/runs", response_model=list[SyncRunOut])
async def spectrum_sync_runs(
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    user: CurrentUser = Depends(require_role("admin")),
    db=Depends(get_db),
) -> list[SyncRunOut]:
    docs = await db[spectrum_sync.RUNS].find({}).sort("started_at", -1).limit(limit).to_list(length=limit)
    return [_run_out(d) for d in docs]


@router.get("/sync/spectrum/runs/{run_id}", response_model=SyncRunOut)
async def spectrum_sync_run_detail(
    run_id: str, user: CurrentUser = Depends(require_role("admin")), db=Depends(get_db)
) -> SyncRunOut:
    try:
        doc = await db[spectrum_sync.RUNS].find_one({"_id": ObjectId(run_id)})
    except InvalidId:
        doc = None
    if doc is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _run_out(doc)


@router.get("/sync/spectrum/items", response_model=SyncItemListOut)
async def spectrum_sync_items(
    status: Annotated[str | None, Query(pattern="^(done|failed|needs_review|processing)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    user: CurrentUser = Depends(require_role("admin")),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> SyncItemListOut:
    """Rows the sync could not finish (`failed`) or wants a person to look at (`needs_review`)."""
    flt = {"status": status} if status else {}
    docs = await db[spectrum_sync.LEDGER].find(flt).sort("updated_at", -1).limit(limit).to_list(length=limit)
    total = await db[spectrum_sync.LEDGER].count_documents(flt)
    items = [
        SyncItemOut(
            source_key=d["source_key"],
            sid_no=d.get("sid_no", ""),
            sid_date=date.fromisoformat(d["sid_date"]),
            status=d.get("status", ""),
            outcome=d.get("outcome"),
            attempts=d.get("attempts", 0),
            entry_id=d.get("entry_id"),
            report_ref=d.get("report_ref"),
            last_error=d.get("last_error"),
            warnings=d.get("warnings", []),
            updated_at=as_utc(d["updated_at"]) if d.get("updated_at") else None,
        )
        for d in docs
    ]
    return SyncItemListOut(items=items, total=total)