"""Business Insights API: Admin-only access (PRD 7, 10.7), filters, caching, below-cost list, PDF, audit, monthly job."""

import io
from datetime import date
from itertools import count

import pymupdf
import pytest
from openpyxl import load_workbook

from app.modules.insights import service
from tests.conftest import API

INS = f"{API}/insights"
TABS = ["overview", "finance", "labs", "patients", "below_cost"]
TODAY = date(2026, 11, 12)
_seq = count(1)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(service, "today_local", lambda cfg: TODAY)
    service.CACHE.clear()


async def admin(portal):
    return portal.auth(await portal.login("boss@example.com"))


def entry(day, lab="spectrum", net="1000", cost="600", cust="c1", status="complete", **kw):
    n = next(_seq)
    return {
        "bill_no": f"{600000 + n}", "registration_date": day, "partner_key": lab,
        "status": status, "customer_id": cust, "tests": ["TSH"], "referred_by": "Dr. A Shah", "age": 45, "age_unit": "Y",
        "gender": "Male", "gross_amount": net, "discount_amount": "0", "net_amount": net, "lab_cost": cost,
        "patient_name": "Secret Person", "phone": "9999999999", "lab_pid": f"P-SECRET-{n}", **kw,
    }


async def seed(portal, *docs):
    await portal.db["entries"].insert_many(list(docs))
    service.CACHE.clear()


# ───────────── access ─────────────
async def test_staff_and_engineer_get_403_from_every_endpoint(portal):
    staff = await portal.as_user("sam@example.com", "staff")
    eng = await portal.as_user("eng@example.com", "engineer")
    for h in (staff, eng):
        for path in ["filters", *TABS, "report.pdf"]:
            assert (await portal.client.get(f"{INS}/{path}", headers=h)).status_code == 403, path
        assert (await portal.client.post(f"{INS}/report.email", headers=h)).status_code == 403
    for path in ["filters", *TABS, "report.pdf"]:
        assert (await portal.client.get(f"{INS}/{path}")).status_code == 401
    assert (await portal.client.post(f"{INS}/report.email")).status_code == 401


async def test_old_grant_does_not_open_insights_and_new_grants_are_refused(portal):
    h = await admin(portal)
    uid = await portal.add_user("sam@example.com", "staff")
    await portal.db["module_access"].insert_one({"user_id": uid, "module": "insights", "submodule": None, "status": "granted"})
    sam = portal.auth(await portal.login("sam@example.com"))
    assert (await portal.client.get(f"{INS}/overview", headers=sam)).status_code == 403
    g = await portal.client.post(f"{API}/access/grants", headers=h, json={"user_id": uid, "module": "insights"})
    assert g.status_code == 422


async def test_tile_only_for_admin_with_no_staff_toggle_and_not_in_matrix(portal):
    h = await admin(portal)
    tiles = {t["key"]: t for t in (await portal.client.get(f"{API}/me/modules", headers=h)).json()}
    assert tiles["insights"]["state"] == "open" and tiles["insights"]["route"] == "/insights"
    assert {s["key"] for s in tiles["insights"]["submodules"]} == {"overview", "finance", "labs", "patients"}
    mods = {m["key"]: m for m in (await portal.client.get(f"{API}/access/modules", headers=h)).json()}
    assert mods["insights"]["staff_eligible"] is False
    sam = await portal.as_user("sam@example.com", "staff")
    assert "insights" not in {t["key"] for t in (await portal.client.get(f"{API}/me/modules", headers=sam)).json()}
    matrix = (await portal.client.get(f"{API}/access/matrix", headers=h)).json()
    assert all(c["module"] != "insights" for c in matrix["columns"])
    toggle = await portal.client.patch(f"{API}/access/modules/insights", headers=h, json={"visible_to_staff": True})
    assert toggle.status_code in (404, 405, 422)


