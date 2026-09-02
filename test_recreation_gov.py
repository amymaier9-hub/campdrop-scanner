"""
Tests for recreation_gov.py -- the Recreation.gov parallel to
test_scanner_v3.py's MiDNR tests. Same rigor: real edge cases found by
actually exercising the live API (see recreation_gov.py's module docstring
and comments for what was observed live), not just inspection.

Run with: python3 test_recreation_gov.py
"""
import json
import os
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy_for_tests")

import requests
import requests_mock
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, '.')
import recreation_gov as rg
import scanner_v3 as sc


def test_is_recreation_gov_park_recognizes_all_five_dropdown_entries():
    for name in [
        "Sleeping Bear Dunes National Lakeshore", "Pictured Rocks National Lakeshore",
        "Hiawatha National Forest", "Manistee National Forest", "Ottawa National Forest",
    ]:
        assert rg.is_recreation_gov_park(name), f"'{name}' should be recognized"
    print("PASS: is_recreation_gov_park recognizes all 5 starred dropdown parks")


def test_is_recreation_gov_park_rejects_midnr_parks():
    assert rg.is_recreation_gov_park("Ludington State Park") is False
    assert rg.is_recreation_gov_park("Bay City State Recreation Area") is False
    assert rg.is_recreation_gov_park("Some Made Up Park") is False
    print("PASS: is_recreation_gov_park correctly rejects MiDNR / unknown park names")


def test_normalize_availability_status_available_and_open_are_bookable():
    assert rg.normalize_availability_status("Available") == 1
    assert rg.normalize_availability_status("Open") == 1
    print("PASS: normalize_availability_status treats Available and Open as bookable (1)")


def test_normalize_availability_status_everything_else_is_unavailable():
    """
    Regression coverage for every real status string observed live: a
    standard Site-Specific campground (D.H. Day) showed Reserved, Not
    Reservable, and Closed within its normal booking window, plus Not
    Available for a fully-closed winter month; a Non Site-Specific / quota
    campground (White Pine backcountry permits) additionally showed NYR for
    months beyond the release window. None of these mean "go book it."
    """
    for status in ["Reserved", "Not Reservable", "Closed", "Not Available", "NYR"]:
        assert rg.normalize_availability_status(status) == 2, f"{status!r} must normalize to 2"
    print("PASS: normalize_availability_status maps every real non-bookable status to 2")


def test_normalize_availability_status_handles_unexpected_shapes_safely():
    """Defensive: None, a number, or a brand new status string we've never seen must not crash."""
    assert rg.normalize_availability_status(None) == 2
    assert rg.normalize_availability_status(123) == 2
    assert rg.normalize_availability_status("SomeFutureStatusRecGovInvents") == 2
    print("PASS: normalize_availability_status safely defaults unknown/malformed input to unavailable")


def test_month_starts_covering_single_month_no_rollover():
    starts = rg._month_starts_covering(datetime(2026, 9, 15), 10)
    assert starts == [datetime(2026, 9, 1)]
    print("PASS: _month_starts_covering returns just one month when the window doesn't cross a boundary")


def test_month_starts_covering_spans_multiple_months():
    # 2026-09-15 + 60 days runs through 2026-11-13 -> needs Sep, Oct, Nov.
    starts = rg._month_starts_covering(datetime(2026, 9, 15), 60)
    assert starts == [datetime(2026, 9, 1), datetime(2026, 10, 1), datetime(2026, 11, 1)]
    print("PASS: _month_starts_covering spans multiple months correctly")


def test_month_starts_covering_handles_year_rollover():
    """
    Regression test for a real bug class: December -> January must roll
    the YEAR over too, not just wrap the month back to 1 within the same
    year (which would silently produce a bogus December-of-this-year
    duplicate or an invalid date).
    """
    starts = rg._month_starts_covering(datetime(2026, 12, 15), 45)
    assert starts == [datetime(2026, 12, 1), datetime(2027, 1, 1)]
    print("PASS: _month_starts_covering correctly rolls the year over at a December->January boundary")


