"""Entries API: duplicate rules, bill_no, edits, Excel month routing, lab access."""

import asyncio
import io

from openpyxl import load_workbook

API = "/api/v1"
PR = f"{API}/patient_registry"


def body(**kw):
    return {"partner_key": "spectrum", "patient_name": "Kalphem MS", "registration_date": "2026-10-14", **kw}


async def admin(portal):
    return portal.auth(await portal.login("boss@example.com"))


async def post(portal, h, **kw):
    return await portal.client.post(f"{PR}/entries", headers=h, json=body(**kw))


async def test_bill_no_sequence_and_exact_match_by_lab_pid(portal):
    h = await admin(portal)
    a = await post(portal, h, lab_pid="A1")
    assert a.status_code == 201 and a.json()["entry"]["bill_no"] == "000001"
    b = await post(portal, h, patient_name="Ramesh Gada", gender="Male", age=52, lab_pid="A2")
    assert b.json()["entry"]["bill_no"] == "000002"
    c = await post(portal, h, lab_pid="A1", referred_by="dr saumil shah")  # same lab + date + PID
    j = c.json()
    assert c.status_code == 200 and j["outcome"] == "merged" and j["entry"]["bill_no"] == "000001"
    assert "referred_by" in j["filled_fields"] and j["entry"]["referred_by"] == "Dr. Saumil Shah"
    assert (await post(portal, h, lab_pid="B7", registration_date="2026-10-15")).json()["entry"]["bill_no"] == "000003"


async def test_conflict_needs_a_choice_and_nothing_is_overwritten(portal):
    h = await admin(portal)
    await post(portal, h, lab_pid="A1", referred_by="Dr. Saumil Shah")
    r = await post(portal, h, lab_pid="A1", referred_by="Dr. Jatin Kothari")
    assert r.status_code == 409 and r.json()["outcome"] == "conflict"
    assert [c["field"] for c in r.json()["conflicts"]] == ["referred_by"] and r.json()["can_force_new"] is False
    eid = r.json()["match"]["id"]
    ok = await post(portal, h, lab_pid="A1", referred_by="Dr. Jatin Kothari", target_entry_id=eid,
                    resolutions={"referred_by": "incoming"})
    assert ok.status_code == 200 and ok.json()["entry"]["referred_by"] == "Dr. Jatin Kothari"


async def test_possible_match_and_different_patient(portal):
    h = await admin(portal)
    first = await post(portal, h, lab_pid="A1")
    r = await post(portal, h, patient_name="Kalphem Ms.", lab_pid="Z9")  # similar name, other PID, no age/gender
    assert r.status_code == 409 and r.json()["outcome"] == "possible_match"
    assert r.json()["possible_matches"][0]["id"] == first.json()["entry"]["id"]
    forced = await post(portal, h, patient_name="Kalphem Ms.", lab_pid="Z9", force_new=True)
    assert forced.status_code == 201 and forced.json()["entry"]["bill_no"] == "000002"


async def test_edit_keeps_bill_no_and_complete_entries_reach_the_right_month_sheet(portal):
    h = await admin(portal)
    e = (await post(portal, h, lab_pid="A1", tests=["Sugar Fasting Blood", "Creatinine, Serum"])).json()["entry"]
    assert e["status"] == "draft"
    r = await portal.client.patch(
        f"{PR}/entries/{e['id']}", headers=h, json={"gross_amount": 500, "discount_amount": 50, "lab_cost": 300}
    )
    out = r.json()
    assert r.status_code == 200 and out["bill_no"] == e["bill_no"] and out["status"] == "complete"
    assert out["net_amount"] == 450 and out["margin"] == 150
    x = await portal.client.get(f"{PR}/excel_register/download?year=2026", headers=h)
    ws = load_workbook(io.BytesIO(x.content))["Oct"]
    row = [c.value for c in ws[2]]
    assert row[0] == e["bill_no"] and "Sugar Fasting Blood; Creatinine, Serum" in row and 450 in row
    assert ws.max_row == 2 and load_workbook(io.BytesIO(x.content))["Nov"].max_row == 1
    # moving the registration date moves the row to the other month sheet
    await portal.client.patch(f"{PR}/entries/{e['id']}", headers=h, json={"registration_date": "2026-11-02"})
    wb = load_workbook(io.BytesIO((await portal.client.get(f"{PR}/excel_register/download?year=2026", headers=h)).content))
    assert wb["Oct"].max_row == 1 and wb["Nov"].max_row == 2


async def test_concurrent_saves_get_unique_contiguous_bill_numbers(portal):
    h = await admin(portal)
    rs = await asyncio.gather(*(post(portal, h, patient_name=f"Patient Number{chr(65 + i)}", lab_pid=f"P{i}") for i in range(12)))
    nos = sorted(r.json()["entry"]["bill_no"] for r in rs)
    assert nos == [str(i).zfill(6) for i in range(1, 13)]


async def test_staff_access_is_per_lab(portal):
    h = await admin(portal)
    sam = await portal.as_user("sam@example.com")
    assert (await portal.client.get(f"{PR}/entries", headers=sam)).status_code == 403
    uid = (await portal.db["users"].find_one({"email": "sam@example.com"}))["_id"]
    g = await portal.client.post(
        f"{API}/access/grants", headers=h, json={"user_id": str(uid), "module": "patient_registry", "submodule": "spectrum"}
    )
    assert g.status_code in (200, 201), g.text
    assert (await post(portal, sam, lab_pid="A1")).status_code == 201
    other = await portal.client.post(
        f"{PR}/entries", headers=sam, json={**body(), "partner_key": "metropolis", "lab_pid": "M1"}
    )
    assert other.status_code == 403
    assert (await portal.client.get(f"{PR}/entries", headers=sam)).json()["total"] == 1