# ───────────── filters ─────────────
async def test_filters_resolve_presets_labs_and_thresholds(portal):
    h = await admin(portal)
    await seed(portal, entry("2026-07-01"))
    f = (await portal.client.get(f"{INS}/filters", headers=h)).json()
    presets = {p["key"]: p for p in f["presets"]}
    assert (presets["this_month"]["start"], presets["this_month"]["end"]) == ("2026-11-01", "2026-11-12")
    assert (presets["this_fy"]["start"], presets["last_fy"]["end"]) == ("2026-04-01", "2026-03-31")
    assert {x["key"] for x in f["labs"]} == {"spectrum", "metropolis", "home_lab"} and all(x["color"] for x in f["labs"])
    assert f["earliest_data_date"] == "2026-07-01" and f["thresholds"]["low_margin_pct"] == 25
    assert f["default_period"] == "this_month" and f["report_sections"] == ["overview", "finance", "labs", "patients"]


async def test_bad_filters_are_rejected_in_plain_words(portal):
    h = await admin(portal)
    assert (await portal.client.get(f"{INS}/overview?period=nonsense", headers=h)).status_code == 422
    assert (await portal.client.get(f"{INS}/overview?compare=sideways", headers=h)).status_code == 422
    r = await portal.client.get(f"{INS}/overview?labs=nolab", headers=h)
    assert r.status_code == 422 and "Unknown lab" in r.json()["detail"]
    r = await portal.client.get(f"{INS}/overview?from=2026-11-05&to=2026-11-01", headers=h)
    assert r.status_code == 422 and "start date" in r.json()["detail"]


# ───────────── numbers ─────────────
async def test_overview_finance_labs_patients_follow_the_filters(portal):
    h = await admin(portal)
    await seed(
        portal,
        entry("2026-11-02", net="1000", cost="600", cust="c1"),
        entry("2026-11-03", lab="metropolis", net="3000", cost="1000", cust="c2"),
        entry("2026-11-04", net="500", cost=None, cust="c3", status="draft"),
        entry("2026-10-02", net="2000", cost="1000", cust="c1"),
    )
    o = (await portal.client.get(f"{INS}/overview", headers=h)).json()
    kpis = {k["key"]: k for k in o["kpis"]}
    assert kpis["billed_revenue"]["value"] == "4000.00" and kpis["billed_revenue"]["compare_value"] == "2000.00"
    assert kpis["billed_revenue"]["direction"] == "up" and kpis["billed_revenue"]["delta_pct"] == 100.0
    assert kpis["visits"]["value"] == 3 and kpis["margin"]["value"] == "2400.00"
    assert o["range"]["start"] == "2026-11-01" and o["compare_range"]["end"] == "2026-10-12" and o["grain"] == "day"
    assert {a["key"]: a["count"] for a in o["attention"]}["awaiting_billing"] == 1
    assert {x["lab"] for x in o["revenue_by_lab"]} == {"spectrum", "metropolis"}

    only = (await portal.client.get(f"{INS}/finance?labs=metropolis&compare=none", headers=h)).json()
    assert only["kpis"][0]["value"] == "3000.00" and only["kpis"][0]["direction"] is None and only["compare_range"] is None
    assert [r["lab"] for r in only["lab_table"]["rows"]] == ["metropolis"]

    labs = (await portal.client.get(f"{INS}/labs", headers=h)).json()
    assert {c["lab"] for c in labs["lab_cards"]} == {"spectrum", "metropolis"} and labs["lab_cost_statement"]["grand_total"] == "1600.00"

    pt = (await portal.client.get(f"{INS}/patients", headers=h)).json()
    assert {k["key"]: k["value"] for k in pt["kpis"]}["patients"] == 3
    assert {k["key"]: k["value"] for k in pt["kpis"]}["returning_patients"] == 1  # c1 was seen in October


async def test_no_patient_identifiers_anywhere_in_the_responses(portal):
    h = await admin(portal)
    await seed(portal, entry("2026-11-02", net="500", cost="900"))
    for tab in TABS:
        text = (await portal.client.get(f"{INS}/{tab}", headers=h)).text
        for secret in ("Secret Person", "9999999999", "P-SECRET"):
            assert secret not in text, tab


