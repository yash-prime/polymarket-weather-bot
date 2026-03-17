"""
data/sources/ecmwf.py — ECMWF Open Data batch GRIB ingest job.

ECMWF delivers GRIB2/NetCDF files via the ecmwf-opendata SDK — it is NOT
a REST API. This module runs as a standalone APScheduler job every 6 hours.
It downloads the latest IFS/AIFS forecast, parses it with cfgrib, and writes
processed grid-point values to the ecmwf_snapshots DB table.

The main scan loop NEVER calls this module directly — it reads ECMWF data
from the DB only. See engine/weather.py for the read path.

System dependency: eccodes binary must be installed:
  Ubuntu/Debian: sudo apt install libeccodes-dev
  macOS:         brew install eccodes

Usage (called by APScheduler in main.py):
    from data.sources.ecmwf import run_ingest_job
    run_ingest_job()
"""
import logging
import os
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Variables to extract from GRIB files
_VARIABLES = [
    "2m_temperature",          # temperature_2m
    "total_precipitation",     # precipitation
    "10m_u_component_of_wind", # wind u-component
    "10m_v_component_of_wind", # wind v-component
]

# Metric name mapping: ECMWF short name → our canonical name
_METRIC_MAP = {
    "2m_temperature": "temperature_2m",
    "total_precipitation": "precipitation",
    "10m_u_component_of_wind": "wind_u_10m",
    "10m_v_component_of_wind": "wind_v_10m",
}


def run_ingest_job(db_path: str | None = None) -> int:
    """
    Download and ingest the latest ECMWF Open Data forecast into ecmwf_snapshots.

    Returns the number of rows written to the DB.
    Logs an error and returns 0 on failure — the main scan loop continues
    without ECMWF data (degraded mode).
    """
    try:
        return _ingest(db_path)
    except ImportError as exc:
        logger.error(
            "ECMWF ingest failed — ecmwf-opendata or cfgrib not installed: %s. "
            "Install: pip install ecmwf-opendata cfgrib && apt install libeccodes-dev",
            exc,
        )
        return 0
    except Exception as exc:
        logger.error("ECMWF ingest job failed: %s", exc, exc_info=True)
        return 0


def _ingest(db_path: str | None = None) -> int:
    """Core ingest logic — separated for easier testing."""
    from ecmwf.opendata import Client  # type: ignore[import]
    import cfgrib  # type: ignore[import]
    import numpy as np

    from db.init import get_connection

    client = Client("ecmwf")
    rows_written = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        grib_path = os.path.join(tmpdir, "ecmwf_latest.grib2")

        logger.info("Downloading ECMWF Open Data GRIB2 file...")
        client.retrieve(
            step=[0, 24, 48, 72, 96, 120, 144, 168],
            param=["2t", "tp", "10u", "10v"],  # ECMWF short names
            target=grib_path,
        )
        logger.info("ECMWF download complete: %s", grib_path)

        datasets = cfgrib.open_datasets(grib_path)
        ingested_at = datetime.now(timezone.utc).isoformat()

        with get_connection(db_path) as conn:
            for ds in datasets:
                for var_name in ds.data_vars:
                    canonical = _METRIC_MAP.get(var_name)
                    if canonical is None:
                        continue

                    da = ds[var_name]
                    # Extract lat/lon grids
                    lats = da.coords.get("latitude", da.coords.get("lat"))
                    lons = da.coords.get("longitude", da.coords.get("lon"))
                    times = da.coords.get("time", da.coords.get("valid_time"))

                    if lats is None or lons is None:
                        logger.warning("No lat/lon coords for var %s — skipping", var_name)
                        continue

                    lat_vals = lats.values.flatten()
                    lon_vals = lons.values.flatten()

                    # Subsample: only every 4th grid point to reduce DB size
                    # Full global grid at 0.25° = ~1M points — too large for SQLite
                    step = 4
                    for i in range(0, len(lat_vals), step):
                        for j in range(0, len(lon_vals), step):
                            try:
                                val = float(da.values.flat[i * len(lon_vals) + j])
                                if np.isnan(val):
                                    continue
                            except (IndexError, TypeError):
                                continue

                            forecast_date = str(times.values[0]) if times is not None else ingested_at

                            conn.execute(
                                "INSERT INTO ecmwf_snapshots "
                                "(lat, lon, metric, forecast_date, value, ingested_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    round(float(lat_vals[i]), 4),
                                    round(float(lon_vals[j]), 4),
                                    canonical,
                                    forecast_date,
                                    val,
                                    ingested_at,
                                ),
                            )
                            rows_written += 1

            conn.commit()

    logger.info("ECMWF ingest complete: %d rows written", rows_written)
    return rows_written


def get_nearest_snapshot(
    lat: float,
    lon: float,
    metric: str,
    db_path: str | None = None,
) -> float | None:
    """
    Read the most recent ECMWF snapshot for the nearest grid point.

    Used by engine/weather.py to incorporate ECMWF data without calling
    the download job inline.

    Returns the value as a float, or None if no snapshot is available.
    """
    from db.init import get_connection

    # Match within ±1° — ECMWF grid at 0.25°, subsampled every 4th point → 1° resolution
    tolerance = 1.0

    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM ecmwf_snapshots "
                "WHERE ABS(lat - ?) <= ? AND ABS(lon - ?) <= ? "
                "  AND metric = ? "
                "ORDER BY ABS(lat - ?) + ABS(lon - ?), ingested_at DESC "
                "LIMIT 1",
                (lat, tolerance, lon, tolerance, metric, lat, lon),
            ).fetchone()

        if row is None:
            logger.debug(
                "No ECMWF snapshot for lat=%.4f lon=%.4f metric=%s", lat, lon, metric
            )
            return None

        return float(row["value"])

    except Exception as exc:
        logger.error("ECMWF DB read failed: %s", exc)
        return None
