"""
Load hand-collected APIMS / IQAir readings into Postgres.

These rows are the calibration reference for the whole project. APIMS keeps
no public archive, so once an hour passes these observations cannot be
recovered - they are the most valuable rows in the database.

Expects manual_readings.csv exported from Google Sheets, with the columns:
    Area, Date of Reading, Time of Reading, API Readings,
    Station Name, Source, Index Type, pm25_ugm3
Column matching is case- and space-insensitive, and a few common variants
are accepted, so light renaming in the sheet will not break the load.

Usage:
    python load_manual.py
    python load_manual.py --file other.csv --dry-run

Writes: raw_manual_readings (idempotent on station + timestamp + source)
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")

TZ = ZoneInfo("Asia/Kuala_Lumpur")
DEFAULT_CSV = Path("manual_readings.csv")

# Canonical field -> accepted header spellings (normalised: lowercase, no
# spaces, underscores or punctuation).
FIELD_ALIASES = {
    "area":        ["area", "location"],
    "date":        ["dateofreading", "date", "readingdate"],
    "time":        ["timeofreading", "time", "readingtime"],
    "api":         ["apireadings", "apireading", "api", "apivalue"],
    "station":     ["stationname", "station"],
    "source":      ["source"],
    "index_type":  ["indextype", "index"],
    "pm25":        ["pm25ugm3", "pm25", "pm25ug", "concentration"],
    "dominant":    ["dominantpollutant", "dominant", "pollutant"],
    "notes":       ["notes", "note", "comment"],
}

# Station names as they appear in stations.yml / doe_stations. The sheet's
# "Station Name" column should already match, but map the obvious variants.
STATION_CANONICAL = {
    "petaling jaya": "Petaling Jaya",
    "putrajaya": "Putrajaya",
    "cyberjaya/putrajaya": "Putrajaya",
    "batu muda": "Batu Muda",
    "kampung batu muda": "Batu Muda",
    "cheras": "Cheras",
}

TIME_FORMATS = ["%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"]
DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]


def normalise(header: str) -> str:
    return "".join(c for c in header.lower() if c.isalnum())


def map_headers(headers: list[str]) -> dict[str, str]:
    """Match the CSV's actual headers to canonical field names."""
    lookup = {normalise(h): h for h in headers}
    mapping = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[field] = lookup[alias]
                break
    missing = [f for f in ("date", "time", "api", "station") if f not in mapping]
    if missing:
        sys.exit(f"CSV is missing required column(s): {', '.join(missing)}\n"
                 f"Headers found: {headers}")
    return mapping