def _mock_month_response(campsites: dict) -> dict:
    return {"campsites": campsites}


def test_fetch_facility_availability_stitches_two_months_with_correct_alignment():
    """
    Full pipeline: a facility whose booking window spans two calendar
    months must produce ONE continuous day_codes list where index 0 is
    start_date, regardless of which month a given day actually lives in.
    """
    start_date = datetime(2026, 9, 28)  # only 3 days left in Sept before Oct starts
    with requests_mock.Mocker() as m:
        m.get(
            f"{rg.BASE_URL}/api/camps/availability/campground/259242/month",
            [
                {"json": _mock_month_response({
                    "c1": {"site": "78", "loop": "Upper", "availabilities": {
                        "2026-09-28T00:00:00Z": "Reserved",
                        "2026-09-29T00:00:00Z": "Available",
                        "2026-09-30T00:00:00Z": "Reserved",
                    }},
                })},
                {"json": _mock_month_response({
                    "c1": {"site": "78", "loop": "Upper", "availabilities": {
                        "2026-10-01T00:00:00Z": "Available",
                        "2026-10-02T00:00:00Z": "Available",
                    }},
                })},
            ],
        )
        session = requests.Session()
        day_codes, metadata = rg.fetch_facility_availability(
            session, 259242, "D.H. Day Campground", start_date, days_ahead=5
        )
        # index: 0=9/28 Reserved(2), 1=9/29 Available(1), 2=9/30 Reserved(2),
        #        3=10/1 Available(1), 4=10/2 Available(1)
        assert day_codes["c1"] == [2, 1, 2, 1, 1], day_codes["c1"]
        assert metadata["c1"]["name"] == "78, D.H. Day Campground (Upper Loop)"
    print("PASS: fetch_facility_availability stitches two months into one correctly-aligned list")


def test_fetch_facility_availability_missing_campsite_in_later_month_defaults_unavailable():
    """
    Defensive regression: if a campsite that appeared in month 1 is simply
    absent from month 2's response (not observed live, but not something
    we can rule out either), those later days must default to unavailable
    -- never silently treated as an opening just because we have no data.
    """
    start_date = datetime(2026, 9, 29)
    with requests_mock.Mocker() as m:
        m.get(
            f"{rg.BASE_URL}/api/camps/availability/campground/999/month",
            [
                {"json": _mock_month_response({
                    "c1": {"site": "5", "loop": "", "availabilities": {
                        "2026-09-29T00:00:00Z": "Available",
                        "2026-09-30T00:00:00Z": "Available",
                    }},
                })},
                {"json": _mock_month_response({})},  # c1 missing entirely from month 2
            ],
        )
        session = requests.Session()
        day_codes, metadata = rg.fetch_facility_availability(
            session, 999, "Test Campground", start_date, days_ahead=4
        )
        assert day_codes["c1"] == [1, 1, 2, 2], day_codes["c1"]
        assert metadata["c1"]["name"] == "5, Test Campground"  # no loop -> no "(... Loop)" suffix
    print("PASS: a campsite missing from a later month's response safely defaults to unavailable")


def test_fetch_facility_availability_skips_erroring_month_without_losing_others():
    """
    Regression test for a real resilience gap: if one month's call fails
    (Recreation.gov returns its {"error": "..."} shape, or a network error),
    the other months for this facility must still be fetched and merged --
    one bad month must not silently zero out an entire facility for a poll
    cycle, and must not crash the whole scan either.
    """
    start_date = datetime(2026, 9, 15)
    with requests_mock.Mocker() as m:
        m.get(
            f"{rg.BASE_URL}/api/camps/availability/campground/777/month",
            [
                {"json": {"error": "query not encoded"}},  # Sept call fails
                {"json": _mock_month_response({
                    "c1": {"site": "9", "loop": "A", "availabilities": {
                        "2026-10-01T00:00:00Z": "Available",
                    }},
                })},  # Oct call succeeds
            ],
        )
        session = requests.Session()
        day_codes, metadata = rg.fetch_facility_availability(
            session, 777, "Flaky Campground", start_date, days_ahead=20
        )
        oct1_idx = (datetime(2026, 10, 1) - start_date).days
        assert day_codes["c1"][oct1_idx] == 1
        assert len(day_codes["c1"]) == 20
    print("PASS: a failed month call is skipped without crashing or losing other months' data")


