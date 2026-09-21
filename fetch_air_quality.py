"""
Extract hourly air quality from Open-Meteo and land it as raw JSON.

This is the EXTRACT step only. It does not clean, reshape or interpret
anything. Raw responses are written unmodified so that a parsing mistake
later can always be corrected by re-reading the file instead of re-fetching
data that may no longer be available.

Usage:
    python fetch_air_quality.py                  # today + 5 day forecast
    python fetch_air_quality.py --past-days 30   # backfill 30 days of history

Output:
    raw/air_quality/YYYY-MM-DD/<station-slug>.json

Data source: Open-Meteo Air Quality API (CAMS). Free for non-commercial use;
attribution to Open-Meteo and CAMS is required wherever the data is shown.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Requested per station. pm2_5 is the one that matters; the rest give
# cross-checks and let us compare against what other apps display.
HOURLY_VARIABLES = [
    "pm2_5",
    "pm10",
    "us_aqi",
    "us_aqi_pm2_5",
    "carbon_monoxide",
    "nitrogen_dioxide",
    "sulphur_dioxide",
    "ozone",
    "aerosol_optical_depth",
]

RAW_DIR = Path("raw/air_quality")
CONFIG_PATH = Path("stations.yml")

REQUEST_TIMEOUT = 30
RETRY_DELAYS = [2, 5, 15]   # exponential-ish backoff between attempts
POLITE_PAUSE = 1            # seconds between stations


def slugify(name: str) -> str:
    return name.lower().replace(" ", "-")


def load_stations(path: Path) -> list[dict]:
    """Read stations.yml.

    Parsed by hand so the script has no third-party dependencies. If the
    config grows beyond this flat shape, install PyYAML and swap this out.
    """
    if not path.exists():
        sys.exit(f"Config not found: {path.resolve()}")

    stations, current = [], None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "stations:":
            continue
        if line.startswith("- "):
            if current:
                stations.append(current)
            current = {}
            line = line[2:].strip()
        if ":" in line and current is not None:
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    if current:
        stations.append(current)

    for station in stations:
        for field in ("name", "lat", "lon"):
            if field not in station:
                sys.exit(f"Station missing '{field}': {station}")
        station["lat"] = float(station["lat"])
        station["lon"] = float(station["lon"])

    if not stations:
        sys.exit(f"No stations found in {path}")
    return stations


def build_url(station: dict, past_days: int, forecast_days: int) -> str:
    params = {
        "latitude": station["lat"],
        "longitude": station["lon"],
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": "Asia/Kuala_Lumpur",
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def fetch(url: str) -> dict:
    """GET with retries. Raises on final failure so the caller decides."""
    last_error = None
    for attempt, delay in enumerate([0] + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "haze-watch-my/0.1 (portfolio project)"},
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            print(f"    attempt {attempt + 1} failed: {error}", file=sys.stderr)
    raise RuntimeError(f"all attempts failed: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--past-days", type=int, default=0,
                        help="days of history to include (max 92)")
    parser.add_argument("--forecast-days", type=int, default=5,
                        help="days of forecast to include (max 7)")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()

    stations = load_stations(args.config)
    run_date = datetime.now().strftime("%Y-%m-%d")
    out_dir = RAW_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)

    succeeded, failed = 0, 0
    for station in stations:
        name = station["name"]
        print(f"  {name} ...")
        url = build_url(station, args.past_days, args.forecast_days)
        try:
            payload = fetch(url)
        except RuntimeError as error:
            print(f"    SKIPPED: {error}", file=sys.stderr)
            failed += 1
            continue

        # Wrap the response rather than altering it: the payload stays exactly
        # as received, with our own context recorded alongside it.
        record = {
            "_meta": {
                "station_name": name,
                "station_site": station.get("station_site"),
                "requested_lat": station["lat"],
                "requested_lon": station["lon"],
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                "source": "open-meteo air-quality",
                "request_url": url,
            },
            "response": payload,
        }

        out_path = out_dir / f"{slugify(name)}.json"
        out_path.write_text(json.dumps(record, indent=2))

        hours = len(payload.get("hourly", {}).get("time", []))
        grid_lat = payload.get("latitude")
        grid_lon = payload.get("longitude")
        print(f"    {hours} hours -> {out_path}")
        print(f"    grid cell used: {grid_lat}, {grid_lon}")
        succeeded += 1
        time.sleep(POLITE_PAUSE)

    print(f"\nDone: {succeeded} succeeded, {failed} failed")
    return 1 if failed and not succeeded else 0


if __name__ == "__main__":
    sys.exit(main())
