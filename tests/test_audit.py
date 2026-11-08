"""Audit writing is a core feature from day one (PRD 10.5)."""

import json

from tests.conftest import API


async def logs(portal, **q):
    return await portal.db["audit_logs"].find(q).to_list(None)


async def test_login_events_are_logged_with_actor_ip_device_and_ist_time(portal):
    await portal.add_user("sam@example.com")
    await portal.login("sam@example.com")
    (login,) = await logs(portal, action="auth.login")
    assert login["actor_email"] == "sam@example.com" and login["role"] == "staff" and login["outcome"] == "success"
    assert login["ip"] and "device" in login and login["ts_local"].endswith("+05:30")
    assert await logs(portal, action="auth.otp_requested")


async def test_failed_login_is_logged_as_failure(portal):
    await portal.add_user("sam@example.com")
    await portal.client.post(f"{API}/auth/otp/request", json={"email": "sam@example.com"})
    await portal.client.post(f"{API}/auth/otp/verify", json={"email": "sam@example.com", "code": "000000"})
    assert await logs(portal, action="auth.login", outcome="failure")


async def test_unknown_email_is_logged_internally_but_client_sees_nothing_different(portal):
    await portal.client.post(f"{API}/auth/otp/request", json={"email": "ghost@example.com"})
    (rec,) = await logs(portal, action="auth.otp_requested")
    assert rec["outcome"] == "denied" and rec["target"] == "unknown_email"


async def test_no_otp_codes_or_tokens_ever_reach_the_log(portal):
    await portal.add_user("sam@example.com")
    token = await portal.login("sam@example.com")
    code = portal.mailer.last_code("sam@example.com")
    dump = json.dumps(await logs(portal), default=str)
    assert code not in dump and token not in dump and portal.client.cookies.get("tu_refresh") not in dump


async def test_admin_actions_log_field_names_not_values(portal):
    admin = portal.auth(await portal.login("boss@example.com"))
    r = await portal.client.post(
        f"{API}/access/users", headers=admin, json={"email": "new@example.com", "role": "staff", "name": "Secret Name"}
    )
    uid = r.json()["id"]
    await portal.client.patch(
        f"{API}/access/users/{uid}", headers=admin, json={"name": "Another Secret", "active": False}
    )
    rec = (await logs(portal, action="user.update"))[0]
    assert rec["fields_changed"] == ["active", "name"] and rec["actor_email"] == "boss@example.com"
    assert "Secret" not in json.dumps(await logs(portal), default=str)


async def test_access_requests_grants_and_visibility_are_logged(portal):
    portal.build_modules("patient_registry")
    admin = portal.auth(await portal.login("boss@example.com"))
    sam = await portal.as_user("sam@example.com")
    r = await portal.client.post(f"{API}/access/requests", headers=sam, json={"module": "patient_registry"})
    await portal.client.post(f"{API}/access/requests/{r.json()['id']}/approve", headers=admin)
    await portal.client.put(
        f"{API}/access/modules/patient_registry/visibility", headers=admin, json={"visible_to_staff": False}
    )
    for action in ("access.request", "access.grant", "access.visibility_change"):
        assert await logs(portal, action=action), action


async def test_audit_module_is_append_only_by_design():
    from app.core import audit

    public = {n for n in dir(audit) if not n.startswith("_")}
    assert not {n for n in public if any(w in n for w in ("update", "delete", "remove", "edit"))}


async def test_audit_failure_never_breaks_the_request(portal):
    class Broken:
        def __getitem__(self, _):
            raise RuntimeError("db down")

    await portal.add_user("sam@example.com")
    token = await portal.login("sam@example.com")
    real = portal.app.state.db
    portal.app.state.db = Broken()
    try:
        from starlette.requests import Request

        from app.core import audit

        scope = {"type": "http", "headers": [], "app": portal.app, "client": ("1.2.3.4", 1)}
        await audit.record(Request(scope), "x.y")  # must not raise
    finally:
        portal.app.state.db = real
    assert token