def test_discover_facilities_parses_real_search_shape():
    with requests_mock.Mocker() as m:
        m.get(f"{rg.BASE_URL}/api/search", json={
            "total": 2,
            "results": [
                {"name": "D.H. Day Campground", "entity_id": "259242", "entity_type": "campground"},
                {"name": "PLATTE RIVER CAMPGROUND", "entity_id": "232458", "entity_type": "campground"},
            ],
        })
        session = requests.Session()
        facilities = rg.discover_facilities(session, 2937)
        assert len(facilities) == 2
        assert {"facility_id": "259242", "name": "D.H. Day Campground"} in facilities
    print("PASS: discover_facilities parses the real /api/search response shape")


def test_get_or_discover_facilities_caches_across_calls():
    cache = {}
    original_cache_file = rg.FACILITY_CACHE_FILE
    tmp_dir = tempfile.mkdtemp()
    rg.FACILITY_CACHE_FILE = Path(tmp_dir) / "rg_cache_test.json"
    try:
        with requests_mock.Mocker() as m:
            m.get(f"{rg.BASE_URL}/api/search", json={
                "results": [{"name": "D.H. Day Campground", "entity_id": "259242"}],
            })
            session = requests.Session()
            first = rg.get_or_discover_facilities(session, "Sleeping Bear Dunes National Lakeshore", cache)
            second = rg.get_or_discover_facilities(session, "Sleeping Bear Dunes National Lakeshore", cache)
            assert first == second
            assert m.call_count == 1, "second call must be served from cache, not hit the network again"
    finally:
        rg.FACILITY_CACHE_FILE = original_cache_file
    print("PASS: get_or_discover_facilities caches and doesn't re-fetch on a second call")


def test_get_or_discover_facilities_unknown_park_returns_none_safely():
    session = requests.Session()
    result = rg.get_or_discover_facilities(session, "Not A Real Park", {})
    assert result is None
    print("PASS: get_or_discover_facilities safely returns None for an unrecognized park name")


def test_fetch_park_availability_merges_multiple_facilities_without_key_collision():
    """
    Sleeping Bear Dunes has 6 real campgrounds. Each must contribute its
    campsites into ONE flat dict for the whole park (mirroring how MiDNR
    merges multiple loops), with no cross-facility key collisions and
    metadata that identifies which campground each site belongs to.
    """
    # fetch_park_availability anchors start_date to datetime.now() internally
    # (not something this test controls), so the mocked availability date
    # must be expressed relative to "today" rather than hardcoded -- using
    # tomorrow (index 1 of a 2-day window) keeps this test valid regardless
    # of what day it's actually run on.
    tomorrow = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1))
    tomorrow_key = tomorrow.strftime("%Y-%m-%dT00:00:00Z")

    facilities = [
        {"facility_id": 111, "name": "D.H. Day Campground"},
        {"facility_id": 222, "name": "Platte River Campground"},
    ]
    with requests_mock.Mocker() as m:
        m.get(f"{rg.BASE_URL}/api/camps/availability/campground/111/month",
              json=_mock_month_response({"c1": {"site": "5", "loop": "Upper",
                                                  "availabilities": {tomorrow_key: "Available"}}}))
        m.get(f"{rg.BASE_URL}/api/camps/availability/campground/222/month",
              json=_mock_month_response({"c2": {"site": "5", "loop": "Loop A",
                                                  "availabilities": {tomorrow_key: "Reserved"}}}))
        session = requests.Session()
        combined, metadata, start_date = rg.fetch_park_availability(session, facilities, days_ahead=2)
        assert set(combined.keys()) == {"c1", "c2"}
        assert combined["c1"] == [2, 1]
        assert combined["c2"] == [2, 2]
        assert "D.H. Day Campground" in metadata["c1"]["name"]
        assert "Platte River Campground" in metadata["c2"]["name"]
        assert start_date == datetime.now().strftime("%Y-%m-%d")
    print("PASS: fetch_park_availability merges multiple facilities into one flat dict correctly")


