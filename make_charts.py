"""
Build the first-results charts from the warehouse.

  1. neighbourhood_api.png  - reconstructed API for every sensor site across
                              the haze episode: this-hour vs 24-hour
  2. validation_18sep.png   - reconstructed API vs official APIMS readings,
                              18 Sep 2026 afternoon
  3. model_vs_sensor.png    - Open-Meteo modelled PM2.5 vs a street-level
                              sensor near the Petaling Jaya station

The 24-hour rolling average is computed in SQL with a window function; the
DOE sub-index formula is applied in Python (api_subindex.py).

Usage:
    pip install pandas matplotlib psycopg2-binary
    python make_charts.py

Reads connection settings from .env in this folder, so no PowerShell
environment setup is needed.
"""

import glob
import json
import math
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # write files only, never open a window
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from api_subindex import api_to_pm25, pm25_to_api

TZ = "Asia/Kuala_Lumpur"
OUT_DIR = Path("charts")
MIN_HOURS_IN_WINDOW = 18     # a 24-h average needs at least this many hours
CALIBRATION_RADIUS_KM = 6.0  # sensors further out are shown but greyed

# Fallback, used only if raw_manual_readings is empty. Run load_manual.py to
# use the real hand-collected readings instead.
FALLBACK_EQMS = [
    ("Petaling Jaya", "2026-09-18 14:26", 165),
    ("Putrajaya",     "2026-09-18 14:31", 175),
    ("Batu Muda",     "2026-09-18 14:34", 169),
    ("Cheras",        "2026-09-18 14:40", 168),
]

# Same bands as the APIMS legend, so the charts read like the official map.
BANDS = [
    (0,   50,  "Good",           "#3b82f6"),
    (50,  100, "Moderate",       "#22c55e"),
    (100, 200, "Unhealthy",      "#eab308"),
    (200, 300, "Very unhealthy", "#f97316"),
    (300, 500, "Hazardous",      "#ef4444"),
]

SOURCE_NOTE = (
    "Data: OpenAQ (AirGradient community sensors). API computed with DOE "
    "Malaysia's published PM2.5 formula. Not official readings - see APIMS."
)

