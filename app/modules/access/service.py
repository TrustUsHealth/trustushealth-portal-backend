"""Access Portal logic: users, visibility, requests/grants, dashboard tiles."""

from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import LOGIN_ROLES, ModuleDef, PortalConfig
from app.core.db import utcnow
from app.core.deps import CurrentUser
from app.core.rbac import ACCESS, STATE, decide_access, grants_for, visibility_map
from app.core.security import normalize_email
from app.modules.access.schemas import (
    GrantOut,
    MatrixColumn,
    MatrixOut,
    MatrixRow,
    ModuleAdminOut,
    SubmoduleOut,
    TileOut,
    UserAdminOut,
)

USERS = "users"


class AccessError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db[USERS].create_index("email", unique=True)
    await db[ACCESS].create_index([("user_id", 1), ("module", 1), ("submodule", 1)], unique=True)
    await db[ACCESS].create_index("status")


async def on_startup(app) -> None:
    await ensure_indexes(app.state.db)


def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId:
        raise AccessError(404, "Not found") from None


# ───────────────────────────── catalog helpers ─────────────────────────────
def admin_only(mod: ModuleDef) -> bool:
    return mod.roles == ["admin"]


def tile_modules(cfg: PortalConfig, mounted: set[str]) -> list[ModuleDef]:
    """Modules with a dashboard tile: built (backend folder exists), tile: true."""
    return [m for m in cfg.modules if m.tile and m.key in mounted]


def submodules_of(cfg: PortalConfig, mod: ModuleDef) -> list[SubmoduleOut]:
    subs = [SubmoduleOut(key=s.key, name=s.name, kind="submodule") for s in mod.submodules]
    subs += [SubmoduleOut(key=p.key, name=p.name, kind="lab") for p in cfg.labs_for(mod.key)]
    return subs


def _find_tile_module(cfg: PortalConfig, mounted: set[str], key: str) -> ModuleDef:
    mod = next((m for m in tile_modules(cfg, mounted) if m.key == key), None)
    if mod is None:
        raise AccessError(404, f"Unknown module '{key}'")
    return mod


def _check_target(cfg: PortalConfig, mounted: set[str], module: str, submodule: str | None) -> ModuleDef:
    mod = _find_tile_module(cfg, mounted, module)
    if admin_only(mod):
        raise AccessError(422, f"'{mod.name}' is Admin-only and cannot be granted.")
    if submodule is not None and submodule not in cfg.submodule_keys(module):
        raise AccessError(422, f"'{submodule}' is not a submodule or lab of '{module}'.")
    return mod


# ───────────────────────────── dashboard tiles ─────────────────────────────
async def tiles_for(db: AsyncDatabase, cfg: PortalConfig, mounted: set[str], user: CurrentUser) -> list[TileOut]:
    vis = await visibility_map(db, cfg)
    grants = {} if user.role == "admin" else await grants_for(db, user.id)
    out: list[TileOut] = []
    for mod in tile_modules(cfg, mounted):
        my = grants.get(mod.key, {})
        granted = {s for s, st in my.items() if st == "granted"}
        subs = submodules_of(cfg, mod)
        if user.role == "admin":
            state, visible_subs = "open", subs
        elif decide_access(mod, user.role, vis[mod.key], granted, None):
            state = "open"
            visible_subs = [s for s in subs if decide_access(mod, user.role, vis[mod.key], granted, s.key)]
        elif user.role == "staff" and vis[mod.key] and "staff" in mod.roles and not admin_only(mod):
            state = "pending" if "requested" in my.values() else "locked"
            visible_subs = []
        else:
            continue  # Off (or not for this role): tile not shown
        out.append(
            TileOut(
                key=mod.key,
                name=mod.name,
                icon=mod.icon,
                route=f"/{mod.key}",
                state=state,
                submodules=visible_subs,
                visible_to_staff=vis[mod.key] if user.role == "admin" else None,
            )
        )
    return out


