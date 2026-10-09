import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_fub import FakeClient  # noqa: E402
from fub_data.config import load_criteria  # noqa: E402
from fub_data.discover import run_discovery  # noqa: E402
from fub_data.report import run_report  # noqa: E402
from fub_data.window import build_window  # noqa: E402
from fub_data.zips import normalize_zip  # noqa: E402


def test_default_window_is_last_full_week():
    w = build_window(today=date(2026, 10, 7))  # a Wednesday
    assert w.label == "2026-09-28_to_2026-10-04"


def test_explicit_window():
    assert build_window("2026-09-28").label == "2026-09-28_to_2026-10-04"
    assert build_window("2026-09-28", "2026-09-30").label == "2026-09-28_to_2026-09-30"


def test_normalize_zip():
    assert normalize_zip("85003-1234") == "85003"
    assert normalize_zip(2702) == "02702"
    assert normalize_zip("n/a") is None


def test_report_end_to_end(tmp_path):
    criteria = load_criteria()
    window = build_window("2026-09-28")
    s = run_report(FakeClient(), criteria, window, tmp_path)

    rows = {int(r["fub_id"]): r for r in csv.DictReader(open(tmp_path / f"funnel_{window.label}.csv"))}
    assert set(rows) == {103, 104, 105, 106, 107, 108, 109, 110}  # 102 is Sep 27 in Pacific time
    assert rows[109]["primary_unqualified_reason"] == "zillow_active"
    assert rows[108]["primary_unqualified_reason"] == "rural_geo"
    assert rows[104]["primary_unqualified_reason"] == "licensed_agent"
    assert "no_zip_found" in rows[104]["review_flags"]
    assert rows[107]["status"] == "Opportunity"          # inbound text
    assert rows[106]["status"] == "Qualified"            # outbound rep text ignored
    assert rows[105]["status"] == "Opportunity"          # stage
    assert rows[105]["geo_tier"] == "core_metro" and rows[105]["market"] == "Nashville"
    assert rows[103]["primary_unqualified_reason"] == "short_sale_or_foreclosure"  # customNotesRedFlags
    assert rows[110]["geo_tier"] == "res_core"
    assert rows[110]["status"] == "Qualified" and rows[110]["market"] == "Phoenix"
    assert rows[110]["zip_origin"] == "event_property"

    assert s["leads_created"] == 8
    assert s["unqualified"] == 4
    assert s["opportunities"] == 2
    assert s["qualified_including_opportunities"] == 4
    assert s["by_stage"]["Referred Out/Appointment Set"] == {"Opportunity": 1}
    geo = {g["segment"]: g for g in s["by_geography"]}
    assert geo["RES core zips"]["leads"] == 5
    assert geo["Core market metro (outside RES list)"]["opportunities"] == 1
    core = geo["Core markets overall"]
    assert core["leads"] == 6 and core["qualified_incl_opps"] == 4
    # every rate is out of all 8 leads, not out of the segment
    assert core["leads_pct_of_total"] == "75.0%" and core["qualified_pct_of_total"] == "50.0%"
    assert geo["RES core zips"]["qualified_pct_of_total"] == "37.5%"
    geo_csv = list(csv.reader(open(tmp_path / f"funnel_{window.label}_geography.csv")))
    assert [r[0] for r in geo_csv[1:]] == ["RES core zips", "Core market metro (outside RES list)", "Core markets overall"]
    assert geo_csv[1][1:] == ["8", "5", "62.5%", "3", "37.5%", "1", "12.5%"]
    assert geo["Outside all lists (rural / out of area)"]["leads"] == 1
    assert geo["No zip found"]["leads"] == 1 and geo["All leads"]["leads"] == 8
    assert (tmp_path / f"funnel_{window.label}_summary.md").exists()


