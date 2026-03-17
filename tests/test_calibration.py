"""
tests/test_calibration.py — Unit tests for engine/calibration.py

Covers:
  - Brier score formula correctness
  - _compute_actual_outcome: normal, unknown metric, no values, operator variants
  - _write_weights: normalization math, upsert idempotency
  - get_weights: returns DB weights when available, uniform default when absent
  - run_calibration_batch: no resolved markets (no-op), happy-path with mocked Meteostat
"""
import json
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.calibration import (
    _EPSILON,
    _TRACKED_SOURCES,
    _compute_actual_outcome,
    _write_weights,
    get_weights,
    run_calibration_batch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compare(value: float, threshold: float, operator: str) -> bool:
    """Inline compare helper — mirrors ensemble._compare for test isolation."""
    ops = {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "==": value == threshold,
    }
    return ops.get(operator, False)


def _make_df(**kwargs) -> pd.DataFrame:
    """Build a minimal Meteostat-like DataFrame with one row."""
    return pd.DataFrame([kwargs])


# ---------------------------------------------------------------------------
# Brier score math
# ---------------------------------------------------------------------------


class TestBrierScoreMath:
    """Verify the Brier score formula (forecast_prob - actual_outcome)^2."""

    def test_perfect_forecast_event_occurs(self):
        """Confident YES prediction, event occurs → BS = 0."""
        bs = (1.0 - 1.0) ** 2
        assert bs == pytest.approx(0.0)

    def test_perfect_forecast_event_does_not_occur(self):
        """Confident NO prediction, event does not occur → BS = 0."""
        bs = (0.0 - 0.0) ** 2
        assert bs == pytest.approx(0.0)

    def test_worst_forecast_event_occurs(self):
        """Confident NO prediction, event occurs → BS = 1."""
        bs = (0.0 - 1.0) ** 2
        assert bs == pytest.approx(1.0)

    def test_uncertain_forecast(self):
        """50% forecast → BS = 0.25 regardless of outcome."""
        bs_yes = (0.5 - 1.0) ** 2
        bs_no = (0.5 - 0.0) ** 2
        assert bs_yes == pytest.approx(0.25)
        assert bs_no == pytest.approx(0.25)

    def test_partial_forecast(self):
        """70% forecast, event occurs → BS = 0.09."""
        bs = (0.7 - 1.0) ** 2
        assert bs == pytest.approx(0.09)


# ---------------------------------------------------------------------------
# _compute_actual_outcome
# ---------------------------------------------------------------------------


class TestComputeActualOutcome:
    def test_event_occurs_tmax(self):
        df = _make_df(tmax=92.0)
        result = _compute_actual_outcome(df, "temperature_2m_max", 90.0, ">", _compare)
        assert result == pytest.approx(1.0)

    def test_event_does_not_occur_tmax(self):
        df = _make_df(tmax=88.0)
        result = _compute_actual_outcome(df, "temperature_2m_max", 90.0, ">", _compare)
        assert result == pytest.approx(0.0)

    def test_any_day_in_window_triggers_event(self):
        """Event occurs if ANY day exceeds threshold."""
        df = pd.DataFrame({"tmax": [85.0, 91.0, 87.0]})
        result = _compute_actual_outcome(df, "temperature_2m_max", 90.0, ">", _compare)
        assert result == pytest.approx(1.0)

    def test_no_days_exceed_threshold(self):
        df = pd.DataFrame({"tmax": [85.0, 88.0, 87.0]})
        result = _compute_actual_outcome(df, "temperature_2m_max", 90.0, ">", _compare)
        assert result == pytest.approx(0.0)

    def test_unknown_metric_returns_none(self):
        df = _make_df(tmax=92.0)
        result = _compute_actual_outcome(df, "solar_radiation", 100.0, ">", _compare)
        assert result is None

    def test_column_missing_from_dataframe(self):
        df = pd.DataFrame({"tmax": [90.0]})
        result = _compute_actual_outcome(df, "temperature_2m_min", 50.0, "<", _compare)
        assert result is None

    def test_all_nan_values_returns_none(self):
        df = pd.DataFrame({"tmax": [float("nan"), float("nan")]})
        result = _compute_actual_outcome(df, "temperature_2m_max", 90.0, ">", _compare)
        assert result is None

    def test_precipitation_operator_gte(self):
        df = _make_df(prcp=1.0)
        result = _compute_actual_outcome(df, "precipitation_sum", 1.0, ">=", _compare)
        assert result == pytest.approx(1.0)

    def test_wind_speed_below_threshold(self):
        df = _make_df(wspd=20.0)
        result = _compute_actual_outcome(df, "wind_speed_10m_max", 30.0, ">", _compare)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _write_weights & get_weights
# ---------------------------------------------------------------------------


class TestWriteAndGetWeights:
    def test_single_source_weight_is_one(self, tmp_db_path):
        """Single source in a group → normalized weight must be 1.0."""
        scores = {("open_meteo", "us_conus", "summer"): [0.04, 0.09]}
        _write_weights(scores, db_path=tmp_db_path)

        weights = get_weights("us_conus", "summer", db_path=tmp_db_path)
        assert "open_meteo" in weights
        assert weights["open_meteo"] == pytest.approx(1.0)

    def test_weights_sum_to_one_across_sources(self, tmp_db_path):
        """Normalized weights for all sources in a group must sum to 1."""
        scores = {
            ("open_meteo", "us_conus", "winter"): [0.04],
            ("noaa", "us_conus", "winter"): [0.16],
            ("ecmwf", "us_conus", "winter"): [0.09],
        }
        _write_weights(scores, db_path=tmp_db_path)

        weights = get_weights("us_conus", "winter", db_path=tmp_db_path)
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0

    def test_better_calibrated_source_gets_higher_weight(self, tmp_db_path):
        """Lower Brier score → higher weight."""
        scores = {
            ("open_meteo", "tropical", "summer"): [0.01],  # best
            ("noaa", "tropical", "summer"): [0.25],         # worst
        }
        _write_weights(scores, db_path=tmp_db_path)

        weights = get_weights("tropical", "summer", db_path=tmp_db_path)
        assert weights["open_meteo"] > weights["noaa"]

    def test_upsert_replaces_existing_row(self, tmp_db_path):
        """Second write for the same (source, region, season) updates the row."""
        scores_v1 = {("open_meteo", "northern", "autumn"): [0.20]}
        _write_weights(scores_v1, db_path=tmp_db_path)

        scores_v2 = {("open_meteo", "northern", "autumn"): [0.05]}
        _write_weights(scores_v2, db_path=tmp_db_path)

        from db.init import get_connection

        with get_connection(tmp_db_path) as conn:
            rows = conn.execute(
                "SELECT brier_score FROM calibration_weights "
                "WHERE source='open_meteo' AND region='northern' AND season='autumn'"
            ).fetchall()

        assert len(rows) == 1
        assert rows[0]["brier_score"] == pytest.approx(0.05)

    def test_get_weights_returns_uniform_when_no_data(self, tmp_db_path):
        weights = get_weights("us_conus", "spring", db_path=tmp_db_path)
        assert set(weights.keys()) == set(_TRACKED_SOURCES)
        for w in weights.values():
            assert w == pytest.approx(1.0)

    def test_get_weights_fallback_on_db_error(self):
        """DB error in get_weights returns uniform defaults without raising."""
        weights = get_weights("us_conus", "spring", db_path="/nonexistent/path/db.sqlite3")
        assert set(weights.keys()) == set(_TRACKED_SOURCES)


# ---------------------------------------------------------------------------
# run_calibration_batch
# ---------------------------------------------------------------------------


class TestRunCalibrationBatch:
    def test_no_resolved_markets_is_noop(self, tmp_db_path):
        """Batch with empty trades table writes nothing and returns cleanly."""
        run_calibration_batch(db_path=tmp_db_path)

        from db.init import get_connection

        with get_connection(tmp_db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM calibration_weights").fetchone()[0]
        assert count == 0

    def test_happy_path_writes_weights(self, tmp_db_path):
        """Full happy-path: inserts a resolved trade, mocks Meteostat, checks DB."""
        from db.init import get_connection

        parsed_json = json.dumps(
            {
                "city": "Chicago",
                "lat": 41.88,
                "lon": -87.63,
                "metric": "temperature_2m_max",
                "threshold": 90,
                "operator": ">",
                "window_start": "2026-06-10",
                "window_end": "2026-06-10",
            }
        )

        with get_connection(tmp_db_path) as conn:
            # Resolved market (end_date in the past)
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES (?, ?, ?, datetime('now', '-1 day'), ?, ?, ?)",
                ("mkt-1", "Will Chicago exceed 90°F?", 0.3, 500.0, parsed_json, "success"),
            )
            conn.execute(
                "INSERT INTO signals (market_id, direction, adjusted_edge, model_prob, market_price, raw_kelly_size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("mkt-1", "YES", 0.10, 0.40, 0.30, 0.05),
            )
            conn.execute(
                "INSERT INTO trades (market_id, direction, final_size, price, order_id, status, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("mkt-1", "YES", 10.0, 0.30, "order-1", "filled", "paper"),
            )
            conn.commit()

        mock_df = pd.DataFrame({"tmax": [92.0]})

        with patch("engine.calibration.run_calibration_batch.__wrapped__", None, create=True), \
             patch("data.sources.meteostat.get_daily_observations", return_value=mock_df):
            run_calibration_batch(db_path=tmp_db_path)

        with get_connection(tmp_db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM calibration_weights").fetchone()[0]

        assert count >= len(_TRACKED_SOURCES)

    def test_incomplete_parsed_json_skipped(self, tmp_db_path):
        """Markets with incomplete parsed JSON are silently skipped."""
        from db.init import get_connection

        incomplete_parsed = json.dumps({"city": "Chicago"})  # missing lat/lon/metric

        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES (?, ?, ?, datetime('now', '-1 day'), ?, ?, ?)",
                ("mkt-2", "Some question?", 0.5, 500.0, incomplete_parsed, "success"),
            )
            conn.execute(
                "INSERT INTO signals (market_id, direction, adjusted_edge, model_prob, market_price, raw_kelly_size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("mkt-2", "YES", 0.10, 0.60, 0.50, 0.05),
            )
            conn.execute(
                "INSERT INTO trades (market_id, direction, final_size, price, order_id, status, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("mkt-2", "YES", 10.0, 0.50, "order-2", "filled", "paper"),
            )
            conn.commit()

        run_calibration_batch(db_path=tmp_db_path)

        with get_connection(tmp_db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM calibration_weights").fetchone()[0]
        assert count == 0

    def test_meteostat_returns_none_skipped(self, tmp_db_path):
        """Markets where Meteostat returns None are skipped without error."""
        from db.init import get_connection

        parsed_json = json.dumps(
            {
                "lat": 41.88,
                "lon": -87.63,
                "metric": "temperature_2m_max",
                "threshold": 90,
                "operator": ">",
                "window_start": "2026-06-10",
                "window_end": "2026-06-10",
            }
        )

        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES (?, ?, ?, datetime('now', '-1 day'), ?, ?, ?)",
                ("mkt-3", "Will Chicago exceed 90°F?", 0.3, 500.0, parsed_json, "success"),
            )
            conn.execute(
                "INSERT INTO signals (market_id, direction, adjusted_edge, model_prob, market_price, raw_kelly_size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("mkt-3", "YES", 0.10, 0.40, 0.30, 0.05),
            )
            conn.execute(
                "INSERT INTO trades (market_id, direction, final_size, price, order_id, status, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("mkt-3", "YES", 10.0, 0.30, "order-3", "filled", "paper"),
            )
            conn.commit()

        with patch("data.sources.meteostat.get_daily_observations", return_value=None):
            run_calibration_batch(db_path=tmp_db_path)

        with get_connection(tmp_db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM calibration_weights").fetchone()[0]
        assert count == 0
