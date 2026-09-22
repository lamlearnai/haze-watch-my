"""
Sensor quality check: compare every site against the network, not against
a single DOE station.

Motivation: Sejati Residences Cyberjaya reconstructed ~46 API points below
its nearest official station across all three reading hours on 18 Sep 2026.
One station is a weak reference - it may itself sit in a local hotspot. This
compares each site against the median of all the others at the same hour,
over the full month, which is a far stronger test.

The two hypotheses this separates:

  Multiplicative  site = k * network      A stable RATIO points at sensor
                                          gain or a persistent local factor.
  Additive        site = network + c      A stable DIFFERENCE points at a
                                          constant local source or sink.

If neither is stable - if the gap only opens during haze, or only at night -
the cause is situational, not a simple calibration error.

Usage:
    python investigate_sensors.py
    python investigate_sensors.py --site "Sejati"

Reads connection settings from .env.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "Asia/Kuala_Lumpur"
HAZE_THRESHOLD = 55.0   # network median PM2.5 above this = hazy hour
MIN_SITES_FOR_MEDIAN = 4

SQL = """
SELECT s.site_name,
       s.distance_km,
       s.nearest_doe_station,
       r.measured_at,
       r.value AS pm25
FROM raw_openaq_hourly r
JOIN openaq_sites s USING (location_id)
WHERE r.parameter = 'pm25'
  AND r.value IS NOT NULL
  AND r.value >= 0
