"""
Tests for scanner_v3's matching, dry-run SMS, and dedup logic.
Run with: SUPABASE_SERVICE_ROLE_KEY=dummy python3 test_scanner_v3.py
"""
import json
import os
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy_for_tests")

import requests
import requests_mock
import sys
from pathlib import Path
sys.path.insert(0, '.')
import scanner_v3 as sc


def test_normalize_park_name_strips_common_suffixes():
    assert sc.normalize_park_name("Bay City State Recreation Area") == "bay city"
    assert sc.normalize_park_name("Bay City State Park") == "bay city"
    assert sc.normalize_park_name("Ludington State Park") == "ludington"
    assert sc.normalize_park_name("Fayette Historic State Park") == "fayette"
    print("PASS: normalize_park_name strips common park-type suffixes correctly")


def test_lookup_park_location_exact_match():
    all_locations = {
        "exact": {"Ludington State Park": {"resource_location_id": -1, "root_map_id": -2}},
        "normalized": {"ludington": {"resource_location_id": -1, "root_map_id": -2}},
    }
    result = sc.lookup_park_location("Ludington State Park", all_locations)
    assert result == {"resource_location_id": -1, "root_map_id": -2}
    print("PASS: lookup_park_location finds an exact match")


def test_lookup_park_location_normalized_fallback_real_bay_city_case():
    """
    Regression test for the actual bug found in live testing: Aspira's own
    fullName for Bay City is "Bay City State Park", but our site (correctly)
    calls it "Bay City State Recreation Area". These must still match via
    the normalized fallback.
    """
    all_locations = {
        "exact": {
            "Bay City": {"resource_location_id": -2147483636, "root_map_id": -2147483625},
            "Bay City State Park": {"resource_location_id": -2147483636, "root_map_id": -2147483625},
        },
        "normalized": {
            "bay city": {"resource_location_id": -2147483636, "root_map_id": -2147483625},
        },
    }
    result = sc.lookup_park_location("Bay City State Recreation Area", all_locations)
    assert result is not None, "Bay City State Recreation Area must resolve via normalized fallback"
    assert result["resource_location_id"] == -2147483636
    print("PASS: lookup_park_location resolves the real Bay City naming mismatch via normalized fallback")


def test_lookup_park_location_no_match_returns_none():
    all_locations = {"exact": {}, "normalized": {}}
    result = sc.lookup_park_location("Nonexistent Park", all_locations)
    assert result is None
    print("PASS: lookup_park_location safely returns None for an unknown park")


def test_fetch_all_resource_locations_indexes_both_short_and_full_name():
    """
    Regression test: previously, when a location had a shortName, fullName
    was silently discarded from the index entirely -- meaning a park name
    that matched fullName exactly (like the real "Ludington State Park"
    case) would still fail to be found.
    """
    with requests_mock.Mocker() as m:
        m.get(f"{sc.BASE_URL}/api/resourceLocation", json=[
            {
                "resourceLocationId": -2147483562, "rootMapId": -999,
                "localizedValues": [
                    {"cultureName": "en-US", "shortName": "Ludington", "fullName": "Ludington State Park"}
                ],
            }
        ])
        session = requests.Session()
        all_locations = sc.fetch_all_resource_locations(session)
        assert "Ludington" in all_locations["exact"]
        assert "Ludington State Park" in all_locations["exact"], (
            "fullName must still be indexed even when shortName is also present"
        )
    print("PASS: fetch_all_resource_locations indexes both shortName and fullName")


def test_normalize_availability_code_handles_plain_int():
    assert sc.normalize_availability_code(1) == 1
    assert sc.normalize_availability_code(0) == 0
    print("PASS: normalize_availability_code passes through plain ints unchanged")


def test_normalize_availability_code_handles_dict_shape():
    """
    Regression test for a real crash found in live testing: the API can
    return {"availability": 5, "remainingQuota": null} instead of a plain
    int for a site with an active cart hold at that moment.
    """
    assert sc.normalize_availability_code({"availability": 5, "remainingQuota": None}) == 5
    assert sc.normalize_availability_code({"remainingQuota": None}) == 0  # missing key -> safe default
    print("PASS: normalize_availability_code correctly unwraps the dict shape")


