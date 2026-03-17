"""
engine/weather.py — Top-level weather probability computation.

This is the single entry point called by the scan loop:

    model_result = weather.compute(market)

Responsibilities
----------------
1. Extract location, metric, threshold, operator, and window from the
   market's parsed JSON field.
2. Fetch data from all live sources in parallel where possible:
     - Open-Meteo ensemble (50–100 members, global)
     - NOAA NWS (US lat/lon only)
     - ECMWF snapshot from the ecmwf_snapshots DB table (globally available)
3. Delegate to ensemble.compute_probability() for weighted aggregation.
4. Return a ModelResult, or None if no sources produced usable data.

Source routing
--------------
- Open-Meteo:  always attempted (global coverage)
- NOAA:        attempted only for US coordinates (24–50°N, 65–125°W)
- ECMWF:       read from DB snapshot (populated by the 6h ingest job)

Error handling
--------------
Each source failure is logged at WARNING level and treated as a degraded
source. The function returns None only when ALL sources fail — a degraded
but non-empty ModelResult is preferable to dropping the market entirely.

Caching
-------
_cache holds successful ModelResult objects keyed by
(round(lat,2), round(lon,2), metric, window_start). Entries expire after
_CACHE_TTL_SECONDS (30 min). None results are never cached. All cache
access is serialised through _cache_lock.
"""
import logging
import threading
import time
from typing import Any

from engine.ensemble import compute_probability
from engine.models import ModelResult
from market.models import Market

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process result cache
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS: float = 30 * 60  # 30 minutes

# Maps (round(lat,2), round(lon,2), metric, window_start) -> (timestamp, ModelResult)
_cache: dict[tuple, tuple[float, ModelResult]] = {}
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Raw data caches — shared across all threshold bins for the same location/date
# ---------------------------------------------------------------------------
_RAW_CACHE_TTL_SECONDS: float = 30 * 60  # 30 minutes

# Open-Meteo: (round(lat,2), round(lon,2), variable, window_start) -> (timestamp, dict|None)
_raw_om_cache: dict[tuple, tuple[float, Any]] = {}
_raw_om_cache_lock = threading.Lock()

# NOAA: (round(lat,2), round(lon,2)) -> (timestamp, dict|None)
_raw_noaa_cache: dict[tuple, tuple[float, Any]] = {}
_raw_noaa_cache_lock = threading.Lock()

# Map canonical metric names (from LLM parser) → Open-Meteo API variable names
# Open-Meteo ensemble returns per-member hourly series for these variables.
_METRIC_TO_OM_VARIABLE: dict[str, str] = {
    "temperature_2m_max": "temperature_2m",
    "temperature_2m_min": "temperature_2m",
    "precipitation_sum": "precipitation",
    "wind_speed_10m_max": "wind_speed_10m",
}

# Bounding box for US CONUS (NOAA NWS coverage)
_US_LAT_MIN, _US_LAT_MAX = 24.0, 50.0
_US_LON_MIN, _US_LON_MAX = -125.0, -65.0


def compute(market: Market, db_path: str | None = None) -> ModelResult | None:
    """
    Compute the ensemble weather probability for a market.

    Parameters
    ----------
    market   : Market with parse_status="success" and a valid parsed dict.
    db_path  : Override the SQLite DB path (used in tests).

    Returns
    -------
    ModelResult on success, None if no source produced usable data.
    """
    parsed = market.parsed
    if not parsed:
        logger.warning("weather.compute: market %s has no parsed JSON — skipping", market.id)
        return None

    lat = parsed.get("lat")
    lon = parsed.get("lon")
    metric = parsed.get("metric")
    threshold = parsed.get("threshold")
    operator = parsed.get("operator", ">")
    window_start = parsed.get("window_start")

    if not all([lat is not None, lon is not None, metric, threshold is not None, window_start]):
        logger.warning(
            "weather.compute: market %s — incomplete parsed JSON (lat=%s lon=%s metric=%s "
            "threshold=%s window_start=%s)",
            market.id, lat, lon, metric, threshold, window_start,
        )
        return None

    lat = float(lat)
    lon = float(lon)
    threshold = float(threshold)
    threshold_high = parsed.get("threshold_high")
    if threshold_high is not None:
        threshold_high = float(threshold_high)

    # Clamp forecast_date: if window_start is in the past use today;
    # if beyond 15 days (Open-Meteo max) cap at 15 days out.
    from datetime import date as _date, timedelta as _td
    _today = _date.today()
    try:
        _ws = _date.fromisoformat(window_start)
        if _ws < _today:
            window_start = str(_today)
        elif _ws > _today + _td(days=15):
            window_start = str(_today + _td(days=15))
    except (ValueError, TypeError):
        window_start = str(_today)

    # --- Cache lookup (include threshold + threshold_high to avoid cross-market cache hits) ---
    cache_key = (round(lat, 2), round(lon, 2), metric, window_start, threshold, threshold_high)
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            cached_at, cached_result = entry
            if time.time() - cached_at < _CACHE_TTL_SECONDS:
                logger.debug(
                    "weather.compute: cache hit for (%s, %s, %s)",
                    lat, lon, metric,
                )
                return cached_result
            # Expired — remove stale entry
            del _cache[cache_key]

    om_variable = _METRIC_TO_OM_VARIABLE.get(metric)
    if om_variable is None:
        logger.warning(
            "weather.compute: unknown metric '%s' for market %s — cannot map to Open-Meteo",
            metric, market.id,
        )

    # --- Fetch Open-Meteo ensemble (raw cache shared across all threshold bins) ---
    open_meteo_ensemble = _fetch_open_meteo_cached(lat, lon, om_variable, window_start)

    # --- Fetch NOAA (US only, raw cache shared across all threshold bins) ---
    noaa_forecast = None
    if _is_us_coordinates(lat, lon):
        noaa_forecast = _fetch_noaa_cached(lat, lon)

    # --- Read ECMWF from DB ---
    ecmwf_value = _fetch_ecmwf(lat, lon, metric, db_path)

    sources_attempted = ["open_meteo"]
    if _is_us_coordinates(lat, lon):
        sources_attempted.append("noaa")
    sources_attempted.append("ecmwf")

    sources_succeeded = []
    if open_meteo_ensemble is not None:
        sources_succeeded.append("open_meteo")
    if noaa_forecast is not None:
        sources_succeeded.append("noaa")
    if ecmwf_value is not None:
        sources_succeeded.append("ecmwf")

    if not sources_succeeded:
        logger.error(
            "weather.compute: all sources failed for market %s (lat=%.4f lon=%.4f metric=%s) "
            "— attempted: %s",
            market.id, lat, lon, metric, sources_attempted,
        )
        return None

    if len(sources_succeeded) < len(sources_attempted):
        missing = set(sources_attempted) - set(sources_succeeded)
        logger.warning(
            "weather.compute: degraded sources for market %s — missing: %s",
            market.id, missing,
        )

    result = compute_probability(
        lat=lat,
        lon=lon,
        metric=metric,
        threshold=threshold,
        operator=operator,
        forecast_date=window_start,
        open_meteo_ensemble=open_meteo_ensemble,
        noaa_forecast=noaa_forecast,
        ecmwf_value=ecmwf_value,
        db_path=db_path,
        threshold_high=threshold_high,
    )

    # --- Cache successful results (never cache None) ---
    if result is not None:
        with _cache_lock:
            _cache[cache_key] = (time.time(), result)

    return result


