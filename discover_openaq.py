"""
Check what OpenAQ coverage exists near the Klang Valley EQMS stations.

Run this BEFORE writing an extract script. It answers three questions that
decide whether OpenAQ can be the project's ground truth:

  1. Are there monitoring locations near each station?
  2. Do they measure PM2.5, and are they reference monitors or low-cost sensors?
  3. How far back does the data go, and is it still updating?

Nothing is ingested here. This is reconnaissance.

Usage:
    export OPENAQ_API_KEY=...          # or set it in Colab secrets
    python3 discover_openaq.py

Docs: https://docs.openaq.org
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.openaq.org/v3"
SEARCH_RADIUS_M = 25_000        # API maximum
CONFIG_PATH = Path("stations.yml")
OUT_PATH = Path("raw/openaq_discovery.json")


def load_stations(path: Path) -> list[dict]:
    """Minimal parser for the flat stations.yml used by the other scripts."""
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
        station["lat"] = float(station["lat"])
        station["lon"] = float(station["lon"])
    return stations


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance between two points, in km."""
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return radius * 2 * math.asin(math.sqrt(a))


def api_get(path: str, params: dict, api_key: str) -> dict:
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "X-API-Key": api_key,
            "User-Agent": "haze-watch-my/0.1 (portfolio project)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:300]
        if error.code == 401:
            sys.exit("401 Unauthorised - check OPENAQ_API_KEY is set correctly.")
        if error.code == 429:
            print("    rate limited, pausing 60s", file=sys.stderr)
            time.sleep(60)
            return api_get(path, params, api_key)
        sys.exit(f"HTTP {error.code} on {path}: {body}")


def describe_location(location: dict, origin: dict) -> dict:
    coords = location.get("coordinates") or {}
    lat, lon = coords.get("latitude"), coords.get("longitude")
    distance = (haversine_km(origin["lat"], origin["lon"], lat, lon)
                if lat is not None else None)

    sensors = []
    for sensor in location.get("sensors") or []:
        parameter = sensor.get("parameter") or {}
        sensors.append({
            "sensor_id": sensor.get("id"),
            "parameter": parameter.get("name"),
            "units": parameter.get("units"),
        })

    instruments = [i.get("name") for i in (location.get("instruments") or [])]

    return {
        "location_id": location.get("id"),
        "name": location.get("name"),
        "provider": (location.get("provider") or {}).get("name"),
        "owner": (location.get("owner") or {}).get("name"),
        "is_monitor": location.get("isMonitor"),
        "instruments": instruments,
        "distance_km": round(distance, 1) if distance is not None else None,
        "latitude": lat,
        "longitude": lon,
        "first_data": (location.get("datetimeFirst") or {}).get("utc"),
        "last_data": (location.get("datetimeLast") or {}).get("utc"),
        "sensors": sensors,
    }


def main() -> int:
    api_key = os.environ.get("OPENAQ_API_KEY")
    if not api_key:
        sys.exit("OPENAQ_API_KEY is not set.")

    stations = load_stations(CONFIG_PATH)
    findings = {}

    for station in stations:
        name = station["name"]
        print(f"\n{name}  ({station['lat']}, {station['lon']})")
        print("-" * 70)

        payload = api_get(
            "locations",
            {
                "coordinates": f"{station['lat']:.4f},{station['lon']:.4f}",
                "radius": SEARCH_RADIUS_M,
                "limit": 100,
            },
            api_key,
        )

        results = payload.get("results", [])
        if not results:
            print("  NO COVERAGE within 25km")
            findings[name] = []
            continue

        described = sorted(
            (describe_location(r, station) for r in results),
            key=lambda d: (d["distance_km"] is None, d["distance_km"]),
        )
        findings[name] = described

        for item in described:
            pollutants = sorted({s["parameter"] for s in item["sensors"] if s["parameter"]})
            has_pm25 = "pm25" in pollutants
            marker = "PM2.5" if has_pm25 else "no pm25"
            print(f"  [{item['location_id']}] {item['name']}")
            print(f"      {item['distance_km']} km away | {marker} | "
                  f"monitor={item['is_monitor']}")
            print(f"      provider: {item['provider']} | owner: {item['owner']}")
            print(f"      instruments: {', '.join(item['instruments']) or 'unknown'}")
            print(f"      data: {item['first_data']}  ->  {item['last_data']}")
            if has_pm25:
                pm25_ids = [s["sensor_id"] for s in item["sensors"]
                            if s["parameter"] == "pm25"]
                print(f"      pm25 sensor id(s): {pm25_ids}")
        time.sleep(1)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(findings, indent=2))
    print(f"\nFull detail saved to {OUT_PATH}")

    # Summary: the thing that decides the next step.
    print("\n" + "=" * 70)
    print("SUMMARY")
    for name, items in findings.items():
        with_pm25 = [i for i in items
                     if any(s["parameter"] == "pm25" for s in i["sensors"])]
        if not with_pm25:
            print(f"  {name:<15} no PM2.5 coverage")
            continue
        nearest = with_pm25[0]
        print(f"  {name:<15} {len(with_pm25)} PM2.5 site(s), "
              f"nearest {nearest['distance_km']}km "
              f"({nearest['provider']}), last data {nearest['last_data']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