def test_discovery_runs(tmp_path):
    out = run_discovery(FakeClient(), load_criteria(), build_window("2026-09-28"), tmp_path, sample_size=8)
    assert out["people_in_window"] == 8
    assert out["person_fields"]["stage"]["top_values"]
    assert out["activity_sample"]["by_kind"]["deals"]["fetch_errors"] == 8
    assert any("Appointment Met" in x for x in out["config_check"])
    assert out["person_fields"]["contacted"]["top_values"]
    assert out["activity_sample"]["by_kind"]["texts"]["direction_field_present"] is True


def test_not_contacted_counts_zero(tmp_path):
    import fake_fub
    orig = list(fake_fub.PEOPLE)
    fake_fub.PEOPLE[:] = [fake_fub.person(200, "2026-09-29T18:00:00Z", contacted=0,
                                          addresses=[{"code": "85004"}])]
    try:
        s = run_report(FakeClient(), load_criteria(), build_window("2026-09-28"), tmp_path)
    finally:
        fake_fub.PEOPLE[:] = orig
    assert s["qualified_not_yet_contacted"] == 1


def test_trash_is_unqualified(tmp_path):
    import fake_fub
    orig = list(fake_fub.PEOPLE)
    fake_fub.PEOPLE[:] = [
        fake_fub.person(300, "2026-09-29T18:00:00Z", stage="Trash", addresses=[{"code": "85004"}]),
        fake_fub.person(301, "2026-09-29T17:00:00Z", stage="Trash", addresses=[{"code": "59001"}]),
        fake_fub.person(302, "2026-09-29T16:00:00Z", addresses=[{"code": "28202"}]),  # Charlotte: other preferred metro
    ]
    try:
        s = run_report(FakeClient(), load_criteria(), build_window("2026-09-28"), tmp_path)
    finally:
        fake_fub.PEOPLE[:] = orig
    rows = {int(r["fub_id"]): r for r in csv.DictReader(open(tmp_path / "funnel_2026-09-28_to_2026-10-04.csv"))}
    assert rows[300]["status"] == "Unqualified" and rows[300]["primary_unqualified_reason"] == "trash"
    assert rows[301]["primary_unqualified_reason"] == "rural_geo"
    assert rows[301]["all_unqualified_reasons"] == "rural_geo; trash"
    assert rows[302]["status"] == "Qualified" and rows[302]["geo_tier"] == "other_metro"
    assert s["unqualified"] == 2


def test_overflow_leads_are_unqualified_but_counted(tmp_path):
    import fake_fub
    orig = list(fake_fub.PEOPLE)
    fake_fub.PEOPLE[:] = [
        fake_fub.person(600, "2026-09-29T18:00:00Z", addresses=[{"code": "85004"}]),
        fake_fub.person(601, "2026-09-29T16:00:00Z", stage="Trash", tags=["Bonus Overflow Lead"],
                        addresses=[{"code": "28202"}]),
    ]
    try:
        s = run_report(FakeClient(), load_criteria(), build_window("2026-09-28"), tmp_path)
    finally:
        fake_fub.PEOPLE[:] = orig
    rows = {int(r["fub_id"]): r for r in csv.DictReader(open(tmp_path / "funnel_2026-09-28_to_2026-10-04.csv"))}
    assert rows[601]["status"] == "Unqualified" and rows[601]["primary_unqualified_reason"] == "overflow"
    assert s["leads_created"] == 2 and s["unqualified"] == 1
    geo = {g["segment"]: g for g in s["by_geography"]}
    assert geo["RES core zips"]["total_leads"] == 2 and geo["RES core zips"]["qualified_pct_of_total"] == "50.0%"


