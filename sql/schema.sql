-- Haze Watch MY - schema
--
-- Layering: raw_* tables hold data as ingested, with as little interpretation
-- as possible. Anything derived (24h averages, reconstructed API, calibration)
-- is computed downstream in dbt, never here. If a transformation turns out to
-- be wrong, the raw tables let us recompute without re-fetching.

-- ---------------------------------------------------------------------------
-- Reference: the DOE stations we compare against
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doe_stations (
    station_name    TEXT PRIMARY KEY,
    station_site    TEXT,
    state           TEXT,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL
);

-- ---------------------------------------------------------------------------
-- Reference: OpenAQ sites, with their nearest DOE station for calibration
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS openaq_sites (
    location_id         INTEGER PRIMARY KEY,
    site_name           TEXT NOT NULL,
    provider            TEXT,
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    nearest_doe_station TEXT REFERENCES doe_stations(station_name),
    distance_km         NUMERIC(6, 2),
    first_seen          TIMESTAMPTZ,
    last_seen           TIMESTAMPTZ
);

-- Distance decides whether a site is usable for calibration; index it because
-- most queries filter on "within N km".
CREATE INDEX IF NOT EXISTS idx_openaq_sites_distance
    ON openaq_sites (nearest_doe_station, distance_km);

-- ---------------------------------------------------------------------------
-- Raw: hourly PM2.5 measurements from OpenAQ
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_openaq_hourly (
    location_id     INTEGER NOT NULL,
    sensor_id       INTEGER NOT NULL,
    measured_at     TIMESTAMPTZ NOT NULL,
    parameter       TEXT NOT NULL,
    value           DOUBLE PRECISION,
    units           TEXT,
    source_file     TEXT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Idempotency: re-running a load for the same hour updates rather than
    -- duplicating. Without this, every re-run doubles the data.
    PRIMARY KEY (sensor_id, measured_at, parameter)
);

CREATE INDEX IF NOT EXISTS idx_raw_openaq_time
    ON raw_openaq_hourly (measured_at DESC);

-- ---------------------------------------------------------------------------
-- Raw: official API readings, collected by hand from the APIMS portal
--
-- This is the calibration reference. APIMS keeps no public archive, so these
-- observations cannot be recovered once the hour passes - they are the most
-- valuable rows in the database.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_manual_readings (
    id                  SERIAL PRIMARY KEY,
    station_name        TEXT NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL,
    api_value           INTEGER,
    dominant_pollutant  TEXT,
    source              TEXT NOT NULL,          -- EQMS | IQAir
    index_type          TEXT NOT NULL,          -- Malaysia API | US AQI
    pm25_ugm3           DOUBLE PRECISION,       -- null for EQMS: index only
    notes               TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (station_name, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_manual_readings_time
    ON raw_manual_readings (observed_at DESC);

-- ---------------------------------------------------------------------------
-- Raw: modelled forecast from Open-Meteo
--
-- Kept for forecast SHAPE only. Validated against ground truth on 18 Sep 2026
-- and found to understate a haze episode roughly threefold, so its magnitude
-- must never be presented as a concentration.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_openmeteo_hourly (
    station_name    TEXT NOT NULL,
    measured_at     TIMESTAMPTZ NOT NULL,
    pm2_5           DOUBLE PRECISION,
    pm10            DOUBLE PRECISION,
    us_aqi          DOUBLE PRECISION,
    is_forecast     BOOLEAN NOT NULL,
    grid_latitude   DOUBLE PRECISION,
    grid_longitude  DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ NOT NULL,
    source_file     TEXT NOT NULL,

    PRIMARY KEY (station_name, measured_at, fetched_at)
);

-- ---------------------------------------------------------------------------
-- Operational: one row per pipeline run, for freshness checks and alerting
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              SERIAL PRIMARY KEY,
    task_name       TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL,              -- success | failed | partial
    rows_written    INTEGER,
    message         TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_task
    ON pipeline_runs (task_name, started_at DESC);
