"""
data/sources/open_meteo.py — Open-Meteo API connector.

Covers all four endpoints:
  - Forecast API       (16-day hourly, global)
  - Ensemble API       (50–100 perturbed members, core probability engine)
  - Historical API     (hourly since 1940, model calibration)
  - Climate API        (long-run normals, anomaly context)

All calls:
  - Rate-limited via RateLimiter
  - Cached via CacheManager with per-endpoint TTLs
  - Retried 3× with exponential backoff (1–10s) via tenacity
  - 10s request timeout

Returns typed dicts on success, None on exhausted retries or rate limit hit.
Source is marked in the returned dict under the key "source".
"""
import logging
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings
from data.cache.manager import CacheManager
from data.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_BASE_FORECAST  = "https://api.open-meteo.com/v1/forecast"
_BASE_ENSEMBLE  = "https://ensemble-api.open-meteo.com/v1/ensemble"
_BASE_HISTORICAL = "https://archive-api.open-meteo.com/v1/archive"
_BASE_CLIMATE   = "https://climate-api.open-meteo.com/v1/climate"

_SOURCE = "open_meteo"

# Shared singletons — instantiated once per process
_cache = CacheManager()
_limiter = RateLimiter()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _get(url: str, params: dict) -> dict:
    """Perform a single GET with timeout. Retried by caller decorator."""
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _cache_key(endpoint: str, lat: float, lon: float, **extra) -> str:
    parts = [endpoint, f"{lat:.4f}", f"{lon:.4f}"]
    for k, v in sorted(extra.items()):
        parts.append(f"{k}={v}")
    return ":".join(parts)


def get_forecast(lat: float, lon: float) -> dict | None:
    """
    Fetch the standard 16-day hourly forecast for a lat/lon.

    Returns a dict with key 'hourly' containing temperature_2m, precipitation, etc.
    Cached for 1 hour.
    """
    if not _limiter.check_and_record(_SOURCE):
        logger.warning("open_meteo rate limit hit — skipping forecast for %.4f,%.4f", lat, lon)
        return None

    key = _cache_key("forecast", lat, lon)
    cached = _cache.get(_SOURCE, key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,windspeed_10m",
        "forecast_days": 16,
        "timezone": "UTC",
    }

    try:
        data = _get(_BASE_FORECAST, params)
        data["source"] = _SOURCE + "_forecast"
        _cache.set(_SOURCE, key, data, ttl_seconds=settings.CACHE_TTL_OPEN_METEO_ENSEMBLE)
        return data
    except Exception as exc:
        logger.error("open_meteo forecast failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def get_ensemble(
    lat: float,
    lon: float,
    variables: list[str] | None = None,
) -> dict | None:
    """
    Fetch ensemble model data (50–100 members: ECMWF ENS, GFS ENS, ICON ENS).

    Returns a dict containing per-member hourly forecasts for each variable.
    Cached for 1 hour.

    Default variables: temperature_2m, precipitation
    """
    if not _limiter.check_and_record(_SOURCE):
        logger.warning("open_meteo rate limit hit — skipping ensemble for %.4f,%.4f", lat, lon)
        return None

    vars_str = ",".join(variables or ["temperature_2m", "precipitation"])
    key = _cache_key("ensemble", lat, lon, vars=vars_str)
    cached = _cache.get(_SOURCE, key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": vars_str,
        "models": "ecmwf_ens,gfs_ensemble_mean_35,icon_seamless",
        "forecast_days": 16,
        "timezone": "UTC",
    }

    try:
        data = _get(_BASE_ENSEMBLE, params)
        data["source"] = _SOURCE + "_ensemble"
        _cache.set(_SOURCE, key, data, ttl_seconds=settings.CACHE_TTL_OPEN_METEO_ENSEMBLE)
        return data
    except Exception as exc:
        logger.error("open_meteo ensemble failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def get_historical(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    variables: list[str] | None = None,
) -> dict | None:
    """
    Fetch hourly historical weather data.

    start_date / end_date: ISO 8601 date strings (YYYY-MM-DD).
    Cached for 24 hours (historical data doesn't change).
    """
    if not _limiter.check_and_record(_SOURCE):
        logger.warning("open_meteo rate limit hit — skipping historical for %.4f,%.4f", lat, lon)
        return None

    vars_str = ",".join(variables or ["temperature_2m", "precipitation"])
    key = _cache_key("historical", lat, lon, start=start_date, end=end_date, vars=vars_str)
    cached = _cache.get(_SOURCE, key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": vars_str,
        "timezone": "UTC",
    }

    try:
        data = _get(_BASE_HISTORICAL, params)
        data["source"] = _SOURCE + "_historical"
        _cache.set(_SOURCE, key, data, ttl_seconds=settings.CACHE_TTL_OPEN_METEO_HISTORICAL)
        return data
    except Exception as exc:
        logger.error("open_meteo historical failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def get_climate(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
) -> dict | None:
    """
    Fetch long-run climate normals for anomaly detection context.

    start_date / end_date: ISO 8601 date strings (YYYY-MM-DD).
    Cached for 24 hours.
    """
    if not _limiter.check_and_record(_SOURCE):
        logger.warning("open_meteo rate limit hit — skipping climate for %.4f,%.4f", lat, lon)
        return None

    key = _cache_key("climate", lat, lon, start=start_date, end=end_date)
    cached = _cache.get(_SOURCE, key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "models": "EC_Earth3P_HR",
        "timezone": "UTC",
    }

    try:
        data = _get(_BASE_CLIMATE, params)
        data["source"] = _SOURCE + "_climate"
        _cache.set(_SOURCE, key, data, ttl_seconds=settings.CACHE_TTL_OPEN_METEO_HISTORICAL)
        return data
    except Exception as exc:
        logger.error("open_meteo climate failed for %.4f,%.4f: %s", lat, lon, exc)
        return None
