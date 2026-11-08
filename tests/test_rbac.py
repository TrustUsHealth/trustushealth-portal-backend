"""RBAC matrix (PRD section 7) and server-side enforcement through require_access."""

import pytest
from fastapi import Depends

from app.core.config import load_config
from app.core.rbac import decide_access, require_access
from tests.conftest import API

CFG = load_config("config")
PR, LOGS = CFG.module("patient_registry"), CFG.module("logs")


@pytest.mark.parametrize(
    ("mod", "role", "visible", "granted", "sub", "expected"),
    [
        # admin: always
        (PR, "admin", False, set(), None, True),
        (LOGS, "admin", False, set(), "staff_logs", True),
        # staff: needs visibility AND a grant
        (PR, "staff", True, set(), None, False),
        (PR, "staff", False, {None}, None, False),  # admin switched it off
        (PR, "staff", True, {None}, None, True),
        (PR, "staff", True, {None}, "spectrum", True),  # module grant covers every lab
        # per-lab grant covers only that lab
        (PR, "staff", True, {"spectrum"}, "spectrum", True),
        (PR, "staff", True, {"spectrum"}, "metropolis", False),
        (PR, "staff", True, {"spectrum"}, None, True),  # may enter the module...
        (PR, "staff", True, {"entries"}, "excel_register", False),
        # engineer: Logs by role, everything else only when granted
        (LOGS, "engineer", False, set(), "staff_logs", True),
        (PR, "engineer", True, set(), None, False),
        (PR, "engineer", False, {None}, None, True),  # explicit grant, visibility toggle is staff-only
        # customer: nothing
        (PR, "customer", True, {None}, None, False),
        (LOGS, "customer", True, {None}, None, False),
    ],
)
def test_decide_access_matrix(mod, role, visible, granted, sub, expected):
    assert decide_access(mod, role, visible, granted, sub) is expected


@pytest.fixture
async def probe(portal):
    """Throw-away protected routes, standing in for modules that are not built yet."""
    app = portal.app
    app.add_api_route(f"{API}/_probe/pr", lambda: {"ok": 1}, dependencies=[Depends(require_access("patient_registry"))])
    app.add_api_route(
        f"{API}/_probe/pr-spectrum",
        lambda: {"ok": 1},
        dependencies=[Depends(require_access("patient_registry", "spectrum"))],
    )
    portal.build_modules("patient_registry")
    return portal


async def grant(portal, user_id, module, sub=None, status="granted"):
    await portal.db["module_access"].insert_one(
        {"user_id": user_id, "module": module, "submodule": sub, "status": status}
    )


async def test_requires_sign_in(probe):
    assert (await probe.client.get(f"{API}/_probe/pr")).status_code == 401


async def test_staff_blocked_without_grant_allowed_with_grant(probe):
    uid = await probe.add_user("sam@example.com")
    h = probe.auth(await probe.login("sam@example.com"))
    assert (await probe.client.get(f"{API}/_probe/pr", headers=h)).status_code == 403
    await grant(probe, uid, "patient_registry")
    assert (await probe.client.get(f"{API}/_probe/pr", headers=h)).status_code == 200


async def test_pending_request_does_not_grant_access(probe):
    uid = await probe.add_user("sam@example.com")
    h = probe.auth(await probe.login("sam@example.com"))
    await grant(probe, uid, "patient_registry", status="requested")
    assert (await probe.client.get(f"{API}/_probe/pr", headers=h)).status_code == 403


async def test_admin_visibility_off_blocks_granted_staff_on_the_server(probe):
    uid = await probe.add_user("sam@example.com")
    h = probe.auth(await probe.login("sam@example.com"))
    await grant(probe, uid, "patient_registry")
    await probe.db["module_state"].insert_one({"_id": "patient_registry", "visible_to_staff": False})
    assert (await probe.client.get(f"{API}/_probe/pr", headers=h)).status_code == 403


async def test_lab_level_grant_is_enforced_per_lab(probe):
    uid = await probe.add_user("sam@example.com")
    h = probe.auth(await probe.login("sam@example.com"))
    await grant(probe, uid, "patient_registry", "metropolis")
    assert (await probe.client.get(f"{API}/_probe/pr-spectrum", headers=h)).status_code == 403
    await grant(probe, uid, "patient_registry", "spectrum")
    assert (await probe.client.get(f"{API}/_probe/pr-spectrum", headers=h)).status_code == 200


async def test_engineer_has_no_patient_module_access_unless_granted(probe):
    uid = await probe.add_user("eng@example.com", "engineer")
    h = probe.auth(await probe.login("eng@example.com"))
    assert (await probe.client.get(f"{API}/_probe/pr", headers=h)).status_code == 403
    await grant(probe, uid, "patient_registry")
    assert (await probe.client.get(f"{API}/_probe/pr", headers=h)).status_code == 200


async def test_admin_endpoints_reject_staff(portal):
    h = await portal.as_user("sam@example.com", "staff")
    for method, path in [("get", "/access/users"), ("get", "/access/matrix"), ("get", "/access/requests")]:
        assert (await getattr(portal.client, method)(f"{API}{path}", headers=h)).status_code == 403
