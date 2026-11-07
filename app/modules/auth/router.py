from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response

from app.core import audit
from app.core.config import PortalConfig, Settings
from app.core.db import utcnow
from app.core.deps import CurrentUser, get_config, get_current_user, get_db, get_mailer, get_settings
from app.core.mailer import Mailer
from app.core.security import normalize_email, rate_limit
from app.modules.auth import service
from app.modules.auth.schemas import OtpRequestedOut, OtpRequestIn, OtpVerifyIn, TokenOut, UserOut

router = APIRouter(tags=["auth"])
MODULE = "auth"
REFRESH_COOKIE = "tu_refresh"
COOKIE_PATH = "/api/v1/auth"


def _user_out(user: dict) -> UserOut:
    return UserOut(id=str(user["_id"]), email=user["email"], name=user.get("name"), role=user["role"])


def _current(user: dict) -> CurrentUser:
    return CurrentUser(**_user_out(user).model_dump())


def _set_cookie(response: Response, settings: Settings, session: service.Session) -> None:
    max_age = max(0, int((session.refresh_expires_at - utcnow()).total_seconds()))
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",  # same origin through the frontend proxy
        path=COOKIE_PATH,
    )


def _token_out(session: service.Session) -> TokenOut:
    return TokenOut(access_token=session.access_token, expires_in=session.expires_in, user=_user_out(session.user))


@router.post(
    "/otp/request",
    status_code=202,
    response_model=OtpRequestedOut,
    dependencies=[Depends(rate_limit("otp_request", 20))],
)
async def request_otp(
    body: OtpRequestIn,
    request: Request,
    background: BackgroundTasks,
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    settings: Settings = Depends(get_settings),
    mailer: Mailer = Depends(get_mailer),
) -> OtpRequestedOut:
    decision = await service.request_otp(db, cfg, settings, body.email)
    if decision.code:
        mail = cfg.app.auth.email
        fmt = {"brand": cfg.app.brand.name, "code": decision.code, "minutes": cfg.app.auth.otp.expires_minutes}
        # Sent in the background so response time does not reveal whether the email exists.
        background.add_task(
            mailer.send, normalize_email(body.email), mail.subject.format(**fmt), mail.body.format(**fmt)
        )
    await audit.record(
        request,
        "auth.otp_requested",
        actor_email=normalize_email(body.email),
        module=MODULE,
        target=decision.skipped,
        outcome="success" if decision.code else "denied",
    )
    return OtpRequestedOut(resend_after_seconds=cfg.app.auth.otp.resend_cooldown_seconds)


@router.post("/otp/verify", response_model=TokenOut, dependencies=[Depends(rate_limit("otp_verify", 30))])
async def verify_otp(
    body: OtpVerifyIn,
    request: Request,
    response: Response,
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    user = await service.verify_otp(db, settings, body.email, body.code)
    if user is None:
        await audit.record(
            request, "auth.login", actor_email=normalize_email(body.email), module=MODULE, outcome="failure"
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code.")
    session = await service.start_session(db, cfg, settings, user)
    _set_cookie(response, settings, session)
    await audit.record(request, "auth.login", actor=_current(user), module=MODULE)
    return _token_out(session)


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    request: Request,
    response: Response,
    tu_refresh: str | None = Cookie(default=None),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    result = await service.rotate_refresh(db, cfg, settings, tu_refresh) if tu_refresh else service.RefreshResult()
    if result.session is None:
        response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
        if result.reuse_detected:
            await audit.record(request, "auth.refresh_reuse", module=MODULE, target=result.user_id, outcome="denied")
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    _set_cookie(response, settings, result.session)
    return _token_out(result.session)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    tu_refresh: str | None = Cookie(default=None),
    db=Depends(get_db),
) -> None:
    user = await service.logout(db, tu_refresh)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    if user:
        await audit.record(request, "auth.logout", actor=_current(user), module=MODULE)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser = Depends(get_current_user)) -> UserOut:
    return UserOut(**user.model_dump())
