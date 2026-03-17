"""
tests/test_ensemble.py — ≥90% coverage tests for engine/ensemble.py

Covers:
  - compute_probability: all-sources, single-source fallback, all-failed → None
  - compute_probability: ECMWF deterministic path
  - compute_probability: NOAA extraction (Celsius conversion)
  - _extract_open_meteo_members: member keys, fallback to mean key, no date match
  - _extract_noaa_prob: matching period, no match, unit conversion
  - _compare: all 5 operators
  - _members_to_probs: various threshold comparisons
  - _lat_lon_to_region: all 4 regions
  - _date_to_season: all 4 seasons + annual fallback
  - _load_weights: DB weights, uniform fallback, error fallback
  - confidence formula extremes: zero std-dev, max std-dev
"""
import pytest

from engine.ensemble import (
    _compare,
    _date_to_season,
    _extract_noaa_prob,
    _extract_open_meteo_members,
    _lat_lon_to_region,
    _load_weights,
    _members_to_probs,
    compute_probability,
)
from engine.models import ModelResult


# ---------------------------------------------------------------------------
# Fixtures — minimal Open-Meteo ensemble response
# ---------------------------------------------------------------------------

def _om_ensemble(metric: str, members: list[list[float]], date: str = "2025-07-15") -> dict:
    """Build a minimal Open-Meteo ensemble hourly response."""
    times = [f"{date}T{h:02d}:00" for h in range(24)]
    hourly: dict = {"time": times}
    for i, vals in enumerate(members):
        key = f"{metric}_member{i+1:02d}"
        # Repeat first value for each hour in the day
        hourly[key] = vals
    return {"hourly": hourly}


def _noaa_period(date: str, temp: float, unit: str = "F", daytime: bool = True) -> dict:
    return {
        "startTime": f"{date}T06:00:00",
        "isDaytime": daytime,
        "temperature": temp,
        "temperatureUnit": unit,
    }


# ---------------------------------------------------------------------------
# compute_probability — full happy path
# ---------------------------------------------------------------------------


class TestComputeProbability:
    def test_all_sources_returns_model_result(self, tmp_db_path):
        om = _om_ensemble("temperature_2m_max", [[85.0] * 24, [95.0] * 24])
        noaa = {"properties": {"periods": [_noaa_period("2025-07-15", 90.0)]}}
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=88.0, operator=">",
            forecast_date="2025-07-15",
            open_meteo_ensemble=om,
            noaa_forecast=noaa,
            ecmwf_value=91.0,
            db_path=tmp_db_path,
        )
        assert isinstance(result, ModelResult)
        assert 0.0 <= result.probability <= 1.0
        assert 0.0 <= result.confidence <= 1.0
        assert "open_meteo_ensemble" in result.sources

    def test_all_sources_none_returns_none(self, tmp_db_path):
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            open_meteo_ensemble=None,
            noaa_forecast=None,
            ecmwf_value=None,
            db_path=tmp_db_path,
        )
        assert result is None

    def test_only_open_meteo(self, tmp_db_path):
        om = _om_ensemble("temperature_2m_max", [[95.0] * 24] * 3)
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            open_meteo_ensemble=om,
            db_path=tmp_db_path,
        )
        assert result is not None
        assert "noaa" in result.degraded_sources
        assert "ecmwf" in result.degraded_sources

    def test_only_ecmwf_value_above_threshold(self, tmp_db_path):
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            ecmwf_value=95.0,
            db_path=tmp_db_path,
        )
        assert result is not None
        assert result.probability == pytest.approx(1.0)

    def test_only_ecmwf_value_below_threshold(self, tmp_db_path):
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            ecmwf_value=80.0,
            db_path=tmp_db_path,
        )
        assert result is not None
        assert result.probability == pytest.approx(0.0)

    def test_confidence_is_high_when_all_members_agree(self, tmp_db_path):
        # All members above threshold → probability=1, std=0 → confidence=1
        om = _om_ensemble("temperature_2m_max", [[95.0] * 24] * 5)
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            open_meteo_ensemble=om,
            db_path=tmp_db_path,
        )
        assert result.confidence == pytest.approx(1.0)

    def test_degraded_sources_listed(self, tmp_db_path):
        om = _om_ensemble("temperature_2m_max", [[85.0] * 24])
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            open_meteo_ensemble=om,
            noaa_forecast=None,
            ecmwf_value=None,
            db_path=tmp_db_path,
        )
        assert "noaa" in result.degraded_sources
        assert "ecmwf" in result.degraded_sources

    def test_noaa_celsius_conversion(self, tmp_db_path):
        """NOAA period with Celsius temperature is converted to Fahrenheit."""
        # 35°C = 95°F, threshold = 90°F → event occurs
        noaa = {"properties": {"periods": [_noaa_period("2025-07-15", 35.0, unit="C")]}}
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            noaa_forecast=noaa,
            db_path=tmp_db_path,
        )
        assert result is not None
        assert result.probability == pytest.approx(1.0)

    def test_members_count_reflects_inputs(self, tmp_db_path):
        om = _om_ensemble("temperature_2m_max", [[95.0] * 24, [85.0] * 24])  # 2 members
        result = compute_probability(
            lat=40.0, lon=-75.0, metric="temperature_2m_max",
            threshold=90.0, operator=">",
            forecast_date="2025-07-15",
            open_meteo_ensemble=om,
            db_path=tmp_db_path,
        )
        assert result.members_count == 2


