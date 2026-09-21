"""
Load raw OpenAQ JSON files into Postgres.

Idempotent: re-running for the same hours updates rather than duplicates,
so a failed or repeated run is always safe.

Usage:
    pip install psycopg2-binary
    export POSTGRES_PASSWORD=...
    python3 load_openaq.py                       # load every raw file
    python3 load_openaq.py --date 2026-09-18     # one day's folder only

Reads:  raw/openaq/YYYY-MM-DD/<location>-<sensor>.json
        raw/openaq_discovery.json   (site metadata)
        stations.yml                (DOE station reference)
Writes: doe_stations, openaq_sites, raw_openaq_hourly, pipeline_runs
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")

DB_CONFIG = {
    "host": os.environ.get("POSTGRES_HOST", "localhost"),
    "port": int(os.environ.get("POSTGRES_PORT", 5433)),
    "dbname": os.environ.get("POSTGRES_DB", "haze"),
    "user": os.environ.get("POSTGRES_USER", "haze"),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
}

RAW_DIR = Path("raw/openaq")
DISCOVERY_PATH = Path("raw/openaq_discovery.json")
STATIONS_PATH = Path("stations.yml")


def load_stations_yml(path: Path) -> list[dict]:
    """Minimal parser for the flat stations.yml shape."""
    if not path.exists():
        return []
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
    return stations


def parse_openaq_file(path: Path) -> tuple[dict, list[tuple]]:
    """Turn one raw OpenAQ file into (site metadata, measurement rows).

    Pure function - no database, no side effects - so it can be tested against
    a sample file without a running Postgres.
    """
    payload = json.loads(path.read_text())
    meta = payload["_meta"]
    nearest = (meta.get("near_doe_stations") or [{}])[0]

    site = {
        "location_id": meta["location_id"],
        "site_name": meta["name"],
        "provider": meta.get("provider"),
        "latitude": meta.get("latitude"),
        "longitude": meta.get("longitude"),
        "nearest_doe_station": nearest.get("station"),
        "distance_km": nearest.get("distance_km"),
        "first_seen": meta.get("first_data"),
        "last_seen": meta.get("last_data"),
    }

    rows = []
    for result in payload.get("results", []):
        period = result.get("period") or {}
        starts = period.get("datetimeFrom") or {}
        measured_at = starts.get("utc") or starts.get("local")
        if not measured_at:
            continue
        parameter = (result.get("parameter") or {})
        rows.append((
            meta["location_id"],
            meta["sensor_id"],
            measured_at,
            parameter.get("name", "pm25"),
            result.get("value"),
            parameter.get("units"),
            path.name,
        ))
    return site, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="load only this dated folder (YYYY-MM-DD)")
    args = parser.parse_args()

    if not DB_CONFIG["password"]:
        sys.exit("POSTGRES_PASSWORD is not set.")

    pattern = f"{RAW_DIR}/{args.date or '*'}/*.json"
    files = sorted(Path(p) for p in glob.glob(pattern))
    if not files:
        sys.exit(f"No files matched {pattern}")

    started = datetime.now(timezone.utc)
    print(f"{len(files)} file(s) to load\n")

    connection = psycopg2.connect(**DB_CONFIG)
    connection.autocommit = False
    cursor = connection.cursor()

    try:
        # DOE reference stations - static, but upserted so the file stays
        # the single source of truth.
        stations = load_stations_yml(STATIONS_PATH)
        if stations:
            execute_values(cursor, """
                INSERT INTO doe_stations
                    (station_name, station_site, state, latitude, longitude)
                VALUES %s
                ON CONFLICT (station_name) DO UPDATE SET
                    station_site = EXCLUDED.station_site,
                    state        = EXCLUDED.state,
                    latitude     = EXCLUDED.latitude,
                    longitude    = EXCLUDED.longitude
            """, [(s["name"], s.get("station_site"), s.get("state"),
                   float(s["lat"]), float(s["lon"])) for s in stations])
            print(f"  doe_stations: {len(stations)} upserted")

        sites, measurements = {}, []
        for path in files:
            site, rows = parse_openaq_file(path)
            sites[site["location_id"]] = site
            measurements.extend(rows)
            print(f"  {path.name}: {len(rows)} rows")

        if sites:
            execute_values(cursor, """
                INSERT INTO openaq_sites
                    (location_id, site_name, provider, latitude, longitude,
                     nearest_doe_station, distance_km, first_seen, last_seen)
                VALUES %s
                ON CONFLICT (location_id) DO UPDATE SET
                    site_name           = EXCLUDED.site_name,
                    provider            = EXCLUDED.provider,
                    nearest_doe_station = EXCLUDED.nearest_doe_station,
                    distance_km         = EXCLUDED.distance_km,
                    last_seen           = EXCLUDED.last_seen
            """, [(s["location_id"], s["site_name"], s["provider"],
                   s["latitude"], s["longitude"], s["nearest_doe_station"],
                   s["distance_km"], s["first_seen"], s["last_seen"])
                  for s in sites.values()])
            print(f"\n  openaq_sites: {len(sites)} upserted")

        if measurements:
            execute_values(cursor, """
                INSERT INTO raw_openaq_hourly
                    (location_id, sensor_id, measured_at, parameter,
                     value, units, source_file)
                VALUES %s
                ON CONFLICT (sensor_id, measured_at, parameter) DO UPDATE SET
                    value       = EXCLUDED.value,
                    source_file = EXCLUDED.source_file,
                    ingested_at = now()
            """, measurements, page_size=1000)
            print(f"  raw_openaq_hourly: {len(measurements)} rows upserted")

        cursor.execute("""
            INSERT INTO pipeline_runs
                (task_name, started_at, finished_at, status, rows_written)
            VALUES (%s, %s, now(), %s, %s)
        """, ("load_openaq", started, "success", len(measurements)))

        connection.commit()
        print("\nCommitted.")

        cursor.execute("""
            SELECT count(*), min(measured_at), max(measured_at)
            FROM raw_openaq_hourly
        """)
        total, earliest, latest = cursor.fetchone()
        print(f"Table now holds {total} rows, {earliest} -> {latest}")

    except Exception as error:
        connection.rollback()
        cursor.execute("""
            INSERT INTO pipeline_runs
                (task_name, started_at, finished_at, status, message)
            VALUES (%s, %s, now(), %s, %s)
        """, ("load_openaq", started, "failed", str(error)[:500]))
        connection.commit()
        raise
    finally:
        cursor.close()
        connection.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
