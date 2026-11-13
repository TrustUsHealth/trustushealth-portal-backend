"""Business Insights API (`/api/v1/insights`). Admin only: every data route sits behind `require_role("admin")`, so
Staff and Engineer get 403 even if an old grant exists. One cached analysis feeds all four tabs and the PDF."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core import audit
from app.core.config import PortalConfig
from app.core.deps import CurrentUser, get_config, get_db, get_settings, require_role
from app.core.security import decode_access_token, safe_equal
from app.modules.insights import compute as K
from app.modules.insights import monthly, report, service
from app.modules.insights.schemas import (
    BelowCostOut,
    FinanceOut,
    FiltersOut,
    LabsOut,
    OverviewOut,
    PatientsOut,
)

MODULE = "insights"
router = APIRouter(tags=["insights"])
admin = Depends(require_role("admin"))
PDF_PER_MINUTE = 6

Period = Literal["this_month", "last_month", "last_3_months", "this_fy", "last_fy", "custom"]
Compare = Literal["previous", "last_year", "none"]


@dataclass
class Common:
    params: service.Params
    refresh: bool


async def common(
    cfg: PortalConfig = Depends(get_config),
    period: Period | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    labs: str | None = Query(None, description="Comma-separated lab keys; empty means all labs"),
    compare: Compare | None = None,
    refresh: bool = False,
) -> Common:
    try:
        params = service.resolve_params(cfg, period=period, start=date_from, end=date_to, labs=labs, compare=compare)
    except service.InsightsError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return Common(params, refresh)


async def _open_once(request: Request, user: CurrentUser, tab: str) -> None:
    """Audit the first open of Insights per sign-in session (names only, never values)."""
    auth = request.headers.get("authorization", "")
    claims = decode_access_token(auth.removeprefix("Bearer ").strip(), request.app.state.settings.jwt_secret)
    sid = (claims or {}).get("sid") or user.id
    seen: set[str] | None = getattr(request.app.state, "insights_seen", None)
    if seen is None:
        seen = request.app.state.insights_seen = set()
    if sid not in seen:
        seen.add(sid)
        await audit.record(request, "insights.open", actor=user, module=MODULE, submodule=tab)


async def _analysis(request: Request, user: CurrentUser, c: Common, tab: str, db, cfg):
    await _open_once(request, user, tab)
    return await service.analysis(db, cfg, c.params, refresh=c.refresh)


@router.get("/filters", response_model=FiltersOut)
async def filters(
    request: Request, user: CurrentUser = admin, db=Depends(get_db), cfg: PortalConfig = Depends(get_config)
) -> dict:
    await _open_once(request, user, "filters")
    ins = cfg.insights
    return K.filters_payload(
        ins, service.today_local(cfg), cfg.app.fiscal_year_start_month, service.lab_infos(cfg),
        await service.earliest_date(db), ins.chart_palette.others, ins.chart_palette.compare_line,
    )


@router.get("/overview", response_model=OverviewOut)
async def overview(
    request: Request, c: Common = Depends(common), user: CurrentUser = admin, db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> dict:
    return K.build_overview(await _analysis(request, user, c, "overview", db, cfg))


@router.get("/finance", response_model=FinanceOut)
async def finance(
    request: Request, c: Common = Depends(common), user: CurrentUser = admin, db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> dict:
    return K.build_finance(await _analysis(request, user, c, "finance", db, cfg))


@router.get("/labs", response_model=LabsOut)
async def labs(
    request: Request, c: Common = Depends(common), user: CurrentUser = admin, db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> dict:
    return K.build_labs(await _analysis(request, user, c, "labs", db, cfg))


@router.get("/patients", response_model=PatientsOut)
async def patients(
    request: Request, c: Common = Depends(common), user: CurrentUser = admin, db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> dict:
    return K.build_patients(await _analysis(request, user, c, "patients", db, cfg))


@router.get("/below_cost", response_model=BelowCostOut)
async def below_cost(
    request: Request, c: Common = Depends(common), user: CurrentUser = admin, db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> dict:
    a = await service.analysis(db, cfg, c.params, refresh=c.refresh)
    await audit.record(
        request, "insights.below_cost_review", actor=user, module=MODULE, submodule="finance",
        target=f"{a.range.start}..{a.range.end}",
    )
    return K.build_below_cost(a)


@router.get("/report.pdf", response_class=StreamingResponse, responses={200: {"content": {"application/pdf": {}}}})
async def report_pdf(
    request: Request,
    c: Common = Depends(common),
    sections: str | None = Query(None, description="Comma-separated: overview, finance, labs, patients"),
    user: CurrentUser = admin,
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> StreamingResponse:
    if not request.app.state.rate_limiter.check(f"insights_pdf:{user.id}", PDF_PER_MINUTE, 60):
        raise HTTPException(status_code=429, detail="Too many reports. Please wait a minute and try again.")
    allowed = list(cfg.insights.report.sections)
    chosen = [s.strip() for s in sections.split(",") if s.strip()] if sections else allowed
    bad = [s for s in chosen if s not in allowed]
    if bad or not chosen:
        raise HTTPException(status_code=422, detail=f"Sections must be from: {', '.join(allowed)}.")
    a = await service.analysis(db, cfg, c.params, refresh=c.refresh)
    pdf = await asyncio.to_thread(report.render, a, chosen, user.name or user.email, cfg.app.timezone)
    await audit.record(
        request, "insights.report_download", actor=user, module=MODULE,
        target=f"period={a.range.start}..{a.range.end};labs={','.join(a.labs_selected) or 'all'};sections={','.join(chosen)}",
    )
    return StreamingResponse(
        io.BytesIO(pdf), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.file_name(a)}"'},
    )


@router.post("/jobs/report_monthly", include_in_schema=False)
async def report_monthly_job(
    request: Request,
    x_job_secret: str | None = Header(default=None),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    settings=Depends(get_settings),
) -> dict:
    """Scheduler endpoint (same secret as the Excel job). Does nothing unless `monthly_email: true`."""
    if not settings.job_secret or not x_job_secret or not safe_equal(x_job_secret, settings.job_secret):
        raise HTTPException(status_code=403, detail="Not allowed.")
    return await monthly.send_last_month(db, cfg, settings, request.app.state.mailer)