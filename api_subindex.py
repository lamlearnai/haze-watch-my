"""
Malaysia Air Pollutant Index (API) - PM2.5 sub-index.

Source: Department of Environment Malaysia,
"PENGIRAAN INDEKS PENCEMAR UDARA (IPU) / API CALCULATION"
https://www.doe.gov.my/wp-content/uploads/2021/09/API_Calculation.pdf

The API is the highest sub-index across six pollutants. During haze PM2.5 is
almost always dominant, so this module implements PM2.5 only.

IMPORTANT: X is the 24-HOUR RUNNING AVERAGE of PM2.5 in ug/m3, not a spot
reading. Feeding a single hourly value in gives you "what the API would say if
this hour lasted a full day" - useful, but it is not the official number.

Known quirks in the published table, and how this module handles them:
  - 75.5 appears as both the end of band 51-100 and the start of 101-200.
    Resolved by evaluating bands in order and taking the first match, so
    75.5 -> band 51-100.
  - Gaps exist between bands: 12.0-12.1, 150.4-150.5, 250.4-250.5.
    Resolved by extending each band's upper edge to the next band's lower
    edge, so no concentration is unclassifiable.
  - The 301-400 row is published with range start 250.4 but offset 250.5.
    The offset is kept exactly as published; only the range edge is adjusted.
Document these choices in the README rather than silently "fixing" them.
"""

from typing import NamedTuple


class Band(NamedTuple):
    conc_min: float      # lower concentration bound, inclusive
    conc_max: float      # upper concentration bound, inclusive
    slope: float         # multiplier from the published equation
    offset: float        # concentration subtracted in the published equation
    intercept: float     # API value added in the published equation


# Bands exactly as published, with upper edges extended to close the gaps.
PM25_BANDS = [
    Band(0.0,   12.1,  4.1667,  0.0,   0.0),
    Band(12.1,  75.5,  0.7741,  12.1,  51.0),
    Band(75.5,  150.5, 1.3218,  75.5,  101.0),
    Band(150.5, 250.4, 0.9909,  150.5, 201.0),
    Band(250.4, 350.5, 0.9909,  250.5, 301.0),
    Band(350.5, 500.4, 0.6604,  350.5, 401.0),
]

API_CEILING = 500.0


def pm25_to_api(concentration: float) -> float:
    """Convert a PM2.5 concentration (ug/m3) to a Malaysian API sub-index.

    Pass the 24-hour running average for the official figure. Pass a single
    hourly value to get the "live" sub-index used to expose the lag.
    """
    if concentration < 0:
        raise ValueError(f"concentration cannot be negative: {concentration}")

    for band in PM25_BANDS:
        if band.conc_min <= concentration <= band.conc_max:
            return band.slope * (concentration - band.offset) + band.intercept

    # Above the published table. The DOE scale tops out at 500 (Emergency).
    return API_CEILING


def api_to_pm25(api: float) -> float:
    """Invert the sub-index: given an API value, the concentration implied.

    Useful because APIMS publishes the index but not the concentration. This
    recovers the 24-hour average PM2.5 that must have produced a reading, so
    it can be compared against measured concentrations from other sources.
    """
    if api < 0:
        raise ValueError(f"API cannot be negative: {api}")

    for band in PM25_BANDS:
        api_low = band.intercept
        api_high = band.slope * (band.conc_max - band.offset) + band.intercept
        if api_low <= api <= api_high:
            return (api - band.intercept) / band.slope + band.offset

    raise ValueError(f"API {api} is outside the published table")


def api_band_label(api: float) -> str:
    """The DOE status label for an API value."""
    if api <= 50:
        return "Good"
    if api <= 100:
        return "Moderate"
    if api <= 200:
        return "Unhealthy"
    if api <= 300:
        return "Very Unhealthy"
    if api <= 500:
        return "Hazardous"
    return "Emergency"


if __name__ == "__main__":
    # Band boundaries should land on the published API values.
    checks = [
        (12.0, 50.0),
        (75.5, 100.1),
        (150.4, 200.1),
        (250.4, 300.1),
    ]
    print("Boundary checks")
    for conc, expected in checks:
        got = pm25_to_api(conc)
        print(f"  {conc:>6.1f} ug/m3 -> API {got:6.1f}  (expected ~{expected})")

    # Real readings collected by hand, Klang Valley, 18 Sep 2026 ~3:15pm.
    # IQAir reports US AQI; its PM2.5 concentration is re-scored here on the
    # Malaysian table to show how much of the gap is scale rather than timing.
    print("\nIQAir concentrations re-scored on the Malaysian table")
    observed = [
        ("Petaling Jaya", 128.3, 204, 164),
        ("Putrajaya",      92.9, 177, 174),
        ("Batu Muda",      81.1, 169, 168),
        ("Cheras",         79.7, 168, 167),
    ]
    for station, conc, us_aqi, eqms_api in observed:
        my_api = pm25_to_api(conc)
        implied = api_to_pm25(eqms_api)
        print(
            f"  {station:<14} PM2.5 {conc:6.1f} | "
            f"US AQI {us_aqi:3d} | MY API {my_api:5.1f} | "
            f"EQMS {eqms_api:3d} -> implied 24h avg {implied:6.1f} ug/m3 | "
            f"{api_band_label(my_api)}"
        )

    # Round trip: every API value should invert back to its own concentration.
    print("\nRound-trip check")
    for api in (25, 75, 150, 165, 250, 350):
        conc = api_to_pm25(api)
        back = pm25_to_api(conc)
        status = "ok" if abs(back - api) < 0.01 else "MISMATCH"
        print(f"  API {api:3d} -> {conc:7.2f} ug/m3 -> API {back:6.2f}  {status}")
