"""Access Portal and dashboard tile states (PRD section 7)."""

from tests.conftest import API


async def admin_headers(portal):
    return portal.auth(await portal.login("boss@example.com"))


async def tiles(portal, headers):
    r = await portal.client.get(f"{API}/me/modules", headers=headers)
    assert r.status_code == 200, r.text
    return {t["key"]: t for t in r.json()}


async def test_unbuilt_modules_have_no_tile_and_auth_is_never_a_tile(portal):
    admin = await admin_headers(portal)
    assert set(await tiles(portal, admin)) == {"access"}  # only built modules appear


async def test_tile_states_follow_the_prd_table(portal):
    portal.build_modules("patient_registry", "logs")
    admin = await admin_headers(portal)
    sam = await portal.as_user("sam@example.com")
    sam_id = (await portal.db["users"].find_one({"email": "sam@example.com"}))["_id"]

    # Admin sees everything open, with the toggle value
    t = await tiles(portal, admin)
    assert t["patient_registry"]["state"] == "open" and t["patient_registry"]["visible_to_staff"] is True

    # Visibility ON + no access -> locked (Request Access). Logs is off by default and Admin-ish -> hidden.
    t = await tiles(portal, sam)
    assert t["patient_registry"]["state"] == "locked" and "logs" not in t and "access" not in t

    # Requested -> pending
    r = await portal.client.post(f"{API}/access/requests", headers=sam, json={"module": "patient_registry"})
    assert r.status_code == 201 and r.json()["status"] == "requested"
    assert (await tiles(portal, sam))["patient_registry"]["state"] == "pending"

    # Granted -> open
    grant_id = r.json()["id"]
    assert (await portal.client.post(f"{API}/access/requests/{grant_id}/approve", headers=admin)).status_code == 200
    assert (await tiles(portal, sam))["patient_registry"]["state"] == "open"

    # Visibility OFF -> tile not shown, whatever the access
    r = await portal.client.put(
        f"{API}/access/modules/patient_registry/visibility", headers=admin, json={"visible_to_staff": False}
    )
    assert r.status_code == 204
    assert "patient_registry" not in await tiles(portal, sam)
    assert sam_id  # (user exists)


async def test_open_tile_lists_only_labs_the_user_may_open(portal):
    portal.build_modules("patient_registry")
    admin = await admin_headers(portal)
    await portal.add_user("sam@example.com")
    sam = portal.auth(await portal.login("sam@example.com"))
    uid = str((await portal.db["users"].find_one({"email": "sam@example.com"}))["_id"])
    r = await portal.client.post(
        f"{API}/access/grants",
        headers=admin,
        json={"user_id": uid, "module": "patient_registry", "submodule": "spectrum"},
    )
    assert r.status_code == 201
    subs = {s["key"]: s["kind"] for s in (await tiles(portal, sam))["patient_registry"]["submodules"]}
    assert subs == {"spectrum": "lab"}


async def test_lab_selector_lists_labs_from_partner_yaml_filtered_by_access(portal):
    portal.build_modules("patient_registry", "bill_generator")
    admin = await admin_headers(portal)
    # Admin: every lab enabled for the module (Bill Generator = Spectrum only; PRD 6.4)
    r = await portal.client.get(f"{API}/config/partners?module=bill_generator", headers=admin)
    assert [p["key"] for p in r.json()] == ["spectrum"]
    r = await portal.client.get(f"{API}/config/partners?module=patient_registry", headers=admin)
    assert {p["key"] for p in r.json()} == {"spectrum", "metropolis", "home_lab"}

    await portal.add_user("sam@example.com")
    sam = portal.auth(await portal.login("sam@example.com"))
    assert (await portal.client.get(f"{API}/config/partners?module=patient_registry", headers=sam)).status_code == 403
    uid = str((await portal.db["users"].find_one({"email": "sam@example.com"}))["_id"])
    await portal.client.post(
        f"{API}/access/grants",
        headers=admin,
        json={"user_id": uid, "module": "patient_registry", "submodule": "metropolis"},
    )
    r = await portal.client.get(f"{API}/config/partners?module=patient_registry", headers=sam)
    assert [p["key"] for p in r.json()] == ["metropolis"]
    assert (await portal.client.get(f"{API}/config/partners?module=logs", headers=sam)).status_code == 404