ORDER BY r.measured_at;
"""


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def fetch() -> pd.DataFrame:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")

    connection = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", 5434)),
        dbname=os.environ.get("POSTGRES_DB", "haze"),
        user=os.environ.get("POSTGRES_USER", "haze"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL)
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
    finally:
        connection.close()

    if not rows:
        sys.exit("raw_openaq_hourly is empty - run load_openaq.py first.")

    df = pd.DataFrame(rows, columns=columns)
    df["measured_at"] = pd.to_datetime(df["measured_at"], utc=True).dt.tz_convert(TZ)
    df["pm25"] = df["pm25"].astype(float)
    df["distance_km"] = df["distance_km"].astype(float)
    return df


def add_peer_reference(df: pd.DataFrame) -> pd.DataFrame:
    """For each site-hour, the median of every OTHER site that same hour.

    Leave-one-out: including the site in its own reference would pull the
    median toward it and mask exactly the deviation being measured.
    """
    wide = df.pivot_table(index="measured_at", columns="site_name", values="pm25")
    enough = wide.notna().sum(axis=1) >= MIN_SITES_FOR_MEDIAN
    wide = wide[enough]

    records = []
    for site in wide.columns:
        others = wide.drop(columns=[site]).median(axis=1, skipna=True)
        block = pd.DataFrame({
            "site_name": site,
            "measured_at": wide.index,
            "pm25": wide[site].to_numpy(),
            "peer_median": others.to_numpy(),
        }).dropna(subset=["pm25", "peer_median"])
        records.append(block)

    out = pd.concat(records, ignore_index=True)
    out = out[out["peer_median"] > 5]          # ratios are meaningless near zero
    out["ratio"] = out["pm25"] / out["peer_median"]
    out["diff"] = out["pm25"] - out["peer_median"]
    out["regime"] = np.where(out["peer_median"] >= HAZE_THRESHOLD, "hazy", "clean")
    out["hour_of_day"] = out["measured_at"].dt.hour
    return out


def model_fit(block: pd.DataFrame) -> tuple[float, float]:
    """RMSE of the multiplicative and additive models, both in ug/m3.

    Comparing the spread of `ratio` against the spread of `diff` directly
    would be meaningless - they are different units, and a coefficient of
    variation on `diff` explodes whenever the mean difference is near zero.
    Instead, fit each model and score both by how far its prediction misses,
    in the same physical units.
    """
    k = block["ratio"].median()
    c = block["diff"].median()
    peer = block["peer_median"].to_numpy()
    actual = block["pm25"].to_numpy()

    mult_rmse = float(np.sqrt(np.mean((actual - k * peer) ** 2)))
    add_rmse = float(np.sqrt(np.mean((actual - (peer + c)) ** 2)))
    return mult_rmse, add_rmse


def report(peers: pd.DataFrame, meta: dict) -> None:
    sites = sorted(peers["site_name"].unique())

    print("Each site vs the median of the other sites, same hour")
    print(f"(ratio 1.00 = matches the network; {len(peers)} site-hours)\n")
    print(f"  {'site':<30} {'n':>5} {'ratio':>7} {'diff':>8} "
          f"{'ratio clean':>12} {'ratio hazy':>11}")
    print("  " + "-" * 78)

    rows = []
    for site in sites:
        block = peers[peers["site_name"] == site]
        clean = block[block["regime"] == "clean"]["ratio"]
        hazy = block[block["regime"] == "hazy"]["ratio"]
        mult_rmse, add_rmse = model_fit(block)
        row = {
            "site": site,
            "n": len(block),
            "ratio": block["ratio"].median(),
            "diff": block["diff"].median(),
            "ratio_clean": clean.median() if len(clean) > 20 else np.nan,
            "ratio_hazy": hazy.median() if len(hazy) > 20 else np.nan,
            "mult_rmse": mult_rmse,
            "add_rmse": add_rmse,
        }
        rows.append(row)
        print(f"  {site:<30} {row['n']:>5} {row['ratio']:>7.2f} "
              f"{row['diff']:>8.1f} {row['ratio_clean']:>12.2f} "
              f"{row['ratio_hazy']:>11.2f}")

    summary = pd.DataFrame(rows)

    print("\nWhich model explains the site better?")
    print("  (RMSE in ug/m3 - how far each model's prediction misses)\n")
    print(f"  {'site':<30} {'x ratio':>9} {'+ offset':>9}   verdict")
    print("  " + "-" * 78)
    for row in summary.itertuples():
        regime_shift = (abs(row.ratio_clean - row.ratio_hazy)
                        if not (np.isnan(row.ratio_clean) or np.isnan(row.ratio_hazy))
                        else 0.0)
        margin = 0.9  # one model must beat the other by 10% to be called

        if abs(row.ratio - 1) < 0.10 and abs(row.diff) < 8:
            verdict = "tracks the network"
        elif regime_shift > 0.20:
            verdict = f"REGIME-DEPENDENT - gap opens in haze ({regime_shift:+.2f})"
        elif row.mult_rmse < row.add_rmse * margin:
            verdict = f"MULTIPLICATIVE - reads {row.ratio:.0%} of network"
        elif row.add_rmse < row.mult_rmse * margin:
            verdict = f"ADDITIVE - constant {row.diff:+.0f} ug/m3"
        else:
            verdict = "both fit equally - cannot separate"
        print(f"  {row.site:<30} {row.mult_rmse:>9.1f} {row.add_rmse:>9.1f}   {verdict}")

    # The reference is the other sites, so if many of them deviate the
    # reference itself is skewed and every ratio shifts together.
    off_network = (summary["ratio"] - 1).abs() > 0.10
    if off_network.sum() > len(summary) / 3:
        print(f"\n  WARNING: {off_network.sum()} of {len(summary)} sites deviate "
              ">10% from the network median.\n"
              "  The reference is built from the other sites, so with this many "
              "outliers it is\n  itself skewed - treat the ratios as relative, "
              "not absolute. A reference-grade\n  monitor would be needed to say "
              "which sites are actually right.")

    # A site that is genuinely cleaner should still respond to the same
    # episodes. A sensor with a gain fault flattens them.
    print("\nDoes the site follow the network's ups and downs?")
    print("  (correlation of hourly values; low = not measuring the same air)\n")
    for site in sites:
        block = peers[peers["site_name"] == site]
        r = block["pm25"].corr(block["peer_median"])
        bar = "#" * int(max(0, r) * 40)
        print(f"  {site:<30} r={r:5.2f}  {bar}")

    if meta:
        print("\nOpenAQ metadata for the flagged site")
        for key in ("location_id", "name", "provider", "latitude", "longitude",
                    "is_monitor", "instruments", "first_data", "last_data"):
            if key in meta:
                print(f"  {key:<14} {meta[key]}")


def diurnal(peers: pd.DataFrame, site: str) -> None:
    block = peers[peers["site_name"].str.contains(site, case=False, na=False)]
    if block.empty:
        print(f"\nNo site matching {site!r}")
        return

    name = block["site_name"].iloc[0]
    print(f"\nRatio by hour of day: {name}")
    print("  (a flat line means the deviation is not time-driven)\n")
    by_hour = block.groupby("hour_of_day")["ratio"].median()
    for hour, ratio in by_hour.items():
        bar = "#" * int(round(ratio * 30))
        print(f"  {hour:02d}:00  {ratio:5.2f}  {bar}")

    print(f"\n  daytime  (08-18)  {by_hour.loc[8:18].median():.2f}")
    night = pd.concat([by_hour.loc[:6], by_hour.loc[19:]])
    print(f"  night    (19-06)  {night.median():.2f}")


def find_meta(site_query: str) -> dict:
    path = Path("raw/openaq_discovery.json")
    if not path.exists():
        return {}
    discovery = json.loads(path.read_text())
    for entries in discovery.values():
        for entry in entries:
            if site_query.lower() in (entry.get("name") or "").lower():
                return entry
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="Sejati",
                        help="site to examine in detail (substring match)")
    args = parser.parse_args()

    load_env()
    if not os.environ.get("POSTGRES_PASSWORD"):
        sys.exit("POSTGRES_PASSWORD not found in .env or the environment.")

    df = fetch()
    print(f"{df['site_name'].nunique()} sites, {len(df)} hourly rows, "
          f"{df['measured_at'].min():%d %b} -> {df['measured_at'].max():%d %b}\n")

    peers = add_peer_reference(df)
    report(peers, find_meta(args.site))
    diurnal(peers, args.site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
