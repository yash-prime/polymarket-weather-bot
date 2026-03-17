"""
data/sources/noaa.py — NOAA National Weather Service (NWS) API connector.

Provides US official forecasts and active weather alerts.
Only covers US lat/lon (NWS is US-only).

All calls:
  - Rate-limited via RateLimiter
  - Cached via CacheManager (TTL=1h)
  - Retried 3× with exponential backoff (1–10s) via tenacity
  - 10s request timeout
  - HTTP 503 returns None with a warning (NWS maintenance windows are common)

Returns typed dicts on success, None on exhausted retries, rate limit, or
out-of-range coordinates.
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

_BASE = "https://api.weather.gov"
_SOURCE = "noaa"
_HEADERS = {"User-Agent": "polymarket-weather-bot/1.0 (contact: bot@example.com)"}

_cache = CacheManager()
_limiter = RateLimiter()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _get(url: str, params: dict | None = None) -> dict | None:
    """
    Perform a GET with timeout.

    Returns None (with warning) on HTTP 503 — NWS maintenance.
    Raises for all other 4xx/5xx responses.
    """
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
    if resp.status_code == 503:
        logger.warning("NOAA NWS returned 503 (maintenance) for %s", url)
        return None
    resp.raise_for_status()
    return resp.json()


def _cache_key(endpoint: str, lat: float, lon: float) -> str:
    return f"{endpoint}:{lat:.4f}:{lon:.4f}"


def get_forecast(lat: float, lon: float) -> dict | None:
    """
    Fetch the NWS gridpoint forecast for a US lat/lon.

    Two-step NWS process:
      1. GET /points/{lat},{lon}  → resolves to grid office + gridX/gridY
      2. GET /gridpoints/{office}/{gridX},{gridY}/forecast

    Returns a dict with key 'properties.periods' containing forecast periods,
    plus 'source': 'noaa_forecast'. Returns None for non-US coordinates or
    any API failure.
    """
    if not _limiter.check_and_record(_SOURCE):
        logger.warning("NOAA rate limit hit — skipping forecast for %.4f,%.4f", lat, lon)
        return None

    key = _cache_key("forecast", lat, lon)
    cached = _cache.get(_SOURCE, key)
    if cached is not None:
        return cached

    try:
        # Step 1: resolve lat/lon to NWS grid
        points_data = _get(f"{_BASE}/points/{lat:.4f},{lon:.4f}")
        if points_data is None:
            return None

        props = points_data.get("properties", {})
        office = props.get("gridId")
        grid_x = props.get("gridX")
        grid_y = props.get("gridY")

        if not all([office, grid_x is not None, grid_y is not None]):
            logger.warning(
                "NOAA points response missing grid info for %.4f,%.4f", lat, lon
            )
            return None

        # Step 2: fetch the actual forecast
        forecast_data = _get(
            f"{_BASE}/gridpoints/{office}/{grid_x},{grid_y}/forecast"
        )
        if forecast_data is None:
            return None

        forecast_data["source"] = "noaa_forecast"
        _cache.set(_SOURCE, key, forecast_data, ttl_seconds=settings.CACHE_TTL_NOAA)
        return forecast_data

    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            # 404 = coordinates outside NWS coverage (non-US)
            logger.debug("NOAA 404 for %.4f,%.4f — likely non-US coordinates", lat, lon)
        else:
            logger.error("NOAA forecast failed for %.4f,%.4f: %s", lat, lon, exc)
        return None
    except Exception as exc:
        logger.error("NOAA forecast failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


def get_alerts(lat: float, lon: float) -> dict | None:
    """
    Fetch active weather alerts for a US lat/lon.

    Returns a dict with key 'features' (list of alert GeoJSON features),
    plus 'source': 'noaa_alerts'. Returns None for non-US coordinates or
    API failure.
    """
    if not _limiter.check_and_record(_SOURCE):
        logger.warning("NOAA rate limit hit — skipping alerts for %.4f,%.4f", lat, lon)
        return None

    key = _cache_key("alerts", lat, lon)
    cached = _cache.get(_SOURCE, key)
    if cached is not None:
        return cached

    try:
        data = _get(f"{_BASE}/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"})
        if data is None:
            return None

        data["source"] = "noaa_alerts"
        _cache.set(_SOURCE, key, data, ttl_seconds=settings.CACHE_TTL_NOAA)
        return data

    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            logger.debug("NOAA alerts 404 for %.4f,%.4f — non-US coordinates", lat, lon)
        else:
            logger.error("NOAA alerts failed for %.4f,%.4f: %s", lat, lon, exc)
        return None
    except Exception as exc:
        logger.error("NOAA alerts failed for %.4f,%.4f: %s", lat, lon, exc)
        return None
