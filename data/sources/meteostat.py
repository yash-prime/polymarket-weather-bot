"""
data/sources/meteostat.py — Historical weather station data via the meteostat library.

Used exclusively by engine/calibration.py to fetch actual weather outcomes for
resolved markets (ground truth for Brier score computation).

NOT used in the live scan loop — calibration runs once daily at 02:00 UTC.

Returns a pandas DataFrame with columns: date, tmax, tmin, prcp, wspd.
Cached for 24 hours per (lat, lon, start_date, end_date) key.
"""
import logging

import pandas as pd

from data.cache.manager import CacheManager

logger = logging.getLogger(__name__)

_SOURCE = "meteostat"
_CACHE = CacheManager()


def get_daily_observations(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
) -> pd.DataFrame | None:
    """
    Fetch daily weather observations for the nearest station to (lat, lon).

    Parameters
    ----------
    lat, lon      : Decimal degrees
    start_date    : ISO 8601 date string (YYYY-MM-DD)
    end_date      : ISO 8601 date string (YYYY-MM-DD)

    Returns
    -------
    pd.DataFrame with columns: date, tmax, tmin, prcp, wspd
    Returns None if the meteostat library is unavailable or no data is found.

    Notes
    -----
    The meteostat library searches for the nearest station within 50 km by default.
    If no station is found, returns None.
    """
    cache_key = f"{lat:.4f}:{lon:.4f}:{start_date}:{end_date}"
    cached = _CACHE.get(_SOURCE, cache_key)
    if cached is not None:
        logger.debug("meteostat cache HIT: %s", cache_key)
        return pd.DataFrame(cached)

    try:
        from datetime import datetime
        from meteostat import Daily, Point  # type: ignore[import]

        location = Point(lat, lon)
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)

        data = Daily(location, start, end)
        df = data.fetch()

        if df.empty:
            logger.warning(
                "meteostat: no data found near lat=%.4f lon=%.4f %s–%s",
                lat,
                lon,
                start_date,
                end_date,
            )
            return None

        # Normalise column names to our canonical schema
        df = df.reset_index()
        rename_map = {
            "time": "date",
            "tmax": "tmax",
            "tmin": "tmin",
            "prcp": "prcp",
            "wspd": "wspd",
        }
        df = df.rename(columns=rename_map)
        df = df[[c for c in ["date", "tmax", "tmin", "prcp", "wspd"] if c in df.columns]]

        # Cache as records list (JSON-serialisable)
        _CACHE.set(_SOURCE, cache_key, df.to_dict("records"), ttl_seconds=86400)

        logger.debug(
            "meteostat fetched %d rows for lat=%.4f lon=%.4f %s–%s",
            len(df),
            lat,
            lon,
            start_date,
            end_date,
        )
        return df

    except ImportError:
        logger.warning(
            "meteostat library not installed — historical observations unavailable. "
            "Install with: pip install meteostat"
        )
        return None
    except Exception as exc:
        logger.error(
            "meteostat fetch failed for lat=%.4f lon=%.4f: %s", lat, lon, exc
        )
        return None