# ───────────────────────────── visibility ─────────────────────────────
async def admin_modules(db: AsyncDatabase, cfg: PortalConfig, mounted: set[str]) -> list[ModuleAdminOut]:
    vis = await visibility_map(db, cfg)
    return [
        ModuleAdminOut(
            key=m.key,
            name=m.name,
            icon=m.icon,
            route=f"/{m.key}",
            visible_to_staff=vis[m.key],
            staff_eligible=not admin_only(m),
            submodules=submodules_of(cfg, m),
        )
        for m in tile_modules(cfg, mounted)
    ]


async def set_visibility(
    db: AsyncDatabase, cfg: PortalConfig, mounted: set[str], key: str, visible: bool, admin: CurrentUser
) -> None:
    mod = _find_tile_module(cfg, mounted, key)
    if admin_only(mod):
        raise AccessError(422, f"'{mod.name}' is Admin-only; the staff toggle does not apply.")
    await db[STATE].update_one(
        {"_id": key},
        {"$set": {"visible_to_staff": visible, "updated_by": admin.id, "updated_at": utcnow()}},
        upsert=True,
    )


# ───────────────────────────── users ─────────────────────────────
def user_out(u: dict) -> UserAdminOut:
    return UserAdminOut(
        id=str(u["_id"]),
        email=u["email"],
        name=u.get("name"),
        role=u["role"],
        active=bool(u.get("active")),
        created_at=u["created_at"],
        last_login_at=u.get("last_login_at"),
    )


async def list_users(db: AsyncDatabase) -> list[UserAdminOut]:
    return [user_out(u) async for u in db[USERS].find({}).sort("created_at", 1)]


async def invite_user(db: AsyncDatabase, email: str, name: str | None, role: str, admin: CurrentUser) -> UserAdminOut:
    """Only invited emails can log in. Invite = register the email with a role."""
    doc = {
        "email": normalize_email(email),
        "name": name,
        "role": role,
        "active": True,
        "invited_by": admin.id,
        "created_at": utcnow(),
        "last_login_at": None,
    }
    try:
        await db[USERS].insert_one(doc)
    except DuplicateKeyError:
        raise AccessError(409, "This email is already registered.") from None
    return user_out(doc)


async def patch_user(
    db: AsyncDatabase, user_id: str, changes: dict, admin: CurrentUser
) -> tuple[UserAdminOut, list[str]]:
    if user_id == admin.id and (changes.get("role") not in (None, "admin") or changes.get("active") is False):
        # Guarantees there is always at least one active Admin.
        raise AccessError(422, "You cannot remove your own Admin access or deactivate yourself.")
    updates = {k: v for k, v in changes.items() if v is not None}
    if not updates:
        raise AccessError(422, "Nothing to change.")
    doc = await db[USERS].find_one_and_update(
        {"_id": oid(user_id)}, {"$set": updates}, return_document=ReturnDocument.AFTER
    )
    if doc is None:
        raise AccessError(404, "User not found")
    return user_out(doc), sorted(updates)


# ───────────────────────────── requests & grants ─────────────────────────────
def grant_out(g: dict, emails: dict[str, str] | None = None) -> GrantOut:
    return GrantOut(
        id=str(g["_id"]),
        user_id=g["user_id"],
        user_email=(emails or {}).get(g["user_id"]),
        module=g["module"],
        submodule=g.get("submodule"),
        status=g["status"],
        requested_at=g.get("requested_at"),
        granted_at=g.get("granted_at"),
    )


async def _emails(db: AsyncDatabase, user_ids: set[str]) -> dict[str, str]:
    users = await db[USERS].find({"_id": {"$in": [ObjectId(i) for i in user_ids]}}).to_list(None)
    return {str(u["_id"]): u["email"] for u in users}


async def request_access(
    db: AsyncDatabase,
    cfg: PortalConfig,
    mounted: set[str],
    user: CurrentUser,
    module: str,
    submodule: str | None,
) -> GrantOut:
    if user.role != "staff":
        raise AccessError(403, "Only Staff request access; Admin grants it directly.")
    mod = _find_tile_module(cfg, mounted, module)
    if "staff" not in mod.roles or not (await visibility_map(db, cfg))[module]:
        # Not shown to this user, so to them it does not exist (also hides Admin-only modules).
        raise AccessError(404, f"Unknown module '{module}'")
    _check_target(cfg, mounted, module, submodule)
    existing = await db[ACCESS].find_one({"user_id": user.id, "module": module, "submodule": submodule})
    if existing:
        if existing["status"] == "granted":
            raise AccessError(409, "You already have access.")
        return grant_out(existing)  # idempotent: request already pending
    doc = {
        "user_id": user.id,
        "module": module,
        "submodule": submodule,
        "status": "requested",
        "requested_at": utcnow(),
        "granted_at": None,
        "granted_by": None,
    }
    try:
        res = await db[ACCESS].insert_one(doc)
    except DuplicateKeyError:  # two taps at once
        return grant_out(await db[ACCESS].find_one({"user_id": user.id, "module": module, "submodule": submodule}))
    doc["_id"] = res.inserted_id
    return grant_out(doc, {user.id: user.email})