def parse_datetime(date_text: str, time_text: str) -> datetime:
    """Combine the sheet's date and time columns into one aware timestamp.

    Day-first is tried before month-first: the sheet is exported from a
    Malaysian locale, so 18/9/2026 means 18 September.
    """
    date_text, time_text = date_text.strip(), time_text.strip().upper()

    parsed_date = None
    for fmt in DATE_FORMATS:
        try:
            parsed_date = datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        raise ValueError(f"unrecognised date: {date_text!r}")

    parsed_time = None
    for fmt in TIME_FORMATS:
        try:
            parsed_time = datetime.strptime(time_text, fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        raise ValueError(f"unrecognised time: {time_text!r}")

    return datetime.combine(parsed_date, parsed_time, tzinfo=TZ)


def to_float(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def read_rows(path: Path) -> tuple[list[tuple], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            sys.exit(f"{path} appears to be empty.")
        columns = map_headers(reader.fieldnames)
        rows, problems = [], []

        for line_number, raw in enumerate(reader, start=2):
            def field(name):
                key = columns.get(name)
                return (raw.get(key) or "").strip() if key else ""

            if not field("date") and not field("api"):
                continue  # blank spacer row

            try:
                observed_at = parse_datetime(field("date"), field("time"))
            except ValueError as error:
                problems.append(f"  line {line_number}: {error}")
                continue

            station_raw = field("station") or field("area")
            station = STATION_CANONICAL.get(station_raw.lower(), station_raw)

            api_value = to_float(field("api"))
            source = field("source") or "EQMS"
            index_type = field("index_type") or "Malaysia API"

            # Guard against the scale mix-up: an IQAir row carrying a US AQI
            # value must not be filed as a Malaysian API reading.
            if "iqair" in source.lower() and "us" not in index_type.lower():
                problems.append(
                    f"  line {line_number}: IQAir row marked '{index_type}' "
                    "- IQAir reports US AQI, not Malaysian API")

            rows.append((
                station,
                observed_at,
                int(api_value) if api_value is not None else None,
                field("dominant") or None,
                source,
                index_type,
                to_float(field("pm25")),
                field("notes") or None,
            ))
    return rows, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and report, write nothing")
    args = parser.parse_args()

    if not args.file.exists():
        sys.exit(f"{args.file} not found. Export the sheet as CSV first "
                 "(File > Download > Comma Separated Values).")

    rows, problems = read_rows(args.file)
    if not rows:
        sys.exit("No usable rows found.")

    print(f"{len(rows)} row(s) parsed from {args.file}\n")
    for station, observed_at, api, _, source, index_type, pm25, _ in rows:
        pm = f"{pm25:6.1f} ug/m3" if pm25 is not None else "          -"
        print(f"  {observed_at:%Y-%m-%d %H:%M}  {station:<15} "
              f"{source:<6} {index_type:<13} API {api}  {pm}")

    if problems:
        print("\nWarnings:")
        print("\n".join(problems))

    by_source = {}
    for row in rows:
        by_source[row[4]] = by_source.get(row[4], 0) + 1
    print("\nBy source: " + ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())))

    eqms_hours = sorted({r[1].replace(minute=0, second=0, microsecond=0)
                         for r in rows if "eqms" in r[4].lower()})
    print(f"Distinct EQMS hours: {len(eqms_hours)}")
    for hour in eqms_hours:
        print(f"  {hour:%Y-%m-%d %H:00}")

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0

    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        # Fall back to .env so this works without shell setup.
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
            password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        sys.exit("POSTGRES_PASSWORD not set and not found in .env")

    started = datetime.now(timezone.utc)
    connection = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", 5434)),
        dbname=os.environ.get("POSTGRES_DB", "haze"),
        user=os.environ.get("POSTGRES_USER", "haze"),
        password=password,
    )
    connection.autocommit = False
    cursor = connection.cursor()
    try:
        execute_values(cursor, """
            INSERT INTO raw_manual_readings
                (station_name, observed_at, api_value, dominant_pollutant,
                 source, index_type, pm25_ugm3, notes)
            VALUES %s
            ON CONFLICT (station_name, observed_at, source) DO UPDATE SET
                api_value          = EXCLUDED.api_value,
                dominant_pollutant = EXCLUDED.dominant_pollutant,
                index_type         = EXCLUDED.index_type,
                pm25_ugm3          = EXCLUDED.pm25_ugm3,
                notes              = EXCLUDED.notes,
                ingested_at        = now()
        """, rows)

        cursor.execute("""
            INSERT INTO pipeline_runs
                (task_name, started_at, finished_at, status, rows_written)
            VALUES (%s, %s, now(), %s, %s)
        """, ("load_manual", started, "success", len(rows)))

        connection.commit()
        cursor.execute("SELECT count(*) FROM raw_manual_readings")
        print(f"\nCommitted. Table now holds {cursor.fetchone()[0]} rows.")
    except Exception as error:
        connection.rollback()
        cursor.execute("""
            INSERT INTO pipeline_runs
                (task_name, started_at, finished_at, status, message)
            VALUES (%s, %s, now(), %s, %s)
        """, ("load_manual", started, "failed", str(error)[:500]))
        connection.commit()
        raise
    finally:
        cursor.close()
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