async def test_below_cost_list_and_audit(portal):
    h = await admin(portal)
    await seed(portal, entry("2026-11-02", net="500", cost="900"), entry("2026-11-03", cust="c2"))
    r = (await portal.client.get(f"{INS}/below_cost", headers=h)).json()
    assert r["total"] == 1 and r["items"][0]["margin"] == "-400.00" and set(r["items"][0]) == {
        "bill_no", "date", "lab", "lab_name", "billed", "lab_cost", "margin"}
    actions = [a["action"] async for a in portal.db["audit_logs"].find({})]
    assert "insights.below_cost_review" in actions


async def test_empty_database_gives_friendly_empty_states(portal):
    h = await admin(portal)
    for tab in TABS[:4]:
        r = await portal.client.get(f"{INS}/{tab}", headers=h)
        assert r.status_code == 200 and r.json()["empty"] is True and r.json()["empty_message"]
    assert (await portal.client.get(f"{INS}/report.pdf", headers=h)).status_code == 200


# ───────────── cache ─────────────
async def test_results_are_cached_until_entries_change_or_refresh(portal):
    h = await admin(portal)
    await seed(portal, entry("2026-11-02", net="1000"))
    first = (await portal.client.get(f"{INS}/overview?compare=none", headers=h)).json()
    await portal.db["entries"].insert_one(entry("2026-11-03", net="1000", cust="c2"))  # no event: still cached
    cached = (await portal.client.get(f"{INS}/overview?compare=none", headers=h)).json()
    assert cached["generated_at"] == first["generated_at"] and cached["kpis"][0]["value"] == "1000.00"
    fresh = (await portal.client.get(f"{INS}/overview?compare=none&refresh=true", headers=h)).json()
    assert fresh["kpis"][0]["value"] == "2000.00"
    await portal.db["entries"].insert_one(entry("2026-11-04", net="500", cust="c3"))
    await portal.app.state.events.publish("entry.saved", {})  # what patient_registry publishes on every save
    assert (await portal.client.get(f"{INS}/overview?compare=none", headers=h)).json()["kpis"][0]["value"] == "2500.00"