async def pending_requests(db: AsyncDatabase) -> list[GrantOut]:
    docs = await db[ACCESS].find({"status": "requested"}).sort("requested_at", 1).to_list(None)
    emails = await _emails(db, {d["user_id"] for d in docs})
    return [grant_out(d, emails) for d in docs]


async def approve(db: AsyncDatabase, grant_id: str, admin: CurrentUser) -> GrantOut:
    doc = await db[ACCESS].find_one_and_update(
        {"_id": oid(grant_id), "status": "requested"},
        {"$set": {"status": "granted", "granted_at": utcnow(), "granted_by": admin.id}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise AccessError(404, "No pending request with this id.")
    return grant_out(doc, await _emails(db, {doc["user_id"]}))


async def deny(db: AsyncDatabase, grant_id: str) -> GrantOut:
    doc = await db[ACCESS].find_one_and_delete({"_id": oid(grant_id), "status": "requested"})
    if doc is None:
        raise AccessError(404, "No pending request with this id.")
    return grant_out(doc, await _emails(db, {doc["user_id"]}))


async def pre_grant(
    db: AsyncDatabase,
    cfg: PortalConfig,
    mounted: set[str],
    user_id: str,
    module: str,
    submodule: str | None,
    admin: CurrentUser,
) -> GrantOut:
    _check_target(cfg, mounted, module, submodule)
    target = await db[USERS].find_one({"_id": oid(user_id), "active": True, "role": {"$in": list(LOGIN_ROLES)}})
    if target is None:
        raise AccessError(404, "User not found")
    if target["role"] == "admin":
        raise AccessError(422, "Admins already have access to everything.")
    now: datetime = utcnow()
    doc = await db[ACCESS].find_one_and_update(
        {"user_id": user_id, "module": module, "submodule": submodule},
        {
            "$set": {"status": "granted", "granted_at": now, "granted_by": admin.id},
            "$setOnInsert": {"requested_at": None},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return grant_out(doc, {user_id: target["email"]})


async def revoke(db: AsyncDatabase, grant_id: str) -> GrantOut:
    doc = await db[ACCESS].find_one_and_delete({"_id": oid(grant_id), "status": "granted"})
    if doc is None:
        raise AccessError(404, "No granted access with this id.")
    return grant_out(doc, await _emails(db, {doc["user_id"]}))


async def matrix(db: AsyncDatabase, cfg: PortalConfig, mounted: set[str]) -> MatrixOut:
    mods = [m for m in tile_modules(cfg, mounted) if not admin_only(m)]
    columns: list[MatrixColumn] = []
    for m in mods:
        columns.append(MatrixColumn(module=m.key, module_name=m.name, submodule=None, label=m.name))
        columns += [
            MatrixColumn(module=m.key, module_name=m.name, submodule=s.key, label=f"{m.name} / {s.name}")
            for s in submodules_of(cfg, m)
        ]
    users = (
        await db[USERS].find({"active": True, "role": {"$in": ["staff", "engineer"]}}).sort("email", 1).to_list(None)
    )
    grants = await db[ACCESS].find({}).to_list(None)
    by_user: dict[str, list[GrantOut]] = {}
    emails = {str(u["_id"]): u["email"] for u in users}
    for g in grants:
        by_user.setdefault(g["user_id"], []).append(grant_out(g, emails))
    return MatrixOut(
        columns=columns,
        rows=[MatrixRow(user=user_out(u), grants=by_user.get(str(u["_id"]), [])) for u in users],
    )
