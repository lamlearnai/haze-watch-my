"""
Extract hourly PM2.5 from the live OpenAQ sites found by discover_openaq.py.

Reads raw/openaq_discovery.json, selects every unique site that is still
reporting, and pulls hourly averages for each of its PM2.5 sensors.

Sites are treated as ONE NETWORK, not as per-station lists: the same sensor
often sits near more than one DOE station, so deduplication happens on
location_id.

Usage:
    export OPENAQ_API_KEY=...
    python3 fetch_openaq.py                    # last 30 days
    python3 fetch_openaq.py --days 90          # more history

Output:
    raw/openaq/YYYY-MM-DD/<location_id>-<sensor_id>.json

Endpoint: /v3/sensors/{id}/hours  (hourly averages)
Docs: https://docs.openaq.org/resources/measurements
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://api.openaq.org/v3"
DISCOVERY_PATH = Path("raw/openaq_discovery.json")
RAW_DIR = Path("raw/openaq")

PAGE_LIMIT = 1000          # API maximum per page
MAX_PAGES = 10             # safety stop
POLITE_PAUSE = 1           # seconds between requests
LIVE_SINCE = "2026-09-15"  # a site must have reported on or after this date


def api_get(path: str, params: dict, api_key: str) -> dict | None:
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "X-API-Key": api_key,
            "User-Agent": "haze-watch-my/0.1 (portfolio project)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:300]
        if error.code == 401:
            sys.exit("401 Unauthorised - check OPENAQ_API_KEY.")
        if error.code == 429:
            print("      rate limited, pausing 60s", file=sys.stderr)
            time.sleep(60)
            return api_get(path, params, api_key)
        if error.code == 408:
            print("      timeout - try a shorter --days window", file=sys.stderr)
            return None
        print(f"      HTTP {error.code}: {body}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"      network error: {error}", file=sys.stderr)
        return None


def collect_live_sites(discovery: dict) -> dict[int, dict]:
    """Flatten the per-station discovery output into one deduplicated network.

    Records which DOE stations each site is near, and how far - that mapping
    is what later allows calibration against official readings.
    """
    network: dict[int, dict] = {}

    for doe_station, sites in discovery.items():
        for site in sites:
            last = site.get("last_data") or ""
            pm25_sensors = [s for s in site.get("sensors", [])
                            if s.get("parameter") == "pm25"]
            if not pm25_sensors or last < LIVE_SINCE:
                continue

            location_id = site["location_id"]
            entry = network.setdefault(location_id, {
                "location_id": location_id,
                "name": site["name"],
                "provider": site["provider"],
                "latitude": site["latitude"],
                "longitude": site["longitude"],
                "first_data": site["first_data"],
                "last_data": last,
                "pm25_sensor_ids": [s["sensor_id"] for s in pm25_sensors],
                "near_doe_stations": [],
            })
            entry["near_doe_stations"].append({
                "station": doe_station,
                "distance_km": site["distance_km"],
            })

    # Nearest DOE station first - that is the one to calibrate against.
    for entry in network.values():
        entry["near_doe_stations"].sort(key=lambda d: d["distance_km"])
    return network


def fetch_hours(sensor_id: int, date_from: str, date_to: str,
                api_key: str) -> list[dict]:
    """Page through hourly averages for one sensor."""
    rows, page = [], 1
    while page <= MAX_PAGES:
        payload = api_get(
            f"sensors/{sensor_id}/hours",
            {
                "datetime_from": date_from,
                "datetime_to": date_to,
                "limit": PAGE_LIMIT,
                "page": page,
            },
            api_key,
        )
        if not payload:
            break
        results = payload.get("results", [])
        rows.extend(results)
        if len(results) < PAGE_LIMIT:
            break
        page += 1
        time.sleep(POLITE_PAUSE)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="days of history to pull")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAQ_API_KEY")
    if not api_key:
        sys.exit("OPENAQ_API_KEY is not set.")
    if not DISCOVERY_PATH.exists():
        sys.exit(f"Run discover_openaq.py first - {DISCOVERY_PATH} not found.")

    discovery = json.loads(DISCOVERY_PATH.read_text())
    network = collect_live_sites(discovery)
    if not network:
        sys.exit("No live PM2.5 sites found in the discovery file.")

    date_to = datetime.now(timezone.utc).date() + timedelta(days=1)  # inclusive of today
    date_from = date_to - timedelta(days=args.days)
    run_date = datetime.now().strftime("%Y-%m-%d")
    out_dir = RAW_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{len(network)} unique live sites, "
          f"{date_from} to {date_to}\n")

    total_rows, failed = 0, 0
    for entry in sorted(network.values(), key=lambda e: e["name"]):
        nearest = entry["near_doe_stations"][0]
        print(f"  {entry['name']} [{entry['location_id']}] "
              f"- nearest DOE: {nearest['station']} ({nearest['distance_km']}km)")

        for sensor_id in entry["pm25_sensor_ids"]:
            rows = fetch_hours(sensor_id, str(date_from), str(date_to), api_key)
            if not rows:
                print(f"      sensor {sensor_id}: no data")
                failed += 1
                continue

            record = {
                "_meta": {
                    **{k: v for k, v in entry.items() if k != "pm25_sensor_ids"},
                    "sensor_id": sensor_id,
                    "date_from": str(date_from),
                    "date_to": str(date_to),
                    "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                    "source": "openaq v3 sensors/hours",
                },
                "results": rows,
            }
            out_path = out_dir / f"{entry['location_id']}-{sensor_id}.json"
            out_path.write_text(json.dumps(record, indent=2))
            print(f"      sensor {sensor_id}: {len(rows)} hours -> {out_path.name}")
            total_rows += len(rows)
            time.sleep(POLITE_PAUSE)

    print(f"\nDone: {total_rows} hourly rows, {failed} sensor(s) with no data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