# ───────────── PDF ─────────────
async def test_pdf_report_download_sections_headers_audit_and_rate_limit(portal):
    h = await admin(portal)
    await seed(portal, entry("2026-11-02"), entry("2026-11-03", lab="metropolis", cust="c2"))
    r = await portal.client.get(f"{INS}/report.pdf?sections=overview,finance", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "TrustUs_Business_Report_2026-11-01_2026-11-12.pdf" in r.headers["content-disposition"]
    with pymupdf.open(stream=r.content, filetype="pdf") as pdf:
        assert pdf[0].get_images(full=True)
        assert "BUSINESS REPORT" in pdf[0].get_text()
    assert (await portal.client.get(f"{INS}/report.pdf?sections=bogus", headers=h)).status_code == 422
    entries = [a async for a in portal.db["audit_logs"].find({"action": "insights.report_download"})]
    assert len(entries) == 1 and "sections=overview,finance" in entries[0]["target"] and "Secret" not in entries[0]["target"]
    codes = [(await portal.client.get(f"{INS}/report.pdf", headers=h)).status_code for _ in range(7)]
    assert codes[-1] == 429


async def test_report_email_sends_the_selected_pdf_to_the_logged_in_admin(portal):
    h = await admin(portal)
    await seed(portal, entry("2026-11-02"))

    r = await portal.client.post(
        f"{INS}/report.email?sections=overview,finance",
        headers=h,
    )
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    sent = portal.mailer.sent[-1]
    assert sent["to"] == "boss@example.com" and "business report" in sent["subject"]
    assert len(sent["attachments"]) == 1
    attachment = sent["attachments"][0]
    assert attachment["filename"] == "TrustUs_Business_Report_2026-11-01_2026-11-12.pdf"
    assert attachment["content_type"] == "application/pdf" and attachment["content"].startswith(b"%PDF")
    audit_entry = [a async for a in portal.db["audit_logs"].find({"action": "insights.report_email"})][0]
    assert "sections=overview,finance" in audit_entry["target"] and "boss@example.com" not in audit_entry["target"]


async def test_first_open_is_audited_once_per_session(portal):
    h = await admin(portal)
    for _ in range(3):
        await portal.client.get(f"{INS}/overview", headers=h)
    assert len([a async for a in portal.db["audit_logs"].find({"action": "insights.open"})]) == 1


# ───────────── done-when: Billed Revenue equals the Excel register ─────────────
async def test_billed_revenue_equals_billed_amount_in_the_excel_register(portal):
    h = await admin(portal)
    pr = f"{API}/patient_registry"
    for i, (day, gross, disc, cost) in enumerate([("2026-10-14", 1500, 100, 900), ("2026-10-20", 800, 0, 500), ("2026-10-21", 2210, 10, 1200)]):
        r = await portal.client.post(
            f"{pr}/entries", headers=h,
            json={"partner_key": "spectrum", "patient_name": f"Person Number{chr(65 + i)}", "registration_date": day,
                  "lab_pid": f"P{i}", "force_new": True, "gross_amount": gross, "discount_amount": disc, "lab_cost": cost},
        )
        assert r.status_code == 201, r.text
    x = await portal.client.get(f"{pr}/excel_register/download?year=2026", headers=h)
    ws = load_workbook(io.BytesIO(x.content))["Oct"]
    headers = [c.value for c in ws[1]]
    col = headers.index("Billed Amount")
    excel_total = sum(float(row[col].value) for row in ws.iter_rows(min_row=2) if row[col].value is not None)
    f = (await portal.client.get(f"{INS}/finance?period=custom&from=2026-10-01&to=2026-10-31&compare=none&refresh=true", headers=h)).json()
    assert float(f["kpis"][0]["value"]) == pytest.approx(excel_total, abs=0.001) == pytest.approx(4400.0)
    assert f["lab_table"]["totals"]["billed_revenue"] == f["kpis"][0]["value"]


# ───────────── monthly email job ─────────────
JOB = f"{INS}/jobs/report_monthly"


async def test_monthly_job_needs_the_secret_and_is_off_by_default(portal):
    portal.app.state.settings.job_secret = "j" * 30
    assert (await portal.client.post(JOB)).status_code == 403
    assert (await portal.client.post(JOB, headers={"X-Job-Secret": "wrong"})).status_code == 403
    r = await portal.client.post(JOB, headers={"X-Job-Secret": "j" * 30})
    assert r.status_code == 200 and r.json()["status"] == "disabled" and not portal.mailer.sent


async def test_monthly_job_sends_once_and_keeps_the_access_portal_working(portal):
    h = await admin(portal)
    sent_before = len(portal.mailer.sent)
    portal.app.state.settings.job_secret = "j" * 30
    portal.app.state.settings.admin_report_emails = "a@example.com,b@example.com"
    portal.app.state.config.insights.report.monthly_email = True
    await seed(portal, entry("2026-10-14"))
    first = await portal.client.post(JOB, headers={"X-Job-Secret": "j" * 30})
    assert first.status_code == 200 and first.json()["status"] == "sent" and first.json()["sent"] == 2
    assert {m["to"] for m in portal.mailer.sent[sent_before:]} == {"a@example.com", "b@example.com"}
    again = await portal.client.post(JOB, headers={"X-Job-Secret": "j" * 30})
    assert again.json()["status"] == "already_sent" and len(portal.mailer.sent) == sent_before + 2
    # the module_state document written by the job must not break the Access Portal
    assert (await portal.client.get(f"{API}/access/modules", headers=h)).status_code == 200
    assert (await portal.client.get(f"{API}/me/modules", headers=h)).status_code == 200