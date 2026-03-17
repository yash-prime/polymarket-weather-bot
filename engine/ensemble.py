"""
engine/ensemble.py — Multi-model ensemble aggregation.

Combines Open-Meteo ensemble members, NOAA forecast, and ECMWF DB snapshots
into a single weighted probability estimate. Weights are loaded from the
calibration_weights DB table (uniform default if no calibration data exists).

The core function is compute_probability(), which:
  1. Collects ensemble member forecasts from available sources
  2. Converts raw values to binary outcomes (did event occur per member?)
  3. Weights sources by Brier-score calibration
  4. Returns a ModelResult with probability, confidence, CI, member count

Confidence derivation (as specified in ARCHITECTURE.md):
    confidence = max(0.0, min(1.0, 1.0 - (std_dev / 0.5)))
    where std_dev = standard deviation of member probabilities across sources
"""
import logging
from typing import Any

import numpy as np

from engine.models import ModelResult

logger = logging.getLogger(__name__)

# Epsilon to avoid division-by-zero in weight normalization
_WEIGHT_EPSILON = 1e-6


def compute_probability(
    lat: float,
    lon: float,
    metric: str,
    threshold: float,
    operator: str,
    forecast_date: str,
    open_meteo_ensemble: dict | None = None,
    noaa_forecast: dict | None = None,
    ecmwf_value: float | None = None,
    db_path: str | None = None,
) -> ModelResult | None:
    """
    Aggregate multi-source ensemble into a single ModelResult.

    Parameters
    ----------
    lat, lon        : Location
    metric          : Canonical metric name (e.g. 'temperature_2m_max')
    threshold       : Numeric threshold for the event (e.g. 90.0)
    operator        : Comparison operator: ">" | ">=" | "<" | "<=" | "=="
    forecast_date   : Target date (YYYY-MM-DD)
    open_meteo_ensemble : Raw response from open_meteo.get_ensemble()
    noaa_forecast   : Raw response from noaa.get_forecast()
    ecmwf_value     : Single float from ecmwf.get_nearest_snapshot()
    db_path         : Override DB path (for tests)

    Returns
    -------
    ModelResult or None if no sources produced usable data.
    """
    region = _lat_lon_to_region(lat, lon)
    season = _date_to_season(forecast_date)
    weights = _load_weights(region, season, db_path)

    member_probs: list[float] = []
    sources_used: list[str] = []
    degraded: list[str] = []

    # --- Open-Meteo ensemble members ---
    if open_meteo_ensemble is not None:
        members = _extract_open_meteo_members(open_meteo_ensemble, metric, forecast_date)
        if members:
            probs = _members_to_probs(members, threshold, operator)
            w = weights.get("open_meteo", 1.0)
            member_probs.extend([p * w for p in probs])
            sources_used.append("open_meteo_ensemble")
            logger.debug("Open-Meteo ensemble: %d members, raw_prob=%.3f", len(probs), np.mean(probs))
        else:
            degraded.append("open_meteo_ensemble")
    else:
        degraded.append("open_meteo_ensemble")

    # --- NOAA single-model (treated as 1 member) ---
    if noaa_forecast is not None:
        noaa_prob = _extract_noaa_prob(noaa_forecast, metric, threshold, operator, forecast_date)
        if noaa_prob is not None:
            w = weights.get("noaa", 1.0)
            member_probs.append(noaa_prob * w)
            sources_used.append("noaa")
        else:
            degraded.append("noaa")
    else:
        degraded.append("noaa")

    # --- ECMWF snapshot (single value → deterministic, treat as 1 member) ---
    if ecmwf_value is not None:
        ecmwf_prob = 1.0 if _compare(ecmwf_value, threshold, operator) else 0.0
        w = weights.get("ecmwf", 1.0)
        member_probs.append(ecmwf_prob * w)
        sources_used.append("ecmwf")
    else:
        degraded.append("ecmwf")

    if not member_probs:
        logger.warning(
            "All sources failed for lat=%.4f lon=%.4f metric=%s — cannot compute probability",
            lat, lon, metric,
        )
        return None

    arr = np.array(member_probs, dtype=float)

    # Normalize weighted probabilities back to [0, 1] range
    total_weight = sum(
        weights.get(s.replace("_ensemble", "").replace("_forecast", ""), 1.0)
        for s in sources_used
    )
    if total_weight > 0:
        arr = arr / (total_weight / len(sources_used))

    # Clamp to valid probability range
    arr = np.clip(arr, 0.0, 1.0)

    probability = float(np.mean(arr))
    std_dev = float(np.std(arr))

    # Confidence: 1 when all members agree, 0 when maximum disagreement
    confidence = float(max(0.0, min(1.0, 1.0 - (std_dev / 0.5))))

    # 90% confidence interval (5th–95th percentile)
    ci_low = float(np.percentile(arr, 5))
    ci_high = float(np.percentile(arr, 95))

    return ModelResult(
        probability=probability,
        confidence=confidence,
        ci_low=ci_low,
        ci_high=ci_high,
        members_count=len(arr),
        sources=sources_used,
        degraded_sources=degraded,
    )