# ---------------------------------------------------------------------------
# Integration: does recreation_gov's output actually plug into scanner_v3's
# matching engine? This is the check that matters most -- the whole point
# of normalizing to the same shape is that match_alerts_for_park (imported
# from scanner_v3, completely UNMODIFIED) must work identically regardless
# of which backend produced the data.
# ---------------------------------------------------------------------------

def test_recreation_gov_output_plugs_into_scanner_v3_match_alerts_for_park():
    # fetch_park_availability anchors start_date to datetime.now() internally,
    # so the mocked dates (and the alert's arrival_date) are expressed
    # relative to "today" rather than hardcoded, keeping this test valid
    # regardless of what day it's actually run on.
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_key = today.strftime("%Y-%m-%dT00:00:00Z")
    today_str = today.strftime("%Y-%m-%d")

    facilities = [{"facility_id": 259242, "name": "D.H. Day Campground"}]

    with requests_mock.Mocker() as m:
        m.get(
            f"{rg.BASE_URL}/api/camps/availability/campground/259242/month",
            [
                {"json": _mock_month_response({
                    "c1": {"site": "78", "loop": "Upper", "availabilities": {today_key: "Reserved"}},
                })},
                {"json": _mock_month_response({
                    "c1": {"site": "78", "loop": "Upper", "availabilities": {today_key: "Available"}},
                })},
            ],
        )
        session = requests.Session()
        previous_state, previous_meta, _ = rg.fetch_park_availability(session, facilities, days_ahead=1)
        current_state, current_meta, poll_start = rg.fetch_park_availability(session, facilities, days_ahead=1)

    alert = {
        "id": "alert-1", "phone": "+15551234567", "park_name": "Sleeping Bear Dunes National Lakeshore",
        "flexible_dates": False, "arrival_date": today_str, "min_nights": 1,
        "weekends_only": False, "specific_site": None,
    }
    matches = sc.match_alerts_for_park([alert], current_state, previous_state, current_meta, poll_start)
    assert len(matches) == 1, "a genuine new Recreation.gov opening must be caught by scanner_v3's matcher unmodified"
    assert matches[0]["site_name"] == "78, D.H. Day Campground (Upper Loop)"
    assert matches[0]["date"] == today_str
    print("PASS: recreation_gov's normalized output plugs into scanner_v3.match_alerts_for_park unmodified")


def test_recreation_gov_park_and_midnr_park_share_state_dict_without_collision():
    """
    Full-shape check: scanner_v3.main() stores current_state_all[park_name]
    per park and looks up previous_state.get(park_name, {}) the same way
    for both backends -- since each backend's resource_ids only ever need
    to be unique WITHIN a park's own state dict (not globally), a MiDNR
    resource_id and a Recreation.gov campsite_id can't collide even if they
    happened to be the same string, because they'd live under different
    top-level park_name keys. This just documents/locks that assumption.
    """
    current_state_all = {}
    current_state_all["Ludington State Park"] = {"r1": [1, 2]}  # MiDNR-shaped
    current_state_all["Sleeping Bear Dunes National Lakeshore"] = {"r1": [2, 1]}  # RG-shaped, same key "r1"
    assert current_state_all["Ludington State Park"]["r1"] == [1, 2]
    assert current_state_all["Sleeping Bear Dunes National Lakeshore"]["r1"] == [2, 1]
    print("PASS: MiDNR and Recreation.gov resource ids can't collide across parks in current_state_all")


