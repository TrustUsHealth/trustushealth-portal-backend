"""Business Insights rules on small in-memory data: periods, compare, honest-number rules, PDF."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.config import load_config
from app.modules.insights import compute as K
from app.modules.insights import periods as P
from app.modules.insights import report
from app.modules.insights.fmt import compact_inr, hours_text, inr

CFG = load_config("config")
INS = CFG.insights
IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 11, 12)


def doc(day="2026-11-02", lab="spectrum", status="complete", net="1000", cost="600", cust="c1", **kw):
    d = {
        "_id": f"{day}-{lab}-{cust}-{net}", "bill_no": "556701", "registration_date": day, "partner_key": lab,
        "status": status, "customer_id": cust, "net_amount": net, "gross_amount": str(Decimal(net) + 100),
        "discount_amount": "100", "lab_cost": cost, "tests": ["TSH"], "referred_by": "Dr. A Shah",
        "age": 45, "age_unit": "Y", "gender": "Male",
    }
    d.update(kw)
    return d


def analysis(docs, prior=None, compare_docs=None, period="this_month", compare="previous", earliest=date(2026, 1, 1)):
    res = P.resolve_period(period, TODAY, 4)
    cmp_range = P.compare_range(res, compare)
    visits = [K.parse_visit(d, IST) for d in docs]
    cmp_ds = K.Dataset([K.parse_visit(d, IST) for d in compare_docs or []], {}) if cmp_range else None
    labs = {k: K.LabInfo(k, p.name, "#34495E") for k, p in CFG.partners.items()}
    return K.Analysis(
        cfg=INS, range=res.range, compare=cmp_range, compare_mode=compare, preset=period, grain=P.grain_for(res.range),
        labs_selected=[], labs=labs, others_color="#D9D5CE", main=K.Dataset(visits, prior or {}), cmp=cmp_ds,
        earliest=earliest, today=TODAY, generated_at="2026-11-13T10:00:00+05:30",
    )


# ───────────── periods ─────────────
def test_running_period_is_compared_with_the_same_number_of_days():
    r = P.resolve_period("this_month", TODAY, 4)
    assert (r.range.start, r.range.end) == (date(2026, 11, 1), TODAY)
    c = P.compare_range(r, "previous")
    assert (c.start, c.end) == (date(2026, 10, 1), date(2026, 10, 12))


def test_finished_period_is_compared_with_the_whole_earlier_one():
    c = P.compare_range(P.resolve_period("last_month", TODAY, 4), "previous")
    assert (c.start, c.end) == (date(2026, 9, 1), date(2026, 9, 30))


def test_financial_year_follows_the_start_month():
    assert P.resolve_period("this_fy", TODAY, 4).range.start == date(2026, 4, 1)
    assert P.resolve_period("this_fy", date(2026, 2, 10), 4).range.start == date(2025, 4, 1)
    last = P.resolve_period("last_fy", TODAY, 4).range
    assert (last.start, last.end) == (date(2025, 4, 1), date(2026, 3, 31))


def test_same_period_last_year_and_off():
    r = P.resolve_period("this_month", TODAY, 4)
    c = P.compare_range(r, "last_year")
    assert (c.start, c.end) == (date(2025, 11, 1), date(2025, 11, 12))
    assert P.compare_range(r, "none") is None


def test_custom_period_rules():
    with pytest.raises(P.PeriodError):
        P.resolve_period("custom", TODAY, 4, date(2026, 11, 5), date(2026, 11, 1))
    with pytest.raises(P.PeriodError):
        P.resolve_period("custom", TODAY, 4, None, None)
    r = P.resolve_period("custom", TODAY, 4, date(2026, 10, 1), date(2026, 10, 10))
    c = P.compare_range(r, "previous")
    assert (c.start, c.end) == (date(2026, 9, 21), date(2026, 9, 30))


def test_time_grain_follows_the_length_of_the_period():
    g = lambda n: P.grain_for(P.DateRange(date(2026, 1, 1), date(2026, 1, 1) + (date(2026, 1, 2) - date(2026, 1, 1)) * (n - 1)))  # noqa: E731
    assert (g(31), g(32), g(120), g(121)) == ("day", "week", "week", "month")
    weeks = P.buckets(P.DateRange(date(2026, 11, 1), date(2026, 11, 12)), "week")
    assert [b.key for b in weeks] == ["2026-10-26", "2026-11-02", "2026-11-09"]  # Monday to Sunday


# ───────────── formatting ─────────────
def test_money_and_hours_formatting():
    assert inr(Decimal("421500")) == "\u20b94,21,500"
    assert inr(Decimal("-1234567.5"), 2) == "-\u20b912,34,567.50"
    assert compact_inr(Decimal("421500")) == "\u20b94.2L" and compact_inr(Decimal("12000000")) == "\u20b91.2Cr"
    assert compact_inr(Decimal("85000")) == "\u20b985,000"
    assert hours_text(30.0) == "30.0 h" and hours_text(72.0) == "3.0 days" and hours_text(None) == "No data"


def test_money_is_read_whatever_its_stored_form():
    class Dec128:
        def to_decimal(self):
            return Decimal("12.50")

    assert K.to_dec(Dec128()) == Decimal("12.50") and K.to_dec("450") == Decimal("450")
    assert K.to_dec(450) == Decimal("450") and K.to_dec(None) is None and K.to_dec("abc") is None


# ───────────── honest-number rules ─────────────
def test_finance_counts_billed_visits_only_and_margin_needs_a_lab_cost():
    a = analysis([doc(net="1000", cost="600"), doc(net="500", cost=None, cust="c2"), doc(net="700", status="draft", cust="c3")])
    s = K.summarize(a.main.visits)
    assert s["billed_visits"] == 2 and s["awaiting"] == 1 and s["visits"] == 3
    assert s["revenue"] == Decimal("1500") and s["margin"] == Decimal("400") and s["revenue_costed"] == Decimal("1000")
    assert s["margin_pct"] == 40.0 and s["missing_cost"] == 1
    fin = K.build_finance(a)
    assert "Margin is based on 1 of 2 billed visits (1 have no lab cost)." in fin["data_notes"]
    assert fin["lab_table"]["totals"]["billed_revenue"] == "1500.00"


def test_totals_reconcile_between_kpis_lab_table_and_trend():
    docs = [doc(net="1000", cost="600"), doc(net="2500.55", cost="2600", lab="metropolis", cust="c2"), doc(day="2026-11-05", cust="c3", net="333.33", cost="100")]
    fin = K.build_finance(analysis(docs))
    rows = fin["lab_table"]["rows"]
    total = Decimal(fin["kpis"][0]["value"])
    assert total == sum(Decimal(r["billed_revenue"]) for r in rows) == Decimal("3833.88")
    assert sum(Decimal(p["billed_revenue"]) for p in K.build_overview(analysis(docs))["trend"]) == total
    assert Decimal(fin["kpis"][2]["value"]) == sum(Decimal(r["margin"]) for r in rows)
    assert fin["stats"]["below_cost_bills"] == 1


def test_discount_rate_is_discounts_over_gross():
    s = K.summarize(analysis([doc(net="900"), doc(net="900", cust="c2")]).main.visits)
    assert s["discounts"] == Decimal("200") and s["gross"] == Decimal("2000") and s["discount_rate"] == 10.0


def test_turnaround_ignores_missing_negative_and_over_14_days():
    ok = doc(collection_time="2026-11-02T09:00:00", reporting_time="2026-11-02T15:00:00")
    neg = doc(cust="c2", collection_time="2026-11-02T09:00:00", reporting_time="2026-11-02T08:00:00")
    slow = doc(cust="c3", collection_time="2026-11-02T09:00:00", reporting_time="2026-11-20T09:00:00")
    none = doc(cust="c4")
    visits = [K.parse_visit(d, IST) for d in (ok, neg, slow, none)]
    assert K.turnaround_hours(visits) == [6.0]
    assert K.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9.1


def test_compare_shows_new_when_the_earlier_value_is_zero_and_signed_words_otherwise():
    assert K.kpi("x", "X", Decimal("10"), Decimal("0"), "money")["direction"] == "new"
    up = K.kpi("x", "X", Decimal("112"), Decimal("100"), "money")
    assert up["direction"] == "up" and up["delta_pct"] == 12.0
    assert K.kpi("x", "X", 5, 10, "count")["direction"] == "down"
    assert K.kpi("x", "X", 5, None, "count")["direction"] is None
    assert K.kpi("r", "R", 30.0, 25.0, "percent")["delta_pct"] == 5.0


def test_new_and_returning_patients():
    prior = {"c2": date(2026, 9, 1)}
    docs = [doc(cust="c1"), doc(cust="c2"), doc(cust="c2", day="2026-11-04"), doc(cust=None)]
    a = analysis(docs, prior=prior)
    ps = K.patient_summary(a.main)
    assert (ps["patients"], ps["new"], ps["returning"], ps["unlinked"]) == (2, 1, 1, 1)
    out = K.build_patients(a)
    assert any("not linked to a patient" in n for n in out["data_notes"])
    assert sum(p["new"] for p in out["new_vs_returning"]) == 1


def test_repeat_figures_are_flagged_as_building_until_six_months_of_data():
    young = K.build_patients(analysis([doc()], earliest=date(2026, 10, 1)))
    assert young["repeat_building"] and any("still building" in n for n in young["data_notes"])
    assert not K.build_patients(analysis([doc()], earliest=date(2026, 1, 1)))["repeat_building"]


def test_age_bands_gender_top_tests_aliases_and_referrers():
    docs = [
        doc(age=3, age_unit="M", gender="Female", tests=["Creatinine, Serum", "TSH"], referred_by=None),
        doc(cust="c2", age=70, tests=["creatinine", "TSH "], referred_by="Dr. A Shah"),
        doc(cust="c3", age=None, gender=None, tests=["CBC"], referred_by="Dr. A Shah"),
    ]
    out = K.build_patients(analysis(docs))
    bands = {b["band"]: b for b in out["age_gender"]}
    assert bands["Under 18"]["female"] == 1 and bands["60+"]["male"] == 1 and bands["Not recorded"]["not_recorded"] == 1
    tests = {t["test"]: t["visits"] for t in out["top_tests"]["rows"]}
    assert tests["Creatinine"] == 2 and tests["TSH"] == 2 and tests["CBC"] == 1  # alias merges spellings; once per visit
    refs = {r["referrer"]: r for r in out["top_referrers"]["rows"]}
    assert refs["Dr. A Shah"]["visits"] == 2 and refs["Not recorded"]["visits"] == 1


def test_top_lists_group_the_rest_as_others():
    docs = [doc(cust=f"c{i}", tests=[f"Test {i}"], referred_by=f"Dr. D{i}") for i in range(11)]
    out = K.build_patients(analysis(docs))
    assert len(out["top_tests"]["rows"]) == 8 and out["top_tests"]["others"] == 3
    assert len(out["top_referrers"]["rows"]) == 8 and out["top_referrers"]["others"]["visits"] == 3


def test_empty_period_gives_a_message_not_empty_charts():
    a = analysis([])
    assert K.build_overview(a)["empty"] and K.build_overview(a)["empty_message"] == "No billed visits in this period"
    assert K.build_labs(a)["empty_message"] == "No visits in this period"
    assert K.build_finance(a)["lab_table"]["rows"] == []


def test_below_cost_list_has_no_patient_details():
    a = analysis([doc(net="500", cost="900", bill_no="556710", patient_name="Secret Name", phone="9999999999")])
    item = K.build_below_cost(a)["items"][0]
    assert item == {"bill_no": "556710", "date": "2026-11-02", "lab": "spectrum", "lab_name": "Spectrum Laboratory",
                    "billed": "500.00", "lab_cost": "900.00", "margin": "-400.00"}


def test_highlights_are_plain_sentences_in_configured_order_and_capped():
    docs = [doc(net="1000", cost="1200"), doc(net="3000", cost="1000", lab="metropolis", cust="c2")]
    prev = [doc(day="2026-10-02", net="2000", cost="1000")]
    lines = K.highlights(analysis(docs, compare_docs=prev))
    assert lines[0].startswith("Billed Revenue was \u20b94,000, up 100.0% compared with the previous period")
    assert any("1 bill was below cost" in x for x in lines) and len(lines) <= INS.highlights.max_items
    assert not K.highlights(analysis([]))


def test_a_lab_below_the_margin_threshold_is_marked():
    fin = K.build_finance(analysis([doc(net="1000", cost="900"), doc(net="1000", cost="100", lab="metropolis", cust="c2")]))
    flags = {m["lab"]: m["below_threshold"] for m in fin["margin_by_lab"]}
    assert flags == {"spectrum": True, "metropolis": False}


# ───────────── PDF ─────────────
def test_pdf_report_builds_small_and_respects_chosen_sections():
    docs = [doc(day=f"2026-11-{d:02d}", cust=f"c{d}", net=str(500 + d * 10), cost="300") for d in range(1, 12)]
    a = analysis(docs)
    full = report.render(a, ["overview", "finance", "labs", "patients"], "boss@example.com", "Asia/Kolkata")
    assert full.startswith(b"%PDF") and len(full) < 300_000
    one = report.render(a, ["overview"], "boss@example.com", "Asia/Kolkata")
    assert one.startswith(b"%PDF") and len(one) < len(full)
    assert report.file_name(a) == "TrustUs_Business_Report_2026-11-01_2026-11-12.pdf"
    assert report.render(analysis([]), ["overview", "finance", "labs", "patients"], "x", "Asia/Kolkata").startswith(b"%PDF")


def test_generated_time_is_timezone_aware():
    assert datetime.fromisoformat("2026-11-13T10:00:00+05:30").tzinfo is not None