SENSOR_SQL = """
WITH hourly AS (
    SELECT r.location_id,
           s.site_name,
           s.nearest_doe_station,
           s.distance_km,
           r.measured_at,
           r.value AS pm25
    FROM raw_openaq_hourly r
    JOIN openaq_sites s USING (location_id)
    WHERE r.parameter = 'pm25'
      AND r.value IS NOT NULL
      AND r.value >= 0
)
SELECT *,
       AVG(pm25)   OVER w AS pm25_24h,
       COUNT(pm25) OVER w AS hours_in_window
FROM hourly
WINDOW w AS (
    PARTITION BY location_id
    ORDER BY measured_at
    RANGE BETWEEN INTERVAL '23 hours' PRECEDING AND CURRENT ROW
)
ORDER BY site_name, measured_at;
"""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_env(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from .env. Values already set in the shell win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


MANUAL_SQL = """
SELECT station_name, observed_at, api_value
FROM raw_manual_readings
WHERE source ILIKE '%%eqms%%'
  AND index_type ILIKE '%%malaysia%%'
  AND api_value IS NOT NULL
ORDER BY observed_at, station_name;
"""


def connect():
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", 5434)),
        dbname=os.environ.get("POSTGRES_DB", "haze"),
        user=os.environ.get("POSTGRES_USER", "haze"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


def query(sql: str) -> pd.DataFrame:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
    finally:
        connection.close()
    return pd.DataFrame(rows, columns=columns)


def fetch_sensor_series() -> pd.DataFrame:
    df = query(SENSOR_SQL)
    if df.empty:
        sys.exit("raw_openaq_hourly is empty - run load_openaq.py first.")
    df["measured_at"] = pd.to_datetime(df["measured_at"], utc=True).dt.tz_convert(TZ)
    for column in ("pm25", "pm25_24h", "distance_km"):
        df[column] = df[column].astype(float)
    return df


def fetch_official_readings() -> pd.DataFrame:
    """Hand-collected APIMS readings; falls back to the hardcoded set."""
    df = query(MANUAL_SQL)
    if df.empty:
        print("  note: raw_manual_readings is empty, using the built-in "
              "fallback readings (run load_manual.py to use your own)")
        df = pd.DataFrame(FALLBACK_EQMS,
                          columns=["station_name", "observed_at", "api_value"])
        df["observed_at"] = pd.to_datetime(df["observed_at"]).dt.tz_localize(TZ)
    else:
        df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(TZ)
    df["api_value"] = df["api_value"].astype(float)
    df["hour"] = df["observed_at"].dt.floor("h")
    # Two readings of one station in the same hour (e.g. 2:26pm and 2:40pm)
    # are one observation; average them rather than double-counting.
    return (df.groupby(["station_name", "hour"], as_index=False)["api_value"]
              .mean())


def add_api(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the DOE formula to the hourly value and to the 24-h average."""
    df = df.copy()
    df["api_live"] = df["pm25"].map(pm25_to_api)
    enough = df["hours_in_window"] >= MIN_HOURS_IN_WINDOW
    df["api_24h"] = df["pm25_24h"].where(enough).map(
        lambda v: pm25_to_api(v) if pd.notna(v) else np.nan
    )
    return df


def load_openmeteo(station_slug: str = "petaling-jaya"):
    """Latest raw Open-Meteo file for one station, or None if absent."""
    paths = sorted(glob.glob(f"raw/air_quality/*/{station_slug}.json"))
    if not paths:
        return None
    record = json.loads(Path(paths[-1]).read_text())
    hourly = record["response"]["hourly"]
    df = pd.DataFrame({
        "time": pd.to_datetime(hourly["time"]).tz_localize(TZ),
        "pm25": pd.to_numeric(pd.Series(hourly["pm2_5"]), errors="coerce"),
    })
    fetched = pd.to_datetime(record["_meta"]["fetched_at_utc"], utc=True).tz_convert(TZ)
    return df, fetched


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def shade_bands(ax, ymax: float) -> None:
    for low, high, _, colour in BANDS:
        if low >= ymax:
            break
        ax.axhspan(low, min(high, ymax), color=colour, alpha=0.10, lw=0)
    ax.axhline(200, color="#b91c1c", lw=0.8, ls=":")
    ax.set_ylim(0, ymax)


def chart_neighbourhoods(df: pd.DataFrame, path: Path) -> None:
    sites = (df.groupby("site_name")["distance_km"].first()
               .sort_values().index.tolist())
    cols = 2
    rows = math.ceil(len(sites) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(14, 3.1 * rows),
                             sharex=True, sharey=True)
    axes = np.atleast_1d(axes).flatten()

    peak = np.nanmax(df["api_live"].to_numpy())
    ymax = min(500.0, max(220.0, peak * 1.08))

    for ax, site in zip(axes, sites):
        s = df[df["site_name"] == site]
        first = s.iloc[0]
        shade_bands(ax, ymax)
        ax.plot(s["measured_at"], s["api_live"], color="#64748b",
                lw=0.7, alpha=0.7, label="This hour")
        ax.plot(s["measured_at"], s["api_24h"], color="#0f172a",
                lw=1.8, label="24-hour average (official method)")
        ax.set_title(
            f"{site}  |  {first['distance_km']:.1f} km from DOE "
            f"{first['nearest_doe_station']}",
            fontsize=10, loc="left",
        )
        ax.grid(axis="y", color="#e2e8f0", lw=0.5)

    for ax in axes[len(sites):]:
        ax.set_visible(False)

    axes[0].legend(loc="upper left", fontsize=8, frameon=False)
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b", tz=None))
    fig.supylabel("Malaysian API (PM2.5 sub-index)")
    fig.suptitle(
        "Haze by neighbourhood, Klang Valley - reconstructed on the official "
        "Malaysian API scale\nDotted red line: API 200, the school-closure trigger",
        fontsize=12, x=0.01, ha="left",
    )
    fig.text(0.01, 0.005, SOURCE_NOTE, fontsize=7.5, color="#475569")
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(path, dpi=160)
    plt.close(fig)


def validation_rows(df: pd.DataFrame, official: pd.DataFrame) -> pd.DataFrame:
    """Pair every official reading with each sensor mapped to that station.

    One row per (sensor, hour). A sensor mapped to two stations is compared
    against each, since both pairings are legitimate tests.
    """
    rows = []
    for reading in official.itertuples():
        nearby = df[(df["nearest_doe_station"] == reading.station_name)
                    & (df["measured_at"] == reading.hour)]
        for sensor in nearby.itertuples():
            rows.append({
                "sensor": sensor.site_name,
                "station": reading.station_name,
                "hour": reading.hour,
                "distance_km": sensor.distance_km,
                "official_api": reading.api_value,
                "reconstructed_api": sensor.api_24h,
            })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.dropna(subset=["reconstructed_api"])
    if out.empty:
        return out
    out["gap"] = out["reconstructed_api"] - out["official_api"]
    out["within_radius"] = out["distance_km"] <= CALIBRATION_RADIUS_KM
    return out.sort_values(["hour", "distance_km"]).reset_index(drop=True)


def chart_validation(pairs: pd.DataFrame, path: Path) -> None:
    """Official vs reconstructed, one point per sensor-hour, with a 1:1 line.

    A scatter rather than bars: it keeps working as more readings are
    collected, and the distance from the 1:1 line is the error, read directly.
    """
    fig, ax = plt.subplots(figsize=(8.2, 7.4))

    low = min(pairs[["official_api", "reconstructed_api"]].min()) - 15
    high = max(pairs[["official_api", "reconstructed_api"]].max()) + 15
    limits = (max(0, low), high)

    ax.plot(limits, limits, color="#0f172a", lw=1, ls="--", zorder=1,
            label="Perfect agreement")
    for margin, shade in ((10, 0.10), (20, 0.06)):
        ax.fill_between(limits,
                        [limits[0] - margin, limits[1] - margin],
                        [limits[0] + margin, limits[1] + margin],
                        color="#0f172a", alpha=shade, lw=0, zorder=0)

    markers = {}
    for i, hour in enumerate(sorted(pairs["hour"].unique())):
        markers[hour] = ["o", "s", "^", "D", "v", "P"][i % 6]

    for hour, marker in markers.items():
        for within, colour, edge in ((True, "#0f172a", "white"),
                                     (False, "#cbd5e1", "#94a3b8")):
            subset = pairs[(pairs["hour"] == hour) & (pairs["within_radius"] == within)]
            if subset.empty:
                continue
            label = (f"{pd.Timestamp(hour):%H:%M}" if within else None)
            ax.scatter(subset["official_api"], subset["reconstructed_api"],
                       marker=marker, s=95, color=colour, edgecolor=edge,
                       lw=0.8, zorder=3, label=label)

    # One label per sensor, placed at its highest point, so clustered
    # observations from different hours do not print on top of each other.
    for sensor, block in pairs.groupby("sensor"):
        anchor = block.loc[block["reconstructed_api"].idxmax()]
        short = sensor.split()[0].title()
        ax.annotate(f"{short} ({anchor.distance_km:.1f} km)",
                    (anchor.official_api, anchor.reconstructed_api),
                    textcoords="offset points", xytext=(9, 4),
                    fontsize=7.5, color="#334155")

    near = pairs[pairs["within_radius"]]
    mae = near["gap"].abs().mean()
    bias = near["gap"].mean()

    ax.set_xlim(limits)
    ax.set_ylim(limits)
    ax.set_aspect("equal")
    ax.set_xlabel("Official APIMS reading")
    ax.set_ylabel("Reconstructed from community sensor")
    ax.set_title(
        "Do community sensors reproduce the official API?\n"
        f"Shaded bands: within 10 and 20 API points. Grey points: sensors "
        f"more than {CALIBRATION_RADIUS_KM:.0f} km away",
        fontsize=11, loc="left",
    )
    ax.grid(color="#e2e8f0", lw=0.5)
    ax.legend(title="Reading hour", loc="upper left", fontsize=8,
              title_fontsize=8, frameon=False)

    ax.text(0.98, 0.04,
            f"Within {CALIBRATION_RADIUS_KM:.0f} km  (n={len(near)})\n"
            f"Mean absolute error  {mae:.1f}\n"
            f"Mean bias  {bias:+.1f}",
            transform=ax.transAxes, ha="right", fontsize=8.5, color="#0f172a",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#f8fafc",
                      edgecolor="#cbd5e1", lw=0.6))

    hours = len(pairs["hour"].unique())
    fig.text(0.01, 0.01, SOURCE_NOTE +
             f"\n{len(pairs)} paired observations across {hours} hour(s) on "
             "one day - an early result, not a validated accuracy figure.",
             fontsize=7, color="#475569")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(path, dpi=160)
    plt.close(fig)


def chart_model_vs_sensor(df: pd.DataFrame, official: pd.DataFrame,
                          openmeteo, path: Path):
    model, fetched = openmeteo
    pj = df[df["nearest_doe_station"] == "Petaling Jaya"]
    if pj.empty:
        return None
    site = pj.sort_values("distance_km")["site_name"].iloc[0]
    sensor = pj[pj["site_name"] == site]

    start, end = sensor["measured_at"].min(), sensor["measured_at"].max()
    model = model[(model["time"] >= start) & (model["time"] <= end)]

    fig, ax = plt.subplots(figsize=(12, 4.2))

    # Everything after the model run is forecast, not analysis - the
    # distinction matters, so shade it rather than leaving one flat line.
    if start <= fetched <= end:
        ax.axvspan(fetched, end, color="#dbeafe", alpha=0.45, lw=0, zorder=0)

    ax.plot(sensor["measured_at"], sensor["pm25"], color="#0f172a", lw=1.2,
            label=f"Community sensor (measured): {site}")
    ax.plot(model["time"], model["pm25"], color="#2563eb", lw=1.2,
            label="Open-Meteo / CAMS (modelled)")

    pj_official = official[official["station_name"] == "Petaling Jaya"]
    for i, reading in enumerate(pj_official.itertuples()):
        implied = api_to_pm25(reading.api_value)
        ax.scatter([reading.hour], [implied], color="#dc2626", zorder=3, s=45,
                   label=("Official Petaling Jaya, converted to the 24-h "
                          "average PM2.5 it implies" if i == 0 else None))

    if start <= fetched <= end:
        ax.axvline(fetched, color="#2563eb", lw=1, ls="--", zorder=2)
        ax.text(fetched, ax.get_ylim()[1] * 0.96,
                "  model run: analysis <- | -> forecast",
                fontsize=8, color="#1d4ed8", va="top")

    ax.set_ylabel("PM2.5 (ug/m3)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b", tz=None))
    ax.grid(axis="y", color="#e2e8f0", lw=0.5)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_title("A free global model vs a street-level sensor, near the "
                 "Petaling Jaya station", fontsize=11, loc="left")
    fig.text(0.01, 0.01, "Data: OpenAQ (AirGradient), Open-Meteo / CAMS. "
             "Official point derived from the APIMS reading via DOE's formula.",
             fontsize=7.5, color="#475569")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return site


# ---------------------------------------------------------------------------

def main() -> int:
    load_env()
    if not os.environ.get("POSTGRES_PASSWORD"):
        sys.exit("POSTGRES_PASSWORD not found in .env or the environment.")

    OUT_DIR.mkdir(exist_ok=True)
    df = add_api(fetch_sensor_series())
    print(f"{df['site_name'].nunique()} sites, {len(df)} hourly rows, "
          f"{df['measured_at'].min():%d %b %H:%M} -> "
          f"{df['measured_at'].max():%d %b %H:%M}")

    official = fetch_official_readings()
    print(f"{len(official)} official reading(s) across "
          f"{official['hour'].nunique()} hour(s)\n")

    chart_neighbourhoods(df, OUT_DIR / "neighbourhood_api.png")
    print(f"  saved {OUT_DIR / 'neighbourhood_api.png'}")

    pairs = validation_rows(df, official)
    if pairs.empty:
        print("  skipped validation chart - no sensor data at any reading hour")
    else:
        chart_validation(pairs, OUT_DIR / "validation.png")
        print(f"  saved {OUT_DIR / 'validation.png'}")

    openmeteo = load_openmeteo()
    if openmeteo is None:
        print("  skipped model chart - no raw/air_quality/*/petaling-jaya.json")
    else:
        site = chart_model_vs_sensor(df, official, openmeteo,
                                     OUT_DIR / "model_vs_sensor.png")
        print(f"  saved {OUT_DIR / 'model_vs_sensor.png'} (compared with {site})")

    # Numbers for the write-up.
    print("\nPeak 24-hour API per site")
    peaks = (df.dropna(subset=["api_24h"])
               .loc[lambda d: d.groupby("site_name")["api_24h"].idxmax()]
               .sort_values("api_24h", ascending=False))
    for r in peaks.itertuples():
        print(f"  {r.site_name:<30} {r.api_24h:5.0f}   {r.measured_at:%d %b %H:%M}")

    if not pairs.empty:
        for hour, block in pairs.groupby("hour"):
            print(f"\nValidation, {pd.Timestamp(hour):%d %b %H:%M}")
            for r in block.itertuples():
                flag = "" if r.within_radius else "   (far)"
                print(f"  {r.sensor:<30} {r.distance_km:4.1f} km  "
                      f"official {r.official_api:3.0f}  "
                      f"reconstructed {r.reconstructed_api:5.0f}  "
                      f"gap {r.gap:+4.0f}{flag}")

        near = pairs[pairs["within_radius"]]
        print(f"\nAll pairs within {CALIBRATION_RADIUS_KM:.0f} km "
              f"(n={len(near)})")
        print(f"  mean absolute error  {near['gap'].abs().mean():5.1f} API points")
        print(f"  mean bias            {near['gap'].mean():+5.1f}")
        print(f"  worst                {near['gap'].abs().max():5.1f}")
        print("\nPer sensor, within radius")
        summary = (near.groupby("sensor")
                       .agg(n=("gap", "size"),
                            distance_km=("distance_km", "first"),
                            mean_gap=("gap", "mean"),
                            mae=("gap", lambda s: s.abs().mean()))
                       .sort_values("distance_km"))
        for name, row in summary.iterrows():
            print(f"  {name:<30} {row.distance_km:4.1f} km  n={row.n:.0f}  "
                  f"bias {row.mean_gap:+6.1f}  mae {row.mae:5.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