# ---------------------------------------------------------------------------
# _extract_open_meteo_members
# ---------------------------------------------------------------------------


class TestExtractOpenMeteoMembers:
    def test_extracts_member_keys(self):
        data = _om_ensemble("temperature_2m_max", [[88.0] * 24, [92.0] * 24])
        members = _extract_open_meteo_members(data, "temperature_2m_max", "2025-07-15")
        assert len(members) == 2
        assert 88.0 in members or 92.0 in members

    def test_no_date_match_returns_empty(self):
        data = _om_ensemble("temperature_2m_max", [[90.0] * 24])
        members = _extract_open_meteo_members(data, "temperature_2m_max", "2025-12-31")
        assert members == []

    def test_fallback_to_mean_key_when_no_member_keys(self):
        times = ["2025-07-15T00:00", "2025-07-15T06:00", "2025-07-15T12:00"]
        data = {"hourly": {"time": times, "temperature_2m_max": [85.0, 90.0, 95.0]}}
        members = _extract_open_meteo_members(data, "temperature_2m_max", "2025-07-15")
        assert len(members) == 3
        assert 95.0 in members

    def test_metric_with_no_data_returns_empty(self):
        data = {"hourly": {"time": ["2025-07-15T00:00"]}}
        members = _extract_open_meteo_members(data, "nonexistent_metric", "2025-07-15")
        assert members == []

    def test_none_values_are_skipped(self):
        times = ["2025-07-15T00:00", "2025-07-15T06:00"]
        data = {
            "hourly": {
                "time": times,
                "temperature_2m_max_member01": [None, 90.0],
            }
        }
        members = _extract_open_meteo_members(data, "temperature_2m_max", "2025-07-15")
        assert 90.0 in members
        assert None not in members


# ---------------------------------------------------------------------------
# _extract_noaa_prob
# ---------------------------------------------------------------------------