# ---------------------------------------------------------------------------
# Internal fetch helpers — each returns None on failure
# ---------------------------------------------------------------------------


def _fetch_open_meteo_cached(
    lat: float, lon: float, variable: str | None, window_start: str
) -> dict | None:
    """Return Open-Meteo ensemble data, using a raw-data cache keyed by location+variable+date.

    All threshold bins for the same city/variable/date share a single API call.
    The cache TTL matches the result cache (30 minutes).
    """
    if variable is None:
        return None
    raw_key = (round(lat, 2), round(lon, 2), variable, window_start)
    with _raw_om_cache_lock:
        entry = _raw_om_cache.get(raw_key)
        if entry is not None:
            cached_at, cached_data = entry
            if time.time() - cached_at < _RAW_CACHE_TTL_SECONDS:
                logger.debug(
                    "_fetch_open_meteo_cached: raw cache hit for %s", raw_key
                )
                return cached_data
            del _raw_om_cache[raw_key]

    data = _fetch_open_meteo(lat, lon, variable)
    with _raw_om_cache_lock:
        _raw_om_cache[raw_key] = (time.time(), data)
    return data


def _fetch_noaa_cached(lat: float, lon: float) -> dict | None:
    """Return NOAA NWS forecast data, using a raw-data cache keyed by location.

    All threshold bins for the same city share a single NOAA API call.
    The cache TTL matches the result cache (30 minutes).
    """
    raw_key = (round(lat, 2), round(lon, 2))
    with _raw_noaa_cache_lock:
        entry = _raw_noaa_cache.get(raw_key)
        if entry is not None:
            cached_at, cached_data = entry
            if time.time() - cached_at < _RAW_CACHE_TTL_SECONDS:
                logger.debug(
                    "_fetch_noaa_cached: raw cache hit for %s", raw_key
                )
                return cached_data
            del _raw_noaa_cache[raw_key]

    data = _fetch_noaa(lat, lon)
    with _raw_noaa_cache_lock:
        _raw_noaa_cache[raw_key] = (time.time(), data)
    return data


def _fetch_open_meteo(lat: float, lon: float, variable: str | None) -> dict | None:
    """Fetch Open-Meteo ensemble data. Returns None on any error."""
    if variable is None:
        logger.debug("_fetch_open_meteo: no variable mapping — skipping")
        return None
    try:
        from data.sources.open_meteo import get_ensemble
        return get_ensemble(lat, lon, variables=[variable])
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fetch_open_meteo failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def _fetch_noaa(lat: float, lon: float) -> dict | None:
    """Fetch NOAA NWS forecast. Returns None on any error or non-US coords."""
    try:
        from data.sources.noaa import get_forecast
        return get_forecast(lat, lon)
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fetch_noaa failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def _fetch_ecmwf(lat: float, lon: float, metric: str, db_path: str | None) -> float | None:
    """Read the most recent ECMWF snapshot from the DB. Returns None on any error."""
    try:
        from data.sources.ecmwf import get_nearest_snapshot
        return get_nearest_snapshot(lat, lon, metric, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fetch_ecmwf failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def _is_us_coordinates(lat: float, lon: float) -> bool:
    """Return True if coordinates fall within the US CONUS bounding box."""
    return (
        _US_LAT_MIN <= lat <= _US_LAT_MAX
        and _US_LON_MIN <= lon <= _US_LON_MAX
    )
