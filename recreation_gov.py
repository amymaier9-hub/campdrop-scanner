"""
Recreation.gov support for CampDrop -- parallel module to the MiDNR/Aspira
code already in scanner_v3.py.

Captured live on 2026-09-02 by watching the real network traffic
recreation.gov's own site makes (same reverse-engineering approach used for
MiDNRReservations.com). No API key or login is required -- this is a public,
unauthenticated JSON endpoint recreation.gov's own frontend calls, mirroring
how MiDNR needed none either. RIDB (ridb.recreation.gov) was investigated and
is NOT used here: this endpoint already gives us everything needed (facility
search AND live day-by-day availability) in one consistent API family.

DESIGN GOAL: normalize Recreation.gov's data into the exact same shape
scanner_v3.py's MiDNR code already produces -- a flat {resource_id: [day
codes]} dict where index 0 is "today" -- so scanner_v3.py's matching engine
(find_available_runs, is_exact_date_newly_available, match_alerts_for_park)
needs ZERO changes to work with either backend. See scanner_v3.py's main()
for the (small) dispatch branch that calls into this module.

KEY API DIFFERENCE FROM MiDNR: MiDNR's Aspira endpoint takes an arbitrary
date range in one call. Recreation.gov's endpoint accepts exactly ONE query
parameter and returns exactly one calendar month per call (confirmed live:
adding an end_date param fails with "Only one query parameter is allowed for
this request"). So covering a multi-month lookahead window here means
multiple sequential per-month calls per facility, stitched together -- see
_month_starts_covering() / fetch_facility_availability().

SCALE NOTE: some of these parks are single campgrounds (Sleeping Bear Dunes'
D.H. Day = 1 facility), but others are entire national forests with dozens
of dispersed campgrounds under one recarea (Hiawatha NF = 43, Huron-Manistee
NF = 25, Ottawa NF = 15). An alert against one of the big forests means
polling every campground under it every cycle -- e.g. Hiawatha at
DAYS_AHEAD=120 is roughly 43 facilities x ~5 months/facility = ~215 HTTP
calls per poll. That's a lot more than any single MiDNR park needs (MiDNR
parks top out around a dozen loops). REQUEST_DELAY_SECONDS below adds a
small delay between calls to be a polite API citizen, but if a real
subscriber sets an alert on one of the big forests, seriously consider a
longer poll interval for Recreation.gov parks specifically rather than
reusing MiDNR's 60s cadence -- this was flagged to the user as an open
question, not resolved in code.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_URL = "https://www.recreation.gov"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

# Exact park_name strings used by the "SELECT YOUR PARK" dropdown on
# mittencamper.com/alerts/ (confirmed live 2026-09-02, each marked with a
# star in that dropdown) mapped to Recreation.gov's internal "recarea"
# entity IDs (also confirmed live, via /api/search/suggest).
#
# NOTE on "Manistee National Forest": Recreation.gov has no separate
# Manistee entity. It's merged with Huron National Forest into ONE combined
# "Huron-Manistee National Forests" recarea (1082, 25 campgrounds) spanning
# both the Manistee side (Baldwin/Manistee/Wellston/Irons, west Michigan)
# and the Huron side (Mio/Oscoda/Glennie, ~100+ miles east). Per product
# decision (2026-09-02), an alert for "Manistee National Forest" scans the
# WHOLE combined unit, Huron side included, rather than a curated
# west-only subset -- simplest option, matches the official administrative
# boundary. A subscriber could in principle get matched at a Huron-side
# campground; revisit with a curated facility whitelist if that becomes a
# real complaint.
RECAREA_IDS = {
    "Sleeping Bear Dunes National Lakeshore": 2937,
    "Pictured Rocks National Lakeshore": 2895,
    "Hiawatha National Forest": 1081,
    "Manistee National Forest": 1082,  # = Huron-Manistee NF, see note above
    "Ottawa National Forest": 1083,
}

# Every per-day status string observed live across both Site-Specific
# campgrounds (e.g. D.H. Day: Available/Reserved/Not Reservable/Closed) and
# Non Site-Specific / quota-based campgrounds (e.g. a backcountry permit
# area: adds Open/NYR, and Not Available for an out-of-season month).
# Only these two count as bookable; everything else -- including any status
# string not yet seen live -- normalizes to "unavailable" defensively, so a
# new/unrecognized status can't accidentally get treated as an opening.
AVAILABLE_STATUSES = {"Available", "Open"}

# Facilities that Recreation.gov lists under a recarea's campground search
# but that 404 on the standard month-availability endpoint -- confirmed live
# in production on 2026-09-02 (4 straight 404s polling Sleeping Bear Dunes).
# 259245 = "Village Campground - North Manitou Island": a boat-in wilderness
# campground (no vehicle access, ferry-only) that lacked a "reserve_type" in
# its search listing during initial research, unlike every bookable
# campground here -- a sign it's a permit-based backcountry area rather than
# a standard site-reservation campground, so it doesn't work through this
# API path. Per product decision (2026-09-02), it should never be offered as
# an alertable option at all rather than silently retried and skipped every
# poll -- so it's filtered out at discovery time, before it's ever cached or
# scanned.
EXCLUDED_FACILITY_IDS = {"259245"}

FACILITY_CACHE_FILE = Path(__file__).parent / "recreation_gov_facility_cache.json"

REQUEST_DELAY_SECONDS = 0.35  # be a polite API citizen -- see SCALE NOTE above


def is_recreation_gov_park(park_name: str) -> bool:
    return park_name in RECAREA_IDS


def normalize_availability_status(status) -> int:
    """
    Maps a Recreation.gov per-day status STRING to the same int scale
    scanner_v3.py already uses for MiDNR (1 = available/bookable). Anything
    else -- Reserved, Not Reservable, Closed, Not Available, NYR, None, a
    non-string, or any future status we haven't seen yet -- normalizes to 2
    (unavailable), matching scanner_v3.AVAILABLE_CODES = {1, 5} (this module
    only ever emits 1, never 5 -- that code is MiDNR-specific).
    """
    if isinstance(status, str) and status in AVAILABLE_STATUSES:
        return 1
    return 2


def _month_starts_covering(start_date: datetime, days_ahead: int) -> list:
    """
    Returns the first-of-month datetimes needed to cover the half-open
    window [start_date, start_date + days_ahead) -- e.g. start_date =
    2026-09-15, days_ahead = 60 needs September, October, AND November
    (the window runs through 2026-11-13). Handles a December -> January
    year rollover correctly (see test_recreation_gov.py).
    """
    end_date = start_date + timedelta(days=days_ahead - 1)
    months = []
    cursor = start_date.replace(day=1)
    while cursor <= end_date:
        months.append(cursor)
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return months


def fetch_facility_month(session: requests.Session, facility_id, month_start: datetime) -> dict:
    """
    One HTTP call: every campsite's full-month day-by-day status for one
    facility (campground). Returns the raw {campsite_id: {...}} dict from
    the "campsites" key of the response.
    """
    start_param = month_start.strftime("%Y-%m-01T00:00:00.000Z")
    url = f"{BASE_URL}/api/camps/availability/campground/{facility_id}/month"
    resp = session.get(url, params={"start_date": start_param}, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Recreation.gov API error for facility {facility_id}: {data['error']}")
    return data.get("campsites", {}) if isinstance(data, dict) else {}


def fetch_facility_availability(session: requests.Session, facility_id, facility_name: str,
                                 start_date: datetime, days_ahead: int) -> tuple:
    """
    Fetches and stitches together every month needed to cover
    [start_date, start_date + days_ahead) for ONE facility. Returns
    (day_codes_by_campsite, metadata_by_campsite):

    - day_codes_by_campsite[campsite_id] is a list of `days_ahead` ints,
      aligned so index 0 == start_date -- the exact same indexing
      convention as MiDNR's fetch_park_availability, so downstream matching
      code (find_available_runs, is_exact_date_newly_available) works
      unmodified.
    - metadata_by_campsite[campsite_id] = {"name": "<site>, <facility>
      (<loop> Loop)"} -- Recreation.gov bundles site/loop into the SAME
      response as availability, so (unlike MiDNR) no separate metadata
      endpoint call is needed.

    A campsite missing from a given month's response (not observed live,
    but defended against) simply keeps its default "unavailable" code for
    those days rather than crashing or silently fabricating an opening. A
    month call that errors is logged and skipped -- one bad month doesn't
    lose the whole facility's data for this poll.
    """
    combined = {}
    metadata = {}

    month_starts = _month_starts_covering(start_date, days_ahead)
    for i, month_start in enumerate(month_starts):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            campsites = fetch_facility_month(session, facility_id, month_start)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            print(f"    WARNING: couldn't fetch '{facility_name}' for {month_start:%Y-%m}: {e}")
            continue

        for campsite_id, site in campsites.items():
            if not isinstance(site, dict):
                continue
            if campsite_id not in combined:
                combined[campsite_id] = [2] * days_ahead
                loop = (site.get("loop") or "").strip()
                site_label = (site.get("site") or campsite_id) or campsite_id
                name = f"{site_label}, {facility_name}"
                if loop:
                    name += f" ({loop} Loop)"
                # Unlike MiDNR/Aspira, Recreation.gov gives every campsite a
                # stable, public, no-login-required detail/booking page at
                # this exact URL shape -- confirmed live 2026-09-02 (landed
                # directly on "Site 01, D.H. Day Campground" with dates ready
                # to pick). No cart/session state needed, so it's safe to
                # text straight to a subscriber.
                metadata[campsite_id] = {
                    "name": name,
                    "booking_url": f"{BASE_URL}/camping/campsites/{campsite_id}",
                }

            for date_str, status in (site.get("availabilities") or {}).items():
                try:
                    day = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
                except ValueError:
                    continue
                idx = (day - start_date).days
                if 0 <= idx < days_ahead:
                    combined[campsite_id][idx] = normalize_availability_status(status)

    return combined, metadata


def discover_facilities(session: requests.Session, recarea_id) -> list:
    """
    Every campground facility under a park (recarea) -- the Recreation.gov
    analog of MiDNR's discover_loop_maps, except this is a flat search
    instead of a map tree to recurse through.
    """
    url = f"{BASE_URL}/api/search"
    params = {"fq": ["entity_type:campground", f"parent_asset_id:{recarea_id}"], "size": 200}
    resp = session.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", []) if isinstance(data, dict) else []
    return [
        {"facility_id": r["entity_id"], "name": r.get("name") or str(r["entity_id"])}
        for r in results
        if r.get("entity_id") and str(r["entity_id"]) not in EXCLUDED_FACILITY_IDS
    ]


def load_facility_cache() -> dict:
    if FACILITY_CACHE_FILE.exists():
        try:
            return json.loads(FACILITY_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_facility_cache(cache: dict) -> None:
    FACILITY_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def get_or_discover_facilities(session: requests.Session, park_name: str, cache: dict):
    """Mirrors scanner_v3.get_or_discover_park_maps's caching behavior."""
    if park_name in cache:
        return cache[park_name]
    recarea_id = RECAREA_IDS.get(park_name)
    if not recarea_id:
        print(f"  WARNING: '{park_name}' is not a known Recreation.gov park -- skipping.")
        return None
    facilities = discover_facilities(session, recarea_id)
    cache[park_name] = facilities
    save_facility_cache(cache)
    print(f"  Discovered {len(facilities)} Recreation.gov campground(s) for '{park_name}'")
    return facilities


def fetch_park_availability(session: requests.Session, facilities: list, days_ahead: int) -> tuple:
    """
    Merges every facility (campground) under this park into ONE flat
    {campsite_id: day_codes} dict plus metadata -- the Recreation.gov
    analog of MiDNR's fetch_park_availability(session, loop_map_ids), which
    merges multiple loops the same way. Returns
    (combined_state, metadata, start_date_str). Unlike the MiDNR version,
    this also returns metadata (see fetch_facility_availability's
    docstring for why) -- scanner_v3.py's dispatch branch uses it to
    populate metadata_cache[park_name] directly instead of calling a
    separate metadata function.
    """
    start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    combined = {}
    metadata = {}

    for i, facility in enumerate(facilities):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        day_codes, meta = fetch_facility_availability(
            session, facility["facility_id"], facility["name"], start_date, days_ahead
        )
        combined.update(day_codes)
        metadata.update(meta)

    return combined, metadata, start_date.strftime("%Y-%m-%d")