def _compare(value: float, threshold: float, operator: str) -> bool:
    ops = {">": value > threshold, ">=": value >= threshold,
           "<": value < threshold, "<=": value <= threshold,
           "==": value == threshold}
    return ops.get(operator, False)


def _members_to_probs(members: list[float], threshold: float, operator: str) -> list[float]:
    """Convert a list of raw member values to binary outcomes (0.0 or 1.0)."""
    return [1.0 if _compare(v, threshold, operator) else 0.0 for v in members]


def _extract_open_meteo_members(
    data: dict, metric: str, forecast_date: str
) -> list[float]:
    """
    Extract per-member values for a given metric and date from Open-Meteo ensemble response.

    Open-Meteo ensemble response structure:
    {
      "hourly": {
        "time": [...],
        "temperature_2m_member01": [...],
        "temperature_2m_member02": [...],
        ...
      }
    }
    """
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # Find indices matching forecast_date
    date_indices = [i for i, t in enumerate(times) if t.startswith(forecast_date)]
    if not date_indices:
        logger.warning("No Open-Meteo data for forecast_date=%s", forecast_date)
        return []

    # Determine the base variable name in the response.
    # The metric may be "temperature_2m_max" while the response has
    # "temperature_2m" / "temperature_2m_member01" (Open-Meteo stores hourly
    # values without the _max/_min/_sum suffix).
    _suffixes = ("_max", "_min", "_sum", "_mean")
    base_metric = metric
    for _suf in _suffixes:
        if metric.endswith(_suf):
            base_metric = metric[: -len(_suf)]
            break

    # Collect all member keys matching the metric or its base form
    member_keys = [k for k in hourly if k.startswith(metric) and "member" in k]
    if not member_keys:
        member_keys = [k for k in hourly if k.startswith(base_metric) and "member" in k]

    if not member_keys:
        # Fallback: non-ensemble key (mean forecast) — try exact metric then base
        key = metric if metric in hourly else (base_metric if base_metric in hourly else None)
        if key:
            vals = [hourly[key][i] for i in date_indices if hourly[key][i] is not None]
            return vals
        return []

    max_vals: list[float] = []
    for key in member_keys:
        vals_for_date = [
            hourly[key][i] for i in date_indices
            if i < len(hourly[key]) and hourly[key][i] is not None
        ]
        if vals_for_date:
            max_vals.append(max(vals_for_date))  # daily max across hourly readings

    return max_vals


def _extract_noaa_prob(
    data: dict, metric: str, threshold: float, operator: str, forecast_date: str
) -> float | None:
    """
    Extract a single probability estimate from NOAA NWS forecast.

    NWS returns qualitative forecasts ("High near 88") — we extract the
    numeric value and compute a deterministic probability (0.0 or 1.0).
    For temperature_2m_max, we look at the daytime high for the target date.
    """
    periods = data.get("properties", {}).get("periods", [])
    for period in periods:
        start_time = period.get("startTime", "")
        if not start_time.startswith(forecast_date):
            continue
        if not period.get("isDaytime", True):
            continue

        temp = period.get("temperature")
        if temp is None:
            continue

        unit = period.get("temperatureUnit", "F")
        if unit == "C":
            # Convert to F for consistency with most US Polymarket questions
            temp = temp * 9 / 5 + 32

        return 1.0 if _compare(float(temp), threshold, operator) else 0.0

    return None


def _lat_lon_to_region(lat: float, lon: float) -> str:
    """Map coordinates to a broad calibration region."""
    if 24 <= lat <= 50 and -125 <= lon <= -65:
        return "us_conus"
    if lat > 50:
        return "northern"
    if lat < 0:
        return "southern_hemisphere"
    return "tropical"


def _date_to_season(date_str: str) -> str:
    """Map a YYYY-MM-DD date to a meteorological season (Northern Hemisphere)."""
    try:
        month = int(date_str[5:7])
    except (ValueError, IndexError):
        return "annual"
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _load_weights(region: str, season: str, db_path: str | None = None) -> dict[str, float]:
    """
    Load Brier-score calibration weights for the given region/season.

    Returns uniform weights if no calibration data exists yet.
    """
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT source, weight FROM calibration_weights "
                "WHERE region = ? AND season = ?",
                (region, season),
            ).fetchall()

        if rows:
            return {row["source"]: row["weight"] for row in rows}

    except Exception as exc:
        logger.warning("Could not load calibration weights: %s — using uniform", exc)

    # Default: uniform weights
    return {"open_meteo": 1.0, "noaa": 1.0, "ecmwf": 1.0}
