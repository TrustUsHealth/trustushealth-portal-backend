"""Server-side access control. Hiding things in the UI is never the only protection.

One dependency, `require_access(module, submodule)`, guards every module route.

Rules (PRD section 7)
- admin    : everything.
- engineer : modules whose `roles` in modules.yaml include engineer (Logs, read-only), without a
             grant; anything else only when granted ("no patient data unless granted").
- staff    : only modules the admin made visible AND granted to them.
- customer : nothing (reserved for the future app).
A grant with no submodule covers the whole module; a grant with a submodule (a sub-area or a
lab key such as `spectrum`) covers only that. A route with no submodule needs ANY grant on the
module, so lab-aware routes must pass the lab key to see per-lab restrictions.
"""

from fastapi import Depends, HTTPException, Request
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import ModuleDef, PortalConfig
from app.core.deps import CurrentUser, get_current_user

STATE = "module_state"  # {_id: module_key, visible_to_staff, updated_by, updated_at}
ACCESS = "module_access"  # {user_id, module, submodule|None, status: requested|granted, ...}


def decide_access(
    mod: ModuleDef, role: str, visible_to_staff: bool, granted: set[str | None], submodule: str | None
) -> bool:
    """Pure decision function (easy to test as a matrix)."""
    if role == "admin":
        return True
    if role == "engineer" and "engineer" in mod.roles:
        return True
    if role not in ("staff", "engineer"):
        return False
    if role == "staff" and not visible_to_staff:
        return False
    if None in granted:
        return True
    if submodule is None:
        return bool(granted)
    return submodule in granted


async def visibility_map(db: AsyncDatabase, cfg: PortalConfig) -> dict[str, bool]:
    """Effective 'Visible to Staff' per module: admin override from the database, else YAML default."""
    vis = {m.key: m.visible_to_staff for m in cfg.modules}
    async for doc in db[STATE].find({}):
        if doc["_id"] in vis:
            vis[doc["_id"]] = bool(doc["visible_to_staff"])
    return vis


async def grants_for(db: AsyncDatabase, user_id: str) -> dict[str, dict[str | None, str]]:
    """{module: {submodule|None: status}} for one user."""
    out: dict[str, dict[str | None, str]] = {}
    async for g in db[ACCESS].find({"user_id": user_id}):
        out.setdefault(g["module"], {})[g.get("submodule")] = g["status"]
    return out


async def has_access(
    db: AsyncDatabase, cfg: PortalConfig, user: CurrentUser, module: str, submodule: str | None = None
) -> bool:
    mod = cfg.module(module)
    if mod is None:
        return False
    if user.role == "admin":
        return True
    vis = (await visibility_map(db, cfg))[module]
    granted = {sub for sub, status in (await grants_for(db, user.id)).get(module, {}).items() if status == "granted"}
    return decide_access(mod, user.role, vis, granted, submodule)


def require_access(module: str, submodule: str | None = None):
    """Route dependency: 403 unless the signed-in user may use `module` (and `submodule`)."""

    async def dependency(request: Request, user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not await has_access(request.app.state.db, request.app.state.config, user, module, submodule):
            raise HTTPException(status_code=403, detail="You do not have access to this module.")
        return user

    return dependency