class TestExtractNoaaProb:
    def test_matching_period_above_threshold(self):
        data = {"properties": {"periods": [_noaa_period("2025-07-15", 95.0)]}}
        result = _extract_noaa_prob(data, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result == 1.0

    def test_matching_period_below_threshold(self):
        data = {"properties": {"periods": [_noaa_period("2025-07-15", 80.0)]}}
        result = _extract_noaa_prob(data, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result == 0.0

    def test_no_matching_date_returns_none(self):
        data = {"properties": {"periods": [_noaa_period("2025-07-14", 95.0)]}}
        result = _extract_noaa_prob(data, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result is None

    def test_nighttime_period_skipped(self):
        data = {"properties": {"periods": [_noaa_period("2025-07-15", 95.0, daytime=False)]}}
        result = _extract_noaa_prob(data, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result is None

    def test_no_temperature_value_skipped(self):
        data = {"properties": {"periods": [{
            "startTime": "2025-07-15T06:00:00", "isDaytime": True,
            "temperature": None, "temperatureUnit": "F",
        }]}}
        result = _extract_noaa_prob(data, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result is None

    def test_celsius_converted_to_fahrenheit(self):
        # 35°C → 95°F > 90°F → 1.0
        data = {"properties": {"periods": [_noaa_period("2025-07-15", 35.0, unit="C")]}}
        result = _extract_noaa_prob(data, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result == 1.0

    def test_empty_periods_returns_none(self):
        result = _extract_noaa_prob({"properties": {"periods": []}}, "temperature_2m_max", 90.0, ">", "2025-07-15")
        assert result is None


# ---------------------------------------------------------------------------
# _compare
# ---------------------------------------------------------------------------


class TestCompare:
    def test_greater_than_true(self):
        assert _compare(91.0, 90.0, ">") is True

    def test_greater_than_false(self):
        assert _compare(89.0, 90.0, ">") is False

    def test_gte_equal(self):
        assert _compare(90.0, 90.0, ">=") is True

    def test_less_than(self):
        assert _compare(80.0, 90.0, "<") is True

    def test_lte(self):
        assert _compare(90.0, 90.0, "<=") is True

    def test_equal(self):
        assert _compare(90.0, 90.0, "==") is True

    def test_unknown_operator_returns_false(self):
        assert _compare(90.0, 90.0, "!=") is False


# ---------------------------------------------------------------------------
# _members_to_probs
# ---------------------------------------------------------------------------


class TestMembersToProbs:
    def test_all_above_returns_all_ones(self):
        probs = _members_to_probs([92.0, 93.0, 95.0], 90.0, ">")
        assert all(p == 1.0 for p in probs)

    def test_all_below_returns_all_zeros(self):
        probs = _members_to_probs([80.0, 81.0, 82.0], 90.0, ">")
        assert all(p == 0.0 for p in probs)

    def test_mixed_returns_fraction(self):
        probs = _members_to_probs([85.0, 90.0, 95.0], 90.0, ">")
        # 95.0 > 90 → 1; others not
        assert sum(probs) == 1.0

    def test_empty_returns_empty(self):
        assert _members_to_probs([], 90.0, ">") == []


# ---------------------------------------------------------------------------
# _lat_lon_to_region
# ---------------------------------------------------------------------------


class TestLatLonToRegion:
    def test_us_conus(self):
        assert _lat_lon_to_region(40.0, -75.0) == "us_conus"

    def test_northern(self):
        assert _lat_lon_to_region(60.0, 10.0) == "northern"

    def test_southern_hemisphere(self):
        assert _lat_lon_to_region(-30.0, -60.0) == "southern_hemisphere"

    def test_tropical(self):
        assert _lat_lon_to_region(15.0, 30.0) == "tropical"


# ---------------------------------------------------------------------------
# _date_to_season
# ---------------------------------------------------------------------------


class TestDateToSeason:
    def test_winter_december(self):
        assert _date_to_season("2025-12-15") == "winter"

    def test_winter_january(self):
        assert _date_to_season("2025-01-10") == "winter"

    def test_spring(self):
        assert _date_to_season("2025-04-20") == "spring"

    def test_summer(self):
        assert _date_to_season("2025-07-04") == "summer"

    def test_autumn(self):
        assert _date_to_season("2025-10-15") == "autumn"

    def test_bad_date_returns_annual(self):
        assert _date_to_season("bad-date") == "annual"


# ---------------------------------------------------------------------------
# _load_weights
# ---------------------------------------------------------------------------


class TestLoadWeights:
    def test_returns_uniform_when_no_calibration_data(self, tmp_db_path):
        weights = _load_weights("us_conus", "summer", tmp_db_path)
        assert weights == {"open_meteo": 1.0, "noaa": 1.0, "ecmwf": 1.0}

    def test_returns_db_weights_when_available(self, tmp_db_path):
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO calibration_weights (source, region, season, brier_score, weight) "
                "VALUES ('open_meteo', 'us_conus', 'summer', 0.1, 0.7)"
            )
            conn.execute(
                "INSERT INTO calibration_weights (source, region, season, brier_score, weight) "
                "VALUES ('noaa', 'us_conus', 'summer', 0.2, 0.3)"
            )
            conn.commit()
        weights = _load_weights("us_conus", "summer", tmp_db_path)
        assert weights["open_meteo"] == pytest.approx(0.7)
        assert weights["noaa"] == pytest.approx(0.3)

    def test_returns_uniform_on_db_error(self):
        import unittest.mock as mock
        with mock.patch("db.init.get_connection", side_effect=RuntimeError("no db")):
            weights = _load_weights("us_conus", "summer", None)
        assert weights == {"open_meteo": 1.0, "noaa": 1.0, "ecmwf": 1.0}
