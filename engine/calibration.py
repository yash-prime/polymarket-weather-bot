"""
engine/calibration.py — Daily Brier-score calibration batch job.

Registered in main.py APScheduler to run at 02:00 UTC daily.

Algorithm
---------
For each resolved market (end_date in the past, trade filled):
  1. Parse the market's stored JSON to extract location, metric, threshold.
  2. Fetch actual daily observations from Meteostat for the event window.
  3. Determine whether the event occurred (actual_outcome ∈ {0.0, 1.0}).
  4. Retrieve the model's forecast probability from the signals table.
  5. Compute Brier Score: BS = (forecast_prob - actual_outcome)²
  6. Aggregate scores by source × region × season.
  7. Convert to weight = 1 / (avg_BS + epsilon), normalize to sum = 1.
  8. Upsert into calibration_weights table.

get_weights(region, season) is the public API used by ensemble.py to load
per-source weights at probability-computation time.
"""
import json
import logging
from collections import defaultdict

import numpy as np

logger = logging.getLogger(__name__)

# Avoid division-by-zero for a perfectly calibrated source (BS = 0)
_EPSILON: float = 1e-4

# Sources tracked in the calibration system
_TRACKED_SOURCES = ("open_meteo", "noaa", "ecmwf")

# Map canonical metric names (from LLM parser) → Meteostat DataFrame columns
_METRIC_TO_COLUMN: dict[str, str] = {
    "temperature_2m_max": "tmax",
    "temperature_2m_min": "tmin",
    "precipitation_sum": "prcp",
    "wind_speed_10m_max": "wspd",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_calibration_batch(db_path: str | None = None) -> None:
    """
    Main entry point for the daily calibration job.

    Reads resolved, filled trades; computes Brier scores per
    source × region × season; writes updated weights to DB.
    """
    from data.sources.meteostat import get_daily_observations
    from db.init import get_connection
    from engine.ensemble import _compare, _date_to_season, _lat_lon_to_region

    logger.info("Calibration batch started")

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                t.market_id,
                m.parsed,
                m.end_date,
                s.model_prob
            FROM trades t
            JOIN markets  m ON m.id        = t.market_id
            JOIN signals  s ON s.market_id = t.market_id
            WHERE t.status       IN ('filled', 'open')
              AND m.end_date      < datetime('now')
              AND m.parsed        IS NOT NULL
              AND m.parse_status  = 'success'
            """
        ).fetchall()

    if not rows:
        logger.info("Calibration batch: no resolved markets to process")
        return

    logger.info("Calibration batch: processing %d resolved market(s)", len(rows))

    # {(source, region, season): [brier_score, ...]}
    scores: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    for row in rows:
        try:
            parsed = json.loads(row["parsed"])
            lat = parsed.get("lat")
            lon = parsed.get("lon")
            metric = parsed.get("metric")
            threshold = parsed.get("threshold")
            operator = parsed.get("operator", ">")
            window_start = parsed.get("window_start")
            window_end = parsed.get("window_end") or row["end_date"][:10]

            if not all([lat is not None, lon is not None, metric, threshold is not None, window_start]):
                logger.debug(
                    "Calibration: skipping market %s — incomplete parsed JSON", row["market_id"]
                )
                continue

            df = get_daily_observations(float(lat), float(lon), window_start, window_end)
            if df is None or df.empty:
                logger.debug(
                    "Calibration: no Meteostat data for market %s", row["market_id"]
                )
                continue

            actual_outcome = _compute_actual_outcome(df, metric, float(threshold), operator, _compare)
            if actual_outcome is None:
                logger.debug(
                    "Calibration: cannot determine outcome for market %s metric=%s",
                    row["market_id"],
                    metric,
                )
                continue

            forecast_prob = float(row["model_prob"])
            brier = (forecast_prob - actual_outcome) ** 2

            region = _lat_lon_to_region(float(lat), float(lon))
            season = _date_to_season(window_start)

            # Attribute the combined model score to each tracked source.
            # Once per-source predictions are stored separately this can be refined.
            for source in _TRACKED_SOURCES:
                scores[(source, region, season)].append(brier)

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Calibration: error processing market %s — %s", row["market_id"], exc
            )
            continue

    if not scores:
        logger.info("Calibration batch: no valid scores computed")
        return

    _write_weights(scores, db_path)
    logger.info(
        "Calibration batch complete — %d source×region×season combinations updated",
        len(scores),
    )


def get_weights(region: str, season: str, db_path: str | None = None) -> dict[str, float]:
    """
    Return calibration weights for the given region and season.

    Returns uniform default weights if no calibration data has been written yet.
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

    except Exception as exc:  # noqa: BLE001
        logger.warning("get_weights: could not load from DB — %s. Using uniform.", exc)

    return {s: 1.0 for s in _TRACKED_SOURCES}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_actual_outcome(
    df,
    metric: str,
    threshold: float,
    operator: str,
    compare_fn,
) -> float | None:
    """
    Determine whether the weather event occurred (1.0) or did not (0.0).

    Uses Meteostat DataFrame columns mapped from the canonical metric name.
    Returns None when the metric is unknown or the column has no data.

    The event is considered to have occurred if ANY day in the window
    satisfies the threshold condition (consistent with Polymarket "any day"
    resolution conventions).
    """
    col = _METRIC_TO_COLUMN.get(metric)
    if col is None:
        logger.debug("_compute_actual_outcome: unmapped metric '%s'", metric)
        return None

    if col not in df.columns:
        logger.debug("_compute_actual_outcome: column '%s' not in DataFrame", col)
        return None

    values = df[col].dropna().tolist()
    if not values:
        return None

    occurred = any(compare_fn(float(v), threshold, operator) for v in values)
    return 1.0 if occurred else 0.0


def _write_weights(
    scores: dict[tuple[str, str, str], list[float]],
    db_path: str | None = None,
) -> None:
    """
    Convert raw Brier score lists to normalized weights and upsert to DB.

    Weight derivation per source within each (region, season) group:
      avg_BS    = mean(brier_scores)
      raw_w     = 1 / (avg_BS + EPSILON)   — higher = better calibrated
      weight    = raw_w / sum(raw_w_all_sources)
    """
    from db.init import get_connection

    # Group by (region, season) to compute normalized weights within each group
    # {(region, season): {source: [brier_scores]}}
    grouped: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (source, region, season), brier_list in scores.items():
        grouped[(region, season)][source].extend(brier_list)

    rows_to_upsert: list[tuple] = []
    for (region, season), source_scores in grouped.items():
        avg_briers = {s: float(np.mean(bs)) for s, bs in source_scores.items()}
        raw_weights = {s: 1.0 / (avg_bs + _EPSILON) for s, avg_bs in avg_briers.items()}
        total = sum(raw_weights.values())
        normalized = {s: w / total for s, w in raw_weights.items()}

        for source, weight in normalized.items():
            rows_to_upsert.append(
                (source, region, season, avg_briers[source], weight)
            )

    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO calibration_weights (source, region, season, brier_score, weight, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(source, region, season) DO UPDATE SET
                brier_score = excluded.brier_score,
                weight      = excluded.weight,
                updated_at  = excluded.updated_at
            """,
            rows_to_upsert,
        )
        conn.commit()

    logger.info("_write_weights: upserted %d row(s)", len(rows_to_upsert))