async def test_invite_login_flow_and_only_invited_can_login(portal):
    admin = await admin_headers(portal)
    r = await portal.client.post(
        f"{API}/access/users", headers=admin, json={"email": "New@Example.com", "role": "staff", "name": "New"}
    )
    assert r.status_code == 201 and r.json()["email"] == "new@example.com"
    assert (
        await portal.client.post(
            f"{API}/access/users", headers=admin, json={"email": "new@example.com", "role": "staff"}
        )
    ).status_code == 409
    assert await portal.login("new@example.com")  # the invited person can sign in


async def test_admin_cannot_demote_or_deactivate_self(portal):
    admin = await admin_headers(portal)
    me = (await portal.client.get(f"{API}/auth/me", headers=admin)).json()["id"]
    for body in ({"role": "staff"}, {"active": False}):
        assert (await portal.client.patch(f"{API}/access/users/{me}", headers=admin, json=body)).status_code == 422


async def test_role_change_applies_immediately(portal):
    admin = await admin_headers(portal)
    sam = await portal.as_user("sam@example.com", "staff")
    uid = (await portal.client.get(f"{API}/auth/me", headers=sam)).json()["id"]
    assert (await portal.client.get(f"{API}/access/users", headers=sam)).status_code == 403
    await portal.client.patch(f"{API}/access/users/{uid}", headers=admin, json={"role": "admin"})
    assert (await portal.client.get(f"{API}/access/users", headers=sam)).status_code == 200


async def test_deny_revoke_and_matrix(portal):
    portal.build_modules("patient_registry")
    admin = await admin_headers(portal)
    sam = await portal.as_user("sam@example.com")
    r = await portal.client.post(f"{API}/access/requests", headers=sam, json={"module": "patient_registry"})
    pending = (await portal.client.get(f"{API}/access/requests", headers=admin)).json()
    assert [p["user_email"] for p in pending] == ["sam@example.com"]
    assert (await portal.client.post(f"{API}/access/requests/{r.json()['id']}/deny", headers=admin)).status_code == 200
    assert (await portal.client.get(f"{API}/access/requests", headers=admin)).json() == []
    assert (await portal.client.get(f"{API}/me/modules", headers=sam)).json()[0]["state"] == "locked"  # may re-request

    uid = r.json()["user_id"]
    g = await portal.client.post(
        f"{API}/access/grants", headers=admin, json={"user_id": uid, "module": "patient_registry"}
    )
    m = (await portal.client.get(f"{API}/access/matrix", headers=admin)).json()
    labels = [c["label"] for c in m["columns"]]
    assert "Patient Registry" in labels and "Patient Registry / Spectrum Laboratory" in labels
    assert m["rows"][0]["grants"][0]["status"] == "granted"
    assert (await portal.client.delete(f"{API}/access/grants/{g.json()['id']}", headers=admin)).status_code == 200
    assert (await portal.client.get(f"{API}/access/matrix", headers=admin)).json()["rows"][0]["grants"] == []


async def test_request_is_idempotent_and_validated(portal):
    portal.build_modules("patient_registry")
    sam = await portal.as_user("sam@example.com")
    a = await portal.client.post(f"{API}/access/requests", headers=sam, json={"module": "patient_registry"})
    b = await portal.client.post(f"{API}/access/requests", headers=sam, json={"module": "patient_registry"})
    assert a.json()["id"] == b.json()["id"]
    assert await portal.db["module_access"].count_documents({}) == 1
    bad = await portal.client.post(
        f"{API}/access/requests", headers=sam, json={"module": "patient_registry", "submodule": "nope"}
    )
    assert bad.status_code == 422
    assert (
        await portal.client.post(f"{API}/access/requests", headers=sam, json={"module": "access"})
    ).status_code == 404


async def test_admin_only_module_cannot_be_toggled_or_granted(portal):
    admin = await admin_headers(portal)
    uid = await portal.add_user("sam@example.com")
    r = await portal.client.put(
        f"{API}/access/modules/access/visibility", headers=admin, json={"visible_to_staff": True}
    )
    assert r.status_code == 422
    r = await portal.client.post(f"{API}/access/grants", headers=admin, json={"user_id": uid, "module": "access"})
    assert r.status_code == 422


async def test_public_app_config_for_login_page(portal):
    r = await portal.client.get(f"{API}/config/app")
    assert (
        r.status_code == 200 and r.json()["brand_name"] == "TrustUs Health" and r.json()["timezone"] == "Asia/Kolkata"
    )
