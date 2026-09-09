"""
CampDrop Scanner - v3
Adds: per-subscriber matching (date window, min nights, site preference,
weekends-only, specific site), Twilio SMS sending, and sms_log-based
duplicate prevention.

SAFETY: DRY_RUN defaults to True. In dry-run mode, no real SMS is sent --
matches are printed and still logged to sms_log (marked dry_run=true) so you
can verify matching/dedup logic against real data before your Twilio
compliance profile is approved. Flip DRY_RUN to False (and set real Twilio
env vars) when ready to go live.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import recreation_gov as rg

BASE_URL = "https://midnrreservations.com"
# The scanner is a trusted backend process, so it uses the SERVICE ROLE key
# (never the public anon key the website uses) -- this is required to read
# subscriber phone numbers and read/write sms_log, which are intentionally
# NOT exposed to the public anon key for privacy reasons.
SUPABASE_URL = "https://xviqwcivsmjjrduasamo.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY","").strip()
if not SUPABASE_SERVICE_ROLE_KEY:
    print("FATAL: SUPABASE_SERVICE_ROLE_KEY environment variable is not set.")
    print("Set it with: export SUPABASE_SERVICE_ROLE_KEY=\"your-key-here\"")
    sys.exit(1)

# --- Twilio config (set these as real environment variables when approved) ---
# DRY_RUN defaults to True (safe) unless explicitly set to "false" in the
# environment -- this lets you flip it on Railway without editing code.
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()

POLL_INTERVAL_SECONDS = 60
DAYS_AHEAD = 120

AVAILABLE_CODES = {1, 5}

STATE_FILE = Path(__file__).parent / "last_known_state_v3.json"
PARK_MAP_CACHE_FILE = Path(__file__).parent / "park_map_cache.json"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "app-language": "en-US",
    "app-version": "5.113.275",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}
SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
}


# ---------------------------------------------------------------------------
# Supabase: full alert rows (not just park names), plus sms_log read/write
# ---------------------------------------------------------------------------

def fetch_active_alerts(session: requests.Session) -> list:
    """Returns full alert rows for every active alert."""
    url = f"{SUPABASE_URL}/rest/v1/alerts"
    params = {"select": "*", "status": "eq.active"}
    resp = session.get(url, headers=SUPABASE_HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def has_been_sms_logged(session: requests.Session, alert_id: str, resource_id: str, date_str: str) -> bool:
    """Checks sms_log for a prior text about this exact (alert, site, date)
    combo. NOT used to gate sending in the main loop (see the comment where
    matches are processed for why) -- kept available for admin/debugging
    queries and as a building block for a future "already notified" UI."""
    url = f"{SUPABASE_URL}/rest/v1/sms_log"
    params = {
        "select": "id",
        "alert_id": f"eq.{alert_id}",
        "resource_id": f"eq.{resource_id}",
        "site_date": f"eq.{date_str}",
    }
    resp = session.get(url, headers=SUPABASE_HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return len(resp.json()) > 0


def log_sms_sent(session: requests.Session, alert_id: str, resource_id: str, site_name: str,
                  date_str: str, park_name: str, phone: str, message: str, twilio_sid, dry_run: bool) -> None:
    url = f"{SUPABASE_URL}/rest/v1/sms_log"
    body = {
        "alert_id": alert_id,
        "phone": phone,
        "message": message,
        "twilio_sid": twilio_sid,
        "status": "dry_run" if dry_run else "sent",
        "resource_id": resource_id,
        "site_name": site_name,
        "site_date": date_str,
        "park_name": park_name,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = session.post(
        url,
        headers={**SUPABASE_HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
        json=body,
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Twilio SMS sending
# ---------------------------------------------------------------------------

def send_sms(session: requests.Session, to_phone: str, message: str) -> tuple:
    """
    Sends a real SMS via Twilio's REST API, unless DRY_RUN is True, in which
    case it just prints what would have been sent. Returns (success, twilio_sid_or_None).
    """
    if DRY_RUN:
        print(f"    [DRY RUN] Would text {to_phone}: {message}")
        return True, None

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        print("    ERROR: DRY_RUN is False but Twilio credentials are not set. Skipping send.")
        return False, None

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    resp = session.post(
        url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        data={"To": to_phone, "From": TWILIO_FROM_NUMBER, "Body": message},
        timeout=15,
    )
    if resp.status_code >= 300:
        print(f"    ERROR sending SMS to {to_phone}: {resp.status_code} {resp.text}")
        return False, None
    return True, resp.json().get("sid")


def build_message(park_name: str, site_name: str, date_str: str, nights: int,
                   booking_url: str, loop_name: str = None) -> str:
    """
    booking_url must be a direct link into the correct booking site for
    THIS park -- MiDNRReservations.com for MiDNR parks, recreation.gov for
    Recreation.gov parks. Callers MUST pass the right one; sending a
    subscriber a recreation.gov link for a MiDNR opening (or vice versa)
    would be actively wrong, not just imprecise. See main()'s dispatch
    branch and build_midnr_booking_url() for how each is built per park.

    loop_name is MiDNR-specific (see fetch_loop_names()) -- Recreation.gov
    already bakes its loop info straight into site_name (see
    recreation_gov.py's fetch_facility_availability), so passing loop_name
    there would double it up. None/empty just omits it from the site line
    rather than showing an awkward blank -- e.g. a brand-new site the loop
    lookup hasn't caught up to yet still gets a text, just without a loop.
    """
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    departure_obj = date_obj + timedelta(days=nights)
    friendly_date = date_obj.strftime("%b %-d")
    date_range = (
        f"{friendly_date}–{departure_obj.day}" if departure_obj.month == date_obj.month
        else f"{friendly_date}–{departure_obj.strftime('%b %-d')}"
    )
    if loop_name:
        site_line = f"{loop_name}, Site {site_name}"
    elif "," in site_name:
        # Recreation.gov's site_name already IS a full descriptive string
        # (e.g. "Site 01, D.H. Day Campground (Loop A Loop)" -- see
        # recreation_gov.py's fetch_facility_availability) -- prepending
        # "Site " again here would double it up ("Site Site 01, ...").
        site_line = site_name
    else:
        site_line = f"Site {site_name}"
    return (
        f"\U0001F3D5️ Site open at {park_name} — {site_line}. "
        f"{date_range}, {nights} night{'s' if nights != 1 else ''}. "
        f"Act fast — cancellations rebook in seconds.\n"
        f"Book now: {booking_url}"
    )


def build_midnr_booking_url(resource_location_id, map_id, date_str: str, nights: int) -> str:
    """
    Builds a direct link into MiDNRReservations.com's live availability
    list for the specific LOOP this site belongs to, pre-filled with the
    subscriber's dates -- as close to a one-click "go book this" link as
    Aspira's booking engine supports.

    Unlike Recreation.gov, Aspira has no stable public URL for a single
    campsite -- picking one only happens inside an active shopping-cart
    session (confirmed live 2026-09-02: clicking "Reserve" on a specific
    site creates a cartUid/bookingUid-scoped session and lands on
    /create-booking/reservationmessages, not a bookmarkable per-site page).
    So this lands the subscriber on the correct loop's site list instead,
    with the right park/loop/dates already selected -- their specific site
    will be right there on the list, just not pre-selected for them.

    Verified live that resourceLocationId + mapId + startDate + endDate +
    nights + isReserving=true is enough on its own (no cart/session state
    required) to deep-link a fresh visitor straight to that loop.

    Falls back to the plain homepage if resource_location_id or map_id is
    unexpectedly missing, rather than emitting a broken/malformed link.
    """
    if not resource_location_id or not map_id:
        return "https://midnrreservations.com"
    arrival = datetime.strptime(date_str, "%Y-%m-%d")
    departure = arrival + timedelta(days=nights)
    return (
        "https://midnrreservations.com/create-booking/results?"
        f"resourceLocationId={resource_location_id}&mapId={map_id}"
        f"&startDate={arrival:%Y-%m-%d}&endDate={departure:%Y-%m-%d}"
        f"&nights={nights}&isReserving=true"
    )


# ---------------------------------------------------------------------------
# MiDNR Aspira API (unchanged from v2)
# ---------------------------------------------------------------------------

_PARK_SUFFIX_RE = re.compile(
    r"\s+(state park|state recreation area|state harbor|historic state park|"
    r"state game area|national forest|national lakeshore)$",
    re.IGNORECASE,
)


def normalize_park_name(name: str) -> str:
    """
    Strips common trailing park-type suffixes and normalizes case/whitespace,
    so e.g. "Bay City State Recreation Area" and "Bay City State Park" (a
    real naming mismatch between our site and Aspira's own internal data)
    both normalize down to "bay city" and can still be matched.
    """
    name = name.strip().lower()
    name = _PARK_SUFFIX_RE.sub("", name)
    return name.strip()


def fetch_all_resource_locations(session: requests.Session) -> dict:
    """
    One-time (cacheable) lookup: park display name -> {resourceLocationId, rootMapId}.
    Indexes by BOTH shortName and fullName (not just whichever is preferred),
    since our own site's park names may match either -- or neither exactly,
    which is why a normalized fallback index is also built.
    """
    url = f"{BASE_URL}/api/resourceLocation"
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    raw = resp.json()

    exact_lookup = {}
    normalized_lookup = {}

    for loc in raw:
        lv_list = loc.get("localizedValues", [])
        lv = next((l for l in lv_list if l.get("cultureName") == "en-US"), {})
        info = {"resource_location_id": loc["resourceLocationId"], "root_map_id": loc["rootMapId"]}

        for candidate_name in (lv.get("shortName"), lv.get("fullName")):
            if not candidate_name:
                continue
            exact_lookup[candidate_name] = info
            norm = normalize_park_name(candidate_name)
            normalized_lookup.setdefault(norm, info)

    return {"exact": exact_lookup, "normalized": normalized_lookup}


def lookup_park_location(park_name: str, all_locations: dict):
    """
    Tries an exact name match first, then falls back to normalized matching
    (stripping "State Park" / "State Recreation Area" / etc suffixes) to
    handle real naming differences between our site and Aspira's own data.
    """
    if park_name in all_locations["exact"]:
        return all_locations["exact"][park_name]
    norm = normalize_park_name(park_name)
    return all_locations["normalized"].get(norm)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _plus_days_str(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def query_map_for_discovery(session: requests.Session, map_id: int) -> dict:
    """Single lightweight query used only for tree discovery (1-night window)."""
    params = {
        "mapId": map_id, "bookingCategoryId": 0, "equipmentCategoryId": -32768,
        "subEquipmentCategoryId": -32764, "startDate": _today_str(), "endDate": _plus_days_str(1),
        "getDailyAvailability": "false", "isReserving": "false", "filterData": "[]",
        "boatLength": 0, "boatDraft": 0, "boatWidth": 0,
        "peopleCapacityCategoryCounts": json.dumps(
            [{"capacityCategoryId": -32768, "subCapacityCategoryId": None, "count": 1, "isAdult": None}]
        ),
        "numEquipment": 0, "seed": datetime.now(timezone.utc).isoformat(),
    }
    resp = session.get(f"{BASE_URL}/api/availability/map", params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def discover_loop_maps(session: requests.Session, root_map_id: int, max_depth: int = 6) -> list:
    """
    Recursively walks a park's map tree to find every TRUE LEAF map -- one
    with actual resourceAvailabilities and no further sub-links.

    This matters because parks vary in tree depth: some (like Bay City) are
    flat, root -> loops, with each loop already a leaf. Others (like
    Ludington) are root -> intermediate groupings -> loops, three levels
    deep, where the intermediate nodes have ZERO resources of their own and
    only exist to organize their children. Treating an intermediate node as
    if it were a leaf silently drops everything beneath it -- this exact
    bug caused Ludington to show 31 sites instead of its real 394.

    max_depth is a safety limit against unexpected cyclic/malformed data;
    real park trees are 1-3 levels deep in practice.
    """
    leaf_map_ids = []

    def _walk(map_id: int, depth: int):
        if depth > max_depth:
            print(f"    WARNING: max map-tree depth exceeded at mapId {map_id} -- stopping traversal here.")
            return
        data = query_map_for_discovery(session, map_id)
        link_ids = list(data.get("mapLinkAvailabilities", {}).keys())
        if not link_ids:
            # No further sub-links -- this is a true leaf (or the root map
            # itself carries resources directly, e.g. a single-loop park).
            leaf_map_ids.append(map_id)
        else:
            for link_id in link_ids:
                _walk(int(link_id), depth + 1)

    _walk(root_map_id, 0)
    return leaf_map_ids


def load_park_map_cache() -> dict:
    if PARK_MAP_CACHE_FILE.exists():
        try:
            return json.loads(PARK_MAP_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_park_map_cache(cache: dict) -> None:
    PARK_MAP_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def get_or_discover_park_maps(session, park_name, all_locations, cache):
    if park_name in cache:
        return cache[park_name]
    loc = lookup_park_location(park_name, all_locations)
    if not loc:
        print(f"  WARNING: '{park_name}' not found in MiDNR resourceLocation list -- skipping.")
        return None
    loop_ids = discover_loop_maps(session, loc["root_map_id"])
    cache[park_name] = {"resource_location_id": loc["resource_location_id"], "loop_map_ids": loop_ids}
    save_park_map_cache(cache)
    print(f"  Discovered {len(loop_ids)} loop(s) for '{park_name}'")
    return cache[park_name]


def fetch_loop_names(session: requests.Session, resource_location_id: int) -> dict:
    """
    One-time (cacheable, folded into fetch_resource_metadata below) lookup:
    resource_id -> the name of the LOOP that site belongs to (e.g. "Front
    Beechwood", "Cedar East Loop"), so subscriber texts can say something
    more useful than a bare site number. Confirmed live 2026-09-09 via
    GET /api/maps?resourceLocationId=X, which returns every loop-level map
    for the park, each with a human-readable title and the list of sites
    (mapResources) that belong to it -- a different, richer endpoint than
    the leaf-only map IDs discover_loop_maps() walks for availability.

    Loop titles are inconsistent about already including the word "Loop"
    (e.g. "Cedar East Loop" vs "Jackpine") -- used verbatim, never appended
    to, so we don't end up with "Cedar East Loop Loop".
    """
    url = f"{BASE_URL}/api/maps"
    params = {"resourceLocationId": resource_location_id}
    resp = session.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    raw = resp.json()

    loop_by_resource = {}
    for map_entry in raw:
        lv = next((l for l in map_entry.get("localizedValues", []) if l.get("cultureName") == "en-US"), {})
        title = lv.get("title")
        if not title:
            continue
        for res in map_entry.get("mapResources", []):
            resource_id = res.get("resourceId")
            if resource_id is not None:
                loop_by_resource[str(resource_id)] = title
    return loop_by_resource


def fetch_resource_metadata(session: requests.Session, resource_location_id: int) -> dict:
    url = f"{BASE_URL}/api/resourcelocation/resources"
    params = {"resourceLocationId": resource_location_id}
    resp = session.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    metadata = {}
    for resource_id, r in raw.items():
        name = None
        for lv in r.get("localizedValues", []):
            if lv.get("cultureName") == "en-US":
                name = lv.get("name")
                break
        metadata[resource_id] = {"name": name or resource_id}

    # Best-effort: merge in each site's loop name so subscriber texts can
    # say more than a bare site number. If this call fails for any reason,
    # fall back to no loop info rather than losing site metadata (and
    # therefore the whole park) over it.
    try:
        loop_by_resource = fetch_loop_names(session, resource_location_id)
        for resource_id, loop_name in loop_by_resource.items():
            if resource_id in metadata:
                metadata[resource_id]["loop"] = loop_name
    except requests.RequestException as e:
        print(f"    WARNING: couldn't fetch loop names for resourceLocationId {resource_location_id}: {e}")

    return metadata


def normalize_availability_code(code) -> int:
    """
    The API almost always returns a plain int status code per day, but for
    a site with an active hold in someone's shopping cart at that moment,
    it can instead return a dict like {"availability": 5, "remainingQuota": null}.
    This normalizes any shape down to a plain int so downstream code never
    has to special-case it.
    """
    if isinstance(code, dict):
        return code.get("availability", 0) or 0
    if isinstance(code, int):
        return code
    return 0


def fetch_park_availability(session: requests.Session, loop_map_ids: list) -> tuple:
    """
    Returns (combined, start_date, resource_map_ids). resource_map_ids is a
    {resource_id: map_id} dict recording which LEAF loop each resource came
    from -- needed by build_midnr_booking_url() to link a subscriber
    straight to the right loop's availability list rather than just the
    site's homepage. A resource_id is only ever returned by one leaf loop
    at a time, so there's no collision risk in overwriting entries here.
    """
    combined = {}
    resource_map_ids = {}
    start_date = _today_str()
    end_date = _plus_days_str(DAYS_AHEAD)
    for map_id in loop_map_ids:
        params = {
            "mapId": map_id, "bookingCategoryId": 0, "equipmentCategoryId": -32768,
            "subEquipmentCategoryId": -32764, "startDate": start_date, "endDate": end_date,
            "getDailyAvailability": "true", "isReserving": "false", "filterData": "[]",
            "boatLength": 0, "boatDraft": 0, "boatWidth": 0,
            "peopleCapacityCategoryCounts": json.dumps(
                [{"capacityCategoryId": -32768, "subCapacityCategoryId": None, "count": 1, "isAdult": None}]
            ),
            "numEquipment": 0, "seed": datetime.now(timezone.utc).isoformat(),
        }
        resp = session.get(f"{BASE_URL}/api/availability/map", params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for resource_id, day_codes in data.get("resourceAvailabilities", {}).items():
            combined[resource_id] = [normalize_availability_code(c) for c in day_codes]
            resource_map_ids[resource_id] = map_id
    return combined, start_date, resource_map_ids


# ---------------------------------------------------------------------------
# Per-subscriber matching
# ---------------------------------------------------------------------------

def find_available_runs(day_codes: list, start_date: str, min_nights: int) -> list:
    """
    Used for FLEXIBLE-date alerts only. Returns the start date of each
    contiguous available run of >= min_nights nights, reporting only the
    FIRST valid start per maximal run -- this deliberately avoids reporting
    every overlapping sub-window of one long opening as a separate "match",
    which would spam a flexible-date subscriber with duplicate texts for
    what is really a single underlying cancellation.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    run_starts = []
    run_len = 0
    for idx, code in enumerate(day_codes):
        if code in AVAILABLE_CODES:
            run_len += 1
        else:
            run_len = 0
        if run_len == min_nights:
            run_start_idx = idx - min_nights + 1
            run_starts.append((start_dt + timedelta(days=run_start_idx)).strftime("%Y-%m-%d"))
    return run_starts


def is_exact_date_newly_available(current_codes: list, previous_codes: list,
                                    start_date: str, target_date: str, min_nights: int) -> bool:
    """
    Used for EXACT-date alerts. Directly checks whether the specific
    requested arrival date now has >= min_nights consecutive available
    nights starting exactly there, and whether that's NEW since the last
    poll (at least one of those nights was unavailable before) -- so we
    don't re-alert on something that was already bookable.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    target_idx = (target_dt - start_dt).days

    if target_idx < 0 or target_idx + min_nights > len(current_codes):
        return False  # requested date is outside the window we polled

    currently_all_available = all(
        current_codes[target_idx + k] in AVAILABLE_CODES for k in range(min_nights)
    )
    if not currently_all_available:
        return False

    if not previous_codes or len(previous_codes) < target_idx + min_nights:
        return False  # no real baseline for this exact window -- treat as not-new

    was_already_all_available = all(
        previous_codes[target_idx + k] in AVAILABLE_CODES for k in range(min_nights)
    )
    return not was_already_all_available


def match_alerts_for_park(park_alerts: list, current_state: dict, previous_state: dict,
                           metadata: dict, start_date: str) -> list:
    """
    For each alert on this park, checks every resource for a genuinely NEW
    qualifying opening matching the alert's date requirements, min_nights,
    weekends_only, and specific_site. Returns a list of match dicts.
    """
    matches = []

    for resource_id, day_codes in current_state.items():
        prev_codes = previous_state.get(resource_id)
        if prev_codes is None:
            continue  # first-ever observation -- baseline only, no alerts

        site_name = metadata.get(resource_id, {}).get("name", resource_id)
        loop_name = metadata.get(resource_id, {}).get("loop")

        for alert in park_alerts:
            if alert.get("specific_site") and alert["specific_site"].strip() != site_name.strip():
                continue

            min_nights = alert.get("min_nights") or 1

            if alert.get("flexible_dates"):
                window_start = alert.get("arrival_window_start")
                window_end = alert.get("arrival_window_end")
                if not window_start or not window_end:
                    continue

                run_starts_now = set(find_available_runs(day_codes, start_date, min_nights))
                run_starts_before = set(find_available_runs(prev_codes, start_date, min_nights))
                newly_opened_runs = run_starts_now - run_starts_before

                for run_date in newly_opened_runs:
                    if not (window_start <= run_date <= window_end):
                        continue
                    if alert.get("weekends_only"):
                        weekday = datetime.strptime(run_date, "%Y-%m-%d").weekday()
                        if weekday not in (4, 5):  # Fri/Sat start
                            continue
                    matches.append({
                        "alert_id": alert["id"], "phone": alert["phone"], "park_name": alert["park_name"],
                        "resource_id": resource_id, "site_name": site_name, "date": run_date, "nights": min_nights,
                        "loop_name": loop_name,
                    })
            else:
                target_date = alert.get("arrival_date")
                if not target_date:
                    continue
                if alert.get("weekends_only"):
                    weekday = datetime.strptime(target_date, "%Y-%m-%d").weekday()
                    if weekday not in (4, 5):
                        continue
                if is_exact_date_newly_available(day_codes, prev_codes, start_date, target_date, min_nights):
                    matches.append({
                        "alert_id": alert["id"], "phone": alert["phone"], "park_name": alert["park_name"],
                        "resource_id": resource_id, "site_name": site_name, "date": target_date, "nights": min_nights,
                        "loop_name": loop_name,
                    })

    return matches


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_last_state() -> dict:
    """
    Loads the saved state, normalizing every day-code as it's read in.
    This matters even though fetch_park_availability already normalizes
    fresh data, because a state file saved by an OLDER version of this
    script (or from a run that crashed before this fix existed) could still
    have the un-normalized dict shape sitting on disk.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    normalized = {}
    for park_name, resources in raw.items():
        normalized[park_name] = {
            resource_id: [normalize_availability_code(c) for c in day_codes]
            for resource_id, day_codes in resources.items()
        }
    return normalized


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    session = requests.Session()

    print(f"DRY_RUN = {DRY_RUN}  (no real SMS will be sent)" if DRY_RUN else "DRY_RUN = False -- LIVE SMS SENDING ENABLED")
    print("Fetching full MiDNR park location list...")
    all_locations = fetch_all_resource_locations(session)
    print(f"Loaded {len(all_locations)} MiDNR locations.\n")

    park_map_cache = load_park_map_cache()
    rg_facility_cache = rg.load_facility_cache()
    previous_state = load_last_state()
    metadata_cache = {}
    # MiDNR only -- resource_location_id is needed (alongside each site's
    # map_id, merged into metadata_cache below) to build a direct MiDNR
    # booking link. Recreation.gov needs neither: its metadata already
    # carries a ready-to-use booking_url per site.
    park_resource_location_ids = {}

    print(f"Starting poll loop (every {POLL_INTERVAL_SECONDS}s). Ctrl+C to stop.\n")

    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            alerts = fetch_active_alerts(session)
        except requests.RequestException as e:
            print(f"[{timestamp}] ERROR fetching alerts from Supabase: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if not alerts:
            print(f"[{timestamp}] No active alerts. Sleeping.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        alerts_by_park = {}
        for a in alerts:
            alerts_by_park.setdefault(a["park_name"], []).append(a)

        print(f"[{timestamp}] Watching {len(alerts_by_park)} park(s), {len(alerts)} total alert(s)")

        current_state_all = {}

        for park_name, park_alerts in alerts_by_park.items():
            # Recreation.gov parks (Sleeping Bear Dunes, Pictured Rocks,
            # Hiawatha/Manistee/Ottawa National Forests) are checked FIRST
            # and dispatched to recreation_gov.py -- everything downstream
            # (matching, SMS sending, dedup, state saving) is completely
            # unchanged and backend-agnostic. See recreation_gov.py for the
            # scale/politeness caveat on the big multi-campground forests.
            if rg.is_recreation_gov_park(park_name):
                facilities = rg.get_or_discover_facilities(session, park_name, rg_facility_cache)
                if not facilities:
                    continue
                try:
                    current_state, rg_metadata, start_date = rg.fetch_park_availability(
                        session, facilities, DAYS_AHEAD
                    )
                except requests.RequestException as e:
                    print(f"  ERROR fetching Recreation.gov availability for '{park_name}': {e}")
                    continue
                # Recreation.gov bundles site/loop metadata into the same
                # response as availability, so (unlike MiDNR) this is cheap
                # to refresh every poll rather than caching it once.
                metadata_cache[park_name] = rg_metadata
            else:
                park_info = get_or_discover_park_maps(session, park_name, all_locations, park_map_cache)
                if not park_info:
                    continue
                park_resource_location_ids[park_name] = park_info["resource_location_id"]

                if park_name not in metadata_cache:
                    metadata_cache[park_name] = fetch_resource_metadata(session, park_info["resource_location_id"])

                try:
                    current_state, start_date, resource_map_ids = fetch_park_availability(
                        session, park_info["loop_map_ids"]
                    )
                except requests.RequestException as e:
                    print(f"  ERROR fetching availability for '{park_name}': {e}")
                    continue

                # resource_map_ids is recomputed fresh every poll (it's cheap
                # -- it falls out of the availability call we already made),
                # so merge it into metadata_cache every time even though the
                # name/existence part of metadata_cache is only fetched once.
                for resource_id, map_id in resource_map_ids.items():
                    metadata_cache[park_name].setdefault(resource_id, {})["map_id"] = map_id

            current_state_all[park_name] = current_state
            prev_state_for_park = previous_state.get(park_name, {})

            matches = match_alerts_for_park(
                park_alerts, current_state, prev_state_for_park, metadata_cache[park_name], start_date
            )

            if not matches:
                print(f"  {park_name}: no matching openings ({len(park_alerts)} alert(s), {len(current_state)} sites)")
                continue

            for m in matches:
                # NOTE: we deliberately do NOT check sms_log here to block
                # sending. match_alerts_for_park() already only returns
                # genuinely NEW transitions (compared to the immediately
                # preceding poll, persisted across restarts via STATE_FILE),
                # so a site that stays continuously available won't re-match
                # on its own. A permanent "already texted this combo" block
                # would incorrectly silence a real FUTURE re-opening of the
                # same site+date if it gets booked and later cancels again.
                # sms_log is still written below, purely as an audit trail.
                if rg.is_recreation_gov_park(park_name):
                    booking_url = metadata_cache[park_name].get(m["resource_id"], {}).get(
                        "booking_url", "https://www.recreation.gov"
                    )
                else:
                    booking_url = build_midnr_booking_url(
                        park_resource_location_ids.get(park_name),
                        metadata_cache[park_name].get(m["resource_id"], {}).get("map_id"),
                        m["date"], m["nights"],
                    )
                message = build_message(
                    m["park_name"], m["site_name"], m["date"], m["nights"], booking_url,
                    loop_name=m.get("loop_name"),
                )
                success, twilio_sid = send_sms(session, m["phone"], message)
                if success:
                    log_sms_sent(
                        session, m["alert_id"], m["resource_id"], m["site_name"],
                        m["date"], m["park_name"], m["phone"], message, twilio_sid, DRY_RUN
                    )
                    print(f"  \U0001F3D5️  MATCH: {m['park_name']} — Site {m['site_name']} on {m['date']} "
                          f"({m['nights']} night(s)) -> texted {m['phone']}")

        save_state(current_state_all)
        previous_state = current_state_all

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