def test_fetch_park_availability_normalizes_mixed_shapes():
    """
    Full pipeline version: a loop response with a mix of plain ints and
    dict-shaped entries (as seen live) must not crash, and must produce a
    clean list of plain ints.
    """
    with requests_mock.Mocker() as m:
        m.get(f"{sc.BASE_URL}/api/availability/map", json={
            "resourceAvailabilities": {
                "normalSite": [1, 2, 1],
                "heldSite": [1, {"availability": 5, "remainingQuota": None}, 2],
            }
        })
        session = requests.Session()
        combined, _ = sc.fetch_park_availability(session, [-1])
        assert combined["normalSite"] == [1, 2, 1]
        assert combined["heldSite"] == [1, 5, 2]
        assert all(isinstance(c, int) for c in combined["heldSite"])
    print("PASS: fetch_park_availability normalizes mixed int/dict shapes without crashing")


def test_match_alerts_refires_after_site_gets_rebooked_then_reopens():
    """
    Regression test for a real design gap found by the user: a site that
    becomes available, then gets booked again, then opens up a SECOND time
    later must trigger a fresh match -- the diff logic (comparing only to
    the immediately preceding poll) naturally supports this, since it has
    no memory of "we already texted about this once, ever."
    """
    metadata = {"r1": {"name": "Site 42"}}
    alert = {
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": False, "arrival_date": "2026-09-04", "min_nights": 1,
        "weekends_only": False, "specific_site": None,
    }

    # Poll 1: baseline, was unavailable.
    poll1 = {"r1": [2]}
    # Poll 2: opened up -- first match should fire.
    poll2 = {"r1": [1]}
    matches_1 = sc.match_alerts_for_park([alert], poll2, poll1, metadata, "2026-09-04")
    assert len(matches_1) == 1, "First opening must fire a match"

    # Poll 3: someone else booked it -- back to unavailable.
    poll3 = {"r1": [2]}
    matches_2 = sc.match_alerts_for_park([alert], poll3, poll2, metadata, "2026-09-04")
    assert len(matches_2) == 0, "Going unavailable again must not fire a match"

    # Poll 4: it opens up AGAIN -- this must fire a fresh match.
    poll4 = {"r1": [1]}
    matches_3 = sc.match_alerts_for_park([alert], poll4, poll3, metadata, "2026-09-04")
    assert len(matches_3) == 1, "A genuine SECOND opening of the same site+date must fire a new match"
    print("PASS: a site that reopens after being rebooked correctly fires a fresh match")


def test_discover_loop_maps_flat_tree_bay_city_shape():
    """Bay City's real shape: root -> 7 direct leaf loops, no deeper nesting."""
    with requests_mock.Mocker() as m:
        def responder(request, context):
            map_id = request.qs['mapid'][0]
            if map_id == '-2147483625':  # root
                return {"mapLinkAvailabilities": {str(i): [1] for i in range(-2147483624, -2147483617)}}
            return {"mapLinkAvailabilities": {}, "resourceAvailabilities": {"x": [1]}}
        m.get(f"{sc.BASE_URL}/api/availability/map", json=responder)
        session = requests.Session()
        leaves = sc.discover_loop_maps(session, -2147483625)
        assert len(leaves) == 7
    print("PASS: discover_loop_maps handles a flat (2-level) tree correctly")


def test_discover_loop_maps_deep_tree_real_ludington_shape():
    """
    Regression test for a real bug found in live testing: Ludington's map
    tree is 3 levels deep (root -> some intermediate nodes with ZERO
    resources of their own -> real leaf loops). The old single-level
    discovery treated intermediate nodes as leaves and silently dropped
    everything beneath them, undercounting 394 real sites as just 31.
    This test uses the EXACT real map IDs and shape discovered live.
    """
    tree = {
        -2147483318: {"links": [-2147483317, -2147483313, -2147483310, -2147483309, -2147483306, -2147483305]},
        -2147483317: {"links": [-2147483316, -2147483315, -2147483314]},  # intermediate, no resources
        -2147483313: {"links": [-2147483312, -2147483311]},              # intermediate, no resources
        -2147483310: {"links": [], "resourceCount": 10},                  # real leaf
        -2147483309: {"links": [-2147483308, -2147483307]},              # intermediate, no resources
        -2147483306: {"links": [], "resourceCount": 1},                   # real leaf
        -2147483305: {"links": [], "resourceCount": 20},                  # real leaf
        -2147483316: {"links": [], "resourceCount": 53},
        -2147483315: {"links": [], "resourceCount": 50},
        -2147483314: {"links": [], "resourceCount": 45},
        -2147483312: {"links": [], "resourceCount": 52},
        -2147483311: {"links": [], "resourceCount": 63},
        -2147483308: {"links": [], "resourceCount": 48},
        -2147483307: {"links": [], "resourceCount": 52},
    }

    with requests_mock.Mocker() as m:
        def responder(request, context):
            map_id = int(request.qs['mapid'][0])
            node = tree[map_id]
            return {
                "mapLinkAvailabilities": {str(l): [1] for l in node["links"]},
                "resourceAvailabilities": {
                    f"r{map_id}_{i}": [1] for i in range(node.get("resourceCount", 0))
                },
            }
        m.get(f"{sc.BASE_URL}/api/availability/map", json=responder)
        session = requests.Session()
        leaves = sc.discover_loop_maps(session, -2147483318)

        # Must find exactly the 10 true leaves, not the 3 intermediate nodes.
        assert len(leaves) == 10, f"Expected 10 real leaves, got {len(leaves)}: {leaves}"
        assert -2147483317 not in leaves, "Intermediate node must NOT be treated as a leaf"
        assert -2147483313 not in leaves, "Intermediate node must NOT be treated as a leaf"
        assert -2147483309 not in leaves, "Intermediate node must NOT be treated as a leaf"

        # Verify the leaves collectively account for the full real total (394).
        total_resources = sum(tree[leaf].get("resourceCount", 0) for leaf in leaves)
        assert total_resources == 394, f"Expected 394 total sites, got {total_resources}"
    print("PASS: discover_loop_maps correctly recurses through a 3-level tree and finds all 394 real sites")


