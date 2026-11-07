"""Access Portal API (`/api/v1/access`) plus the two small shell endpoints it owns:
`GET /api/v1/me/modules` (dashboard tiles) and `GET /api/v1/config/*` (lab selector, brand).
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import audit
from app.core.config import PortalConfig
from app.core.deps import CurrentUser, get_config, get_current_user, get_db, require_role
from app.core.rbac import has_access
from app.modules.access import service
from app.modules.access.schemas import (
    AccessRequestIn,
    AppConfigOut,
    GrantIn,
    GrantOut,
    InviteIn,
    MatrixOut,
    ModuleAdminOut,
    PartnerOut,
    TileOut,
    UserAdminOut,
    UserPatchIn,
    VisibilityIn,
)

MODULE = "access"
router = APIRouter(tags=["access"])
me_router = APIRouter(tags=["dashboard"])
config_router = APIRouter(tags=["config"])
admin = Depends(require_role("admin"))


def _mounted(request: Request) -> set[str]:
    return request.app.state.mounted_modules


def _fail(exc: service.AccessError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


# ───────────────────────────── dashboard & config (any signed-in user) ─────────────────────────────
@me_router.get("/modules", response_model=list[TileOut])
async def my_modules(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> list[TileOut]:
    return await service.tiles_for(db, cfg, _mounted(request), user)


@config_router.get("/app", response_model=AppConfigOut)
async def app_config(cfg: PortalConfig = Depends(get_config)) -> AppConfigOut:
    a = cfg.app
    return AppConfigOut(
        brand_name=a.brand.name,
        logo_full=a.brand.logo_full,
        logo_mark=a.brand.logo_mark,
        favicon=a.brand.favicon,
        timezone=a.timezone,
        currency=a.currency,
        upload_max_mb=a.upload.max_mb,
        upload_allowed=list(a.upload.allowed),
    )


@config_router.get("/partners", response_model=list[PartnerOut])
async def partners_for_module(
    module: str,
    user: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> list[PartnerOut]:
    """Lab selector source: labs enabled for `module` (partner YAML) that this user may open."""
    mod = cfg.module(module)
    if mod is None or not mod.lab_aware:
        raise HTTPException(status_code=404, detail=f"'{module}' has no labs.")
    if not await has_access(db, cfg, user, module):
        raise HTTPException(status_code=403, detail="You do not have access to this module.")
    return [
        PartnerOut(key=p.key, name=p.name)
        for p in cfg.labs_for(module)
        if await has_access(db, cfg, user, module, p.key)
    ]


# ───────────────────────────── staff: request access ─────────────────────────────
@router.post("/requests", response_model=GrantOut, status_code=201)
async def create_request(
    body: AccessRequestIn,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> GrantOut:
    try:
        grant = await service.request_access(db, cfg, _mounted(request), user, body.module, body.submodule)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request,
        "access.request",
        actor=user,
        module=MODULE,
        target=f"{body.module}:{body.submodule}" if body.submodule else body.module,
    )
    return grant


# ───────────────────────────── admin ─────────────────────────────
@router.get("/users", response_model=list[UserAdminOut], dependencies=[admin])
async def users(db=Depends(get_db)) -> list[UserAdminOut]:
    return await service.list_users(db)


@router.post("/users", response_model=UserAdminOut, status_code=201)
async def invite(body: InviteIn, request: Request, who: CurrentUser = admin, db=Depends(get_db)) -> UserAdminOut:
    try:
        created = await service.invite_user(db, body.email, body.name, body.role, who)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request, "user.invite", actor=who, module=MODULE, target=created.id, fields_changed=["email", "role"]
    )
    return created


@router.patch("/users/{user_id}", response_model=UserAdminOut)
async def patch_user(
    user_id: str, body: UserPatchIn, request: Request, who: CurrentUser = admin, db=Depends(get_db)
) -> UserAdminOut:
    try:
        updated, changed = await service.patch_user(db, user_id, body.model_dump(), who)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(request, "user.update", actor=who, module=MODULE, target=user_id, fields_changed=changed)
    return updated


@router.get("/modules", response_model=list[ModuleAdminOut], dependencies=[admin])
async def modules(request: Request, db=Depends(get_db), cfg: PortalConfig = Depends(get_config)):
    return await service.admin_modules(db, cfg, _mounted(request))


@router.put("/modules/{key}/visibility", status_code=204)
async def set_visibility(
    key: str,
    body: VisibilityIn,
    request: Request,
    who: CurrentUser = admin,
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> None:
    try:
        await service.set_visibility(db, cfg, _mounted(request), key, body.visible_to_staff, who)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request, "access.visibility_change", actor=who, module=MODULE, target=key, fields_changed=["visible_to_staff"]
    )


@router.get("/requests", response_model=list[GrantOut], dependencies=[admin])
async def pending(db=Depends(get_db)) -> list[GrantOut]:
    return await service.pending_requests(db)


@router.post("/requests/{grant_id}/approve", response_model=GrantOut)
async def approve(grant_id: str, request: Request, who: CurrentUser = admin, db=Depends(get_db)) -> GrantOut:
    try:
        grant = await service.approve(db, grant_id, who)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request,
        "access.grant",
        actor=who,
        module=MODULE,
        target=f"{grant.user_id}:{grant.module}" + (f":{grant.submodule}" if grant.submodule else ""),
    )
    return grant


@router.post("/requests/{grant_id}/deny", response_model=GrantOut)
async def deny(grant_id: str, request: Request, who: CurrentUser = admin, db=Depends(get_db)) -> GrantOut:
    try:
        grant = await service.deny(db, grant_id)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request,
        "access.deny",
        actor=who,
        module=MODULE,
        target=f"{grant.user_id}:{grant.module}" + (f":{grant.submodule}" if grant.submodule else ""),
    )
    return grant


@router.post("/grants", response_model=GrantOut, status_code=201)
async def pre_grant(
    body: GrantIn,
    request: Request,
    who: CurrentUser = admin,
    db=Depends(get_db),
    cfg: PortalConfig = Depends(get_config),
) -> GrantOut:
    try:
        grant = await service.pre_grant(db, cfg, _mounted(request), body.user_id, body.module, body.submodule, who)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request,
        "access.grant",
        actor=who,
        module=MODULE,
        target=f"{grant.user_id}:{grant.module}" + (f":{grant.submodule}" if grant.submodule else ""),
    )
    return grant


@router.delete("/grants/{grant_id}", response_model=GrantOut)
async def revoke(grant_id: str, request: Request, who: CurrentUser = admin, db=Depends(get_db)) -> GrantOut:
    try:
        grant = await service.revoke(db, grant_id)
    except service.AccessError as exc:
        raise _fail(exc) from None
    await audit.record(
        request,
        "access.revoke",
        actor=who,
        module=MODULE,
        target=f"{grant.user_id}:{grant.module}" + (f":{grant.submodule}" if grant.submodule else ""),
    )
    return grant


@router.get("/matrix", response_model=MatrixOut, dependencies=[admin])
async def matrix(request: Request, db=Depends(get_db), cfg: PortalConfig = Depends(get_config)) -> MatrixOut:
    return await service.matrix(db, cfg, _mounted(request))


# Extra routers mounted by discovery at /api/v1/<prefix>
extra_routers = {"me": me_router, "config": config_router}