def test_diverted_tags_option(tmp_path):
    import fake_fub
    orig = list(fake_fub.PEOPLE)
    fake_fub.PEOPLE[:] = [
        fake_fub.person(400, "2026-09-29T18:00:00Z", addresses=[{"code": "85004"}]),            # RES core, qualified
        fake_fub.person(401, "2026-09-29T17:00:00Z", addresses=[{"code": "80002"}]),            # RES core, qualified
        fake_fub.person(402, "2026-09-29T16:00:00Z", stage="Trash", tags=["Bonus Overflow Lead"],
                        addresses=[{"code": "28202"}]),                                           # diverted
        fake_fub.person(403, "2026-09-29T15:00:00Z", addresses=[{"code": "59001"}]),            # rural
    ]
    crit = load_criteria()
    crit["diverted_tags"] = ["Bonus Overflow Lead"]
    try:
        s = run_report(FakeClient(), crit, build_window("2026-09-28"), tmp_path)
    finally:
        fake_fub.PEOPLE[:] = orig
    rows = {int(r["fub_id"]): r for r in csv.DictReader(open(tmp_path / "funnel_2026-09-28_to_2026-10-04.csv"))}
    assert rows[402]["pool"] == "diverted" and rows[402]["status"].startswith("Diverted")
    assert s["leads_created_all"] == 4 and s["diverted_not_sent_to_res"] == 1
    assert s["leads_created"] == 3 and s["unqualified"] == 1
    geo = {g["segment"]: g for g in s["by_geography"]}
    assert geo["RES core zips"]["total_leads"] == 3
    assert geo["RES core zips"]["qualified_pct_of_total"] == "66.7%"
    md = (tmp_path / "funnel_2026-09-28_to_2026-10-04_summary.md").read_text()
    assert "Diverted to 3rd party" in md and "**Total leads** | **3**" in md


def test_hap_info_requested_needs_sms_review(tmp_path):
    import fake_fub
    orig_p, orig_t = list(fake_fub.PEOPLE), dict(fake_fub.ACTIVITY["textMessages"])
    fake_fub.PEOPLE[:] = [
        fake_fub.person(500, "2026-09-29T18:00:00Z", stage="HAP Info Requested", addresses=[{"code": "85004"}]),
        fake_fub.person(501, "2026-09-29T17:00:00Z", stage="HAP Info Requested", addresses=[{"code": "85004"}]),
        fake_fub.person(502, "2026-09-29T16:00:00Z", stage="Sourcing Cash Offers", addresses=[{"code": "85004"}]),
    ]
    fake_fub.ACTIVITY["textMessages"][500] = [
        {"id": 9, "isIncoming": False, "created": "2026-09-30T10:00:00Z", "message": "Here is the HAP info you asked for"},
        {"id": 10, "isIncoming": True, "created": "2026-09-30T11:00:00Z", "message": "Thanks, still deciding"},
    ]
    fake_fub.ACTIVITY["textMessages"][501] = [
        {"id": 11, "isIncoming": True, "created": "2026-09-30T11:00:00Z", "message": "Sure, I'd like to talk to an agent"},
    ]
    try:
        s = run_report(FakeClient(), load_criteria(), build_window("2026-09-28"), tmp_path)
    finally:
        fake_fub.PEOPLE[:] = orig_p
        fake_fub.ACTIVITY["textMessages"].clear(); fake_fub.ACTIVITY["textMessages"].update(orig_t)
    rows = {int(r["fub_id"]): r for r in csv.DictReader(open(tmp_path / "funnel_2026-09-28_to_2026-10-04.csv"))}
    assert rows[500]["status"] == "Qualified"                         # not automatically an opp
    assert "hap_info_requested_check_sms" in rows[500]["review_flags"]
    assert rows[500]["recent_sms"].startswith("[2026-09-30 US] Here is the HAP info")
    assert "[2026-09-30 LEAD] Thanks, still deciding" in rows[500]["recent_sms"]
    assert rows[501]["status"] == "Opportunity" and not rows[501]["review_flags"]   # SMS shows agreement
    assert rows[502]["status"] == "Opportunity"                       # Sourcing Cash Offers
    assert s["review_flags"] == {"hap_info_requested_check_sms": 1}