def test_discover_loop_maps_single_loop_park():
    """A park where the root map itself has no sub-links but does have direct resources."""
    with requests_mock.Mocker() as m:
        m.get(f"{sc.BASE_URL}/api/availability/map",
              json={"mapLinkAvailabilities": {}, "resourceAvailabilities": {"1": [1]}})
        session = requests.Session()
        leaves = sc.discover_loop_maps(session, -999)
        assert leaves == [-999]
    print("PASS: single-loop park (root has no sub-links) is treated as its own leaf")


def test_load_last_state_normalizes_stale_dict_shaped_data():
    """
    Regression test for a real second-round crash: a state file saved by an
    older/crashed version of this script can have leftover dict-shaped day
    codes on disk. Loading it back in must normalize those too, not just
    freshly-fetched data.
    """
    import tempfile
    stale_data = {
        "Bay City State Recreation Area": {
            "r1": [1, {"availability": 5, "remainingQuota": None}, 2]
        }
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(stale_data, f)
        temp_path = f.name

    original_state_file = sc.STATE_FILE
    sc.STATE_FILE = Path(temp_path)
    try:
        loaded = sc.load_last_state()
        assert loaded["Bay City State Recreation Area"]["r1"] == [1, 5, 2]
        assert all(isinstance(c, int) for c in loaded["Bay City State Recreation Area"]["r1"])
    finally:
        sc.STATE_FILE = original_state_file
        os.remove(temp_path)
    print("PASS: load_last_state normalizes stale dict-shaped data from disk")


def test_find_available_runs_basic():
    # day0=avail, day1=avail, day2=unavail, day3=avail, day4=avail, day5=avail
    codes = [1, 1, 2, 1, 1, 1]
    runs = sc.find_available_runs(codes, "2026-09-01", min_nights=2)
    # Used for FLEXIBLE alerts only -- reports one start per contiguous run
    # (day3-4-5 is one real opening, not two), to avoid duplicate texts.
    assert "2026-09-01" in runs  # day0-1 run
    assert "2026-09-04" in runs  # day3 starts the day3-4-5 run
    assert "2026-09-05" not in runs  # part of the SAME run as day3, not separate
    assert "2026-09-03" not in runs  # day2 is unavailable, can't start a run there
    assert len(runs) == 2
    print("PASS: find_available_runs (flexible-date path) reports one start per contiguous run")


def test_find_available_runs_respects_min_nights():
    codes = [1, 2, 1, 1]  # only day2-3 is a 2-night run
    runs = sc.find_available_runs(codes, "2026-09-01", min_nights=3)
    assert runs == []  # no 3-night run exists
    print("PASS: find_available_runs correctly returns nothing when no run is long enough")


def test_exact_date_matches_non_first_day_of_a_run():
    """
    THE BUG THIS TEST GUARDS AGAINST: an exact-date alert for a date that is
    NOT the first day of a longer available run must still match, since the
    date itself is genuinely available. Using find_available_runs for this
    (which only reports first-of-run dates) would incorrectly miss it.
    """
    # day3, day4, day5 all just became available (were all unavailable before).
    current = [2, 2, 2, 1, 1, 1]
    previous = [2, 2, 2, 2, 2, 2]
    # Someone wants exactly day5 (index 4 = 2026-09-05), 1 night.
    result = sc.is_exact_date_newly_available(current, previous, "2026-09-01", "2026-09-05", min_nights=1)
    assert result is True, "Exact-date match for a non-first day of a run must still succeed"
    print("PASS: exact-date alert correctly matches a date that isn't the first day of a run")


def test_exact_date_does_not_rematch_already_available_window():
    """If the requested window was already fully available last poll, it's not a NEW opening."""
    current = [1, 1, 1]
    previous = [1, 1, 1]  # already available before, nothing changed
    result = sc.is_exact_date_newly_available(current, previous, "2026-09-01", "2026-09-01", min_nights=2)
    assert result is False
    print("PASS: exact-date check does not re-fire on an already-available window")


def test_exact_date_requires_full_window_available():
    """A 2-night request needs BOTH nights open, not just one."""
    current = [1, 2]  # only first night available
    previous = [2, 2]
    result = sc.is_exact_date_newly_available(current, previous, "2026-09-01", "2026-09-01", min_nights=2)
    assert result is False
    print("PASS: exact-date check requires the FULL requested window to be available")


def test_exact_date_out_of_range_is_safe():
    """A target date outside the polled window should not crash, just return False."""
    current = [1, 1, 1]
    previous = [2, 2, 2]
    result = sc.is_exact_date_newly_available(current, previous, "2026-09-01", "2026-12-25", min_nights=1)
    assert result is False
    print("PASS: exact-date check handles out-of-range dates safely")


def test_match_alerts_exact_date_basic_match():
    current_state = {"r1": [1, 1, 2]}
    previous_state = {"r1": [2, 2, 2]}
    metadata = {"r1": {"name": "Site 42"}}
    alerts = [{
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": False, "arrival_date": "2026-09-01", "min_nights": 1,
        "weekends_only": False, "specific_site": None,
    }]
    matches = sc.match_alerts_for_park(alerts, current_state, previous_state, metadata, "2026-09-01")
    assert len(matches) == 1
    assert matches[0]["site_name"] == "Site 42"
    assert matches[0]["date"] == "2026-09-01"
    print("PASS: match_alerts_for_park finds a correct exact-date match")


def test_match_alerts_exact_date_second_day_of_run():
    """Full pipeline version of the bug-fix test above."""
    current_state = {"r1": [2, 2, 2, 1, 1, 1]}
    previous_state = {"r1": [2, 2, 2, 2, 2, 2]}
    metadata = {"r1": {"name": "Site 42"}}
    alerts = [{
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": False, "arrival_date": "2026-09-05", "min_nights": 1,  # index 4, not the run's first day
        "weekends_only": False, "specific_site": None,
    }]
    matches = sc.match_alerts_for_park(alerts, current_state, previous_state, metadata, "2026-09-01")
    assert len(matches) == 1
    assert matches[0]["date"] == "2026-09-05"
    print("PASS: full match pipeline correctly matches an exact date mid-run")


def test_match_alerts_for_park_specific_site_filter():
    current_state = {"r1": [1, 1], "r2": [1, 1]}
    previous_state = {"r1": [2, 2], "r2": [2, 2]}
    metadata = {"r1": {"name": "Site 42"}, "r2": {"name": "Site 99"}}
    alerts = [{
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": False, "arrival_date": "2026-09-01", "min_nights": 1,
        "weekends_only": False, "specific_site": "Site 99",
    }]
    matches = sc.match_alerts_for_park(alerts, current_state, previous_state, metadata, "2026-09-01")
    assert len(matches) == 1
    assert matches[0]["site_name"] == "Site 99"
    print("PASS: specific_site filter correctly excludes non-matching sites")


def test_match_alerts_for_park_no_match_wrong_date():
    current_state = {"r1": [1, 1]}
    previous_state = {"r1": [2, 2]}
    metadata = {"r1": {"name": "Site 1"}}
    alerts = [{
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": False, "arrival_date": "2026-12-25", "min_nights": 1,  # out of polled range
        "weekends_only": False, "specific_site": None,
    }]
    matches = sc.match_alerts_for_park(alerts, current_state, previous_state, metadata, "2026-09-01")
    assert len(matches) == 0
    print("PASS: no match fires when opening doesn't fall in the alert's requested date")


def test_match_alerts_flexible_date_basic_match():
    current_state = {"r1": [2, 2, 1, 1]}
    previous_state = {"r1": [2, 2, 2, 2]}
    metadata = {"r1": {"name": "Site 1"}}
    alerts = [{
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": True, "arrival_window_start": "2026-09-01", "arrival_window_end": "2026-09-10",
        "min_nights": 2, "weekends_only": False, "specific_site": None,
    }]
    matches = sc.match_alerts_for_park(alerts, current_state, previous_state, metadata, "2026-09-01")
    assert len(matches) == 1
    assert matches[0]["date"] == "2026-09-03"  # index 2
    print("PASS: flexible-date match finds a correct new run within the window")


def test_match_alerts_suppresses_on_first_observation():
    current_state = {"r1": [1, 1, 1]}
    previous_state = {}  # never seen before
    metadata = {"r1": {"name": "Site 1"}}
    alerts = [{
        "id": "alert-1", "phone": "+15551234567", "park_name": "Test Park",
        "flexible_dates": False, "arrival_date": "2026-09-01", "min_nights": 1,
        "weekends_only": False, "specific_site": None,
    }]
    matches = sc.match_alerts_for_park(alerts, current_state, previous_state, metadata, "2026-09-01")
    assert len(matches) == 0
    print("PASS: no false-positive flood on first-ever observation with real alerts present")


def test_send_sms_dry_run_does_not_hit_network():
    sc.DRY_RUN = True
    session = requests.Session()
    # No mock registered -- if this tries to hit the real network, it will raise.
    success, sid = sc.send_sms(session, "+15551234567", "test message")
    assert success is True
    assert sid is None
    print("PASS: dry-run SMS never touches the network")


def test_log_sms_sent_matches_real_schema():
    with requests_mock.Mocker() as m:
        m.post(f"{sc.SUPABASE_URL}/rest/v1/sms_log", status_code=201)
        session = requests.Session()
        sc.log_sms_sent(session, "alert-1", "r1", "Site 1", "2026-09-01", "Test Park",
                         "+15551234567", "hello", None, True)
        sent_body = m.request_history[0].json()
        expected_keys = {"alert_id", "phone", "message", "twilio_sid", "status",
                          "resource_id", "site_name", "site_date", "park_name", "sent_at"}
        assert expected_keys.issubset(sent_body.keys())
        assert sent_body["status"] == "dry_run"
    print("PASS: log_sms_sent sends a body matching the real sms_log schema")


def test_has_been_sms_logged_dedup_check():
    with requests_mock.Mocker() as m:
        m.get(f"{sc.SUPABASE_URL}/rest/v1/sms_log", json=[{"id": "existing-row"}])
        session = requests.Session()
        result = sc.has_been_sms_logged(session, "alert-1", "r1", "2026-09-01")
        assert result is True
    print("PASS: has_been_sms_logged correctly detects an existing log entry")


if __name__ == "__main__":
    test_match_alerts_refires_after_site_gets_rebooked_then_reopens()
    test_discover_loop_maps_flat_tree_bay_city_shape()
    test_discover_loop_maps_deep_tree_real_ludington_shape()
    test_discover_loop_maps_single_loop_park()
    test_load_last_state_normalizes_stale_dict_shaped_data()
    test_normalize_availability_code_handles_plain_int()
    test_normalize_availability_code_handles_dict_shape()
    test_fetch_park_availability_normalizes_mixed_shapes()
    test_normalize_park_name_strips_common_suffixes()
    test_lookup_park_location_exact_match()
    test_lookup_park_location_normalized_fallback_real_bay_city_case()
    test_lookup_park_location_no_match_returns_none()
    test_fetch_all_resource_locations_indexes_both_short_and_full_name()
    test_find_available_runs_basic()
    test_find_available_runs_respects_min_nights()
    test_exact_date_matches_non_first_day_of_a_run()
    test_exact_date_does_not_rematch_already_available_window()
    test_exact_date_requires_full_window_available()
    test_exact_date_out_of_range_is_safe()
    test_match_alerts_exact_date_basic_match()
    test_match_alerts_exact_date_second_day_of_run()
    test_match_alerts_for_park_specific_site_filter()
    test_match_alerts_for_park_no_match_wrong_date()
    test_match_alerts_flexible_date_basic_match()
    test_match_alerts_suppresses_on_first_observation()
    test_send_sms_dry_run_does_not_hit_network()
    test_log_sms_sent_matches_real_schema()
    test_has_been_sms_logged_dedup_check()
    print("\nALL TESTS PASSED")