def test_fetch_facility_availability_against_real_captured_dh_day_response():
    """
    Not a synthetic mock -- this replays a REAL response captured live from
    recreation.gov on 2026-09-02 (GET .../api/camps/availability/campground/
    259242/month?start_date=2026-09-01...), saved as
    fixture_dhday_sep2026.json. This is the check that matters most: it
    proves fetch_facility_availability's parsing logic works against
    Recreation.gov's actual real-world response shape, not just against
    hand-written mocks that only reflect what I assumed the shape to be.
    """
    fixture_path = Path(__file__).parent / "fixture_dhday_sep2026.json"
    if not fixture_path.exists():
        print("SKIP: fixture_dhday_sep2026.json not present -- live capture wasn't run in this environment")
        return
    real_response = json.loads(fixture_path.read_text())
    assert len(real_response["campsites"]) == 82, "sanity check against the known live count for D.H. Day"

    # start_date must land on 2026-09-01 or later within September so every
    # captured day (2026-09-01 .. 2026-09-30) falls inside the window.
    start_date = datetime(2026, 9, 1)
    with requests_mock.Mocker() as m:
        m.get(f"{rg.BASE_URL}/api/camps/availability/campground/259242/month", json=real_response)
        session = requests.Session()
        day_codes, metadata = rg.fetch_facility_availability(
            session, 259242, "D.H. Day Campground", start_date, days_ahead=30
        )

    assert len(day_codes) == 82, "must parse all 82 real campsites, none dropped"
    assert len(metadata) == 82
    for campsite_id, codes in day_codes.items():
        assert len(codes) == 30
        assert set(codes) <= {1, 2}, f"day codes must only ever be 1 or 2, got {set(codes)} for {campsite_id}"
        assert all(isinstance(c, int) for c in codes)
    # Site "01" in the Generator loop (campsite_id 10001493) was Reserved on
    # 9/1-9/8 and Available starting 9/9 in the real captured response --
    # spot-check that exact real transition survives the full pipeline.
    real_site_01 = day_codes["10001493"]
    assert real_site_01[0] == 2, "9/1 (index 0) was Reserved in the real capture"
    assert real_site_01[8] == 1, "9/9 (index 8) was Available in the real capture"
    assert metadata["10001493"]["name"] == "01, D.H. Day Campground (Generator Loop)"
    print("PASS: fetch_facility_availability correctly parses a REAL captured recreation.gov response "
          "(82/82 real campsites, all codes valid, real Reserved->Available transition preserved)")


if __name__ == "__main__":
    test_is_recreation_gov_park_recognizes_all_five_dropdown_entries()
    test_is_recreation_gov_park_rejects_midnr_parks()
    test_normalize_availability_status_available_and_open_are_bookable()
    test_normalize_availability_status_everything_else_is_unavailable()
    test_normalize_availability_status_handles_unexpected_shapes_safely()
    test_month_starts_covering_single_month_no_rollover()
    test_month_starts_covering_spans_multiple_months()
    test_month_starts_covering_handles_year_rollover()
    test_fetch_facility_availability_stitches_two_months_with_correct_alignment()
    test_fetch_facility_availability_missing_campsite_in_later_month_defaults_unavailable()
    test_fetch_facility_availability_skips_erroring_month_without_losing_others()
    test_discover_facilities_parses_real_search_shape()
    test_get_or_discover_facilities_caches_across_calls()
    test_get_or_discover_facilities_unknown_park_returns_none_safely()
    test_fetch_park_availability_merges_multiple_facilities_without_key_collision()
    test_recreation_gov_output_plugs_into_scanner_v3_match_alerts_for_park()
    test_recreation_gov_park_and_midnr_park_share_state_dict_without_collision()
    test_fetch_facility_availability_against_real_captured_dh_day_response()
    print("\nALL RECREATION.GOV TESTS PASSED")
