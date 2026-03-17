"""
tests/test_weather.py — Unit tests for engine/weather.py

Covers:
  - Normal case: all three sources succeed
  - Single-source fallback: only Open-Meteo available
  - All-sources-failed: returns None
  - No parsed JSON: returns None
  - Incomplete parsed JSON: returns None
  - US coordinates: NOAA is fetched
  - Non-US coordinates: NOAA is NOT fetched
  - Unknown metric: open_meteo skipped, result still possible via NOAA/ECMWF
  - Internal helpers: _is_us_coordinates
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.models import ModelResult
from engine.weather import _is_us_coordinates, compute
from market.models import Market


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PARSED_US = {
    "city": "Chicago",
    "lat": 41.88,
    "lon": -87.63,
    "metric": "temperature_2m_max",
    "threshold": 90,
    "operator": ">",
    "window_start": "2026-06-10",
    "window_end": "2026-06-15",
}

_PARSED_EU = {
    "city": "London",
    "lat": 51.50,
    "lon": -0.12,
    "metric": "temperature_2m_max",
    "threshold": 30,
    "operator": ">",
    "window_start": "2026-07-01",
    "window_end": "2026-07-05",
}

_FAKE_MODEL_RESULT = ModelResult(
    probability=0.40,
    confidence=0.80,
    ci_low=0.25,
    ci_high=0.55,
    members_count=51,
    sources=["open_meteo_ensemble"],
    degraded_sources=[],
)


def _make_market(parsed: dict | None = _PARSED_US, parse_status: str = "success") -> Market:
    return Market(
        id="mkt-test",
        question="Will Chicago exceed 90°F?",
        yes_price=0.30,
        end_date=datetime(2026, 6, 15, tzinfo=timezone.utc),
        volume=1000.0,
        parsed=parsed,
        parse_status=parse_status,
    )


# ---------------------------------------------------------------------------
# _is_us_coordinates
# ---------------------------------------------------------------------------


class TestIsUsCoordinates:
    def test_chicago_is_us(self):
        assert _is_us_coordinates(41.88, -87.63) is True

    def test_new_york_is_us(self):
        assert _is_us_coordinates(40.71, -74.00) is True

    def test_london_is_not_us(self):
        assert _is_us_coordinates(51.50, -0.12) is False

    def test_tokyo_is_not_us(self):
        assert _is_us_coordinates(35.68, 139.69) is False

    def test_lat_below_range_is_not_us(self):
        assert _is_us_coordinates(20.0, -90.0) is False

    def test_lat_above_range_is_not_us(self):
        assert _is_us_coordinates(55.0, -90.0) is False


# ---------------------------------------------------------------------------
# compute() — normal and fallback cases
# ---------------------------------------------------------------------------


class TestComputeAllSourcesSucceed:
    """All three sources return data — ModelResult is produced."""

    def test_returns_model_result(self):
        market = _make_market(_PARSED_US)
        with (
            patch("engine.weather._fetch_open_meteo", return_value={"hourly": {}}),
            patch("engine.weather._fetch_noaa", return_value={"properties": {"periods": []}}),
            patch("engine.weather._fetch_ecmwf", return_value=92.0),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            result = compute(market)
        assert result is _FAKE_MODEL_RESULT

    def test_noaa_called_for_us_coordinates(self):
        market = _make_market(_PARSED_US)
        with (
            patch("engine.weather._fetch_open_meteo", return_value={}),
            patch("engine.weather._fetch_noaa", return_value={}) as mock_noaa,
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            compute(market)
        mock_noaa.assert_called_once()

    def test_noaa_not_called_for_eu_coordinates(self):
        market = _make_market(_PARSED_EU)
        with (
            patch("engine.weather._fetch_open_meteo", return_value={}),
            patch("engine.weather._fetch_noaa", return_value={}) as mock_noaa,
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            compute(market)
        mock_noaa.assert_not_called()


class TestComputeSingleSourceFallback:
    """Only one source has data — result is still returned (degraded)."""

    def test_only_open_meteo_succeeds(self):
        market = _make_market(_PARSED_EU)
        with (
            patch("engine.weather._fetch_open_meteo", return_value={"hourly": {}}),
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            result = compute(market)
        assert result is _FAKE_MODEL_RESULT

    def test_only_ecmwf_succeeds(self):
        market = _make_market(_PARSED_EU)
        with (
            patch("engine.weather._fetch_open_meteo", return_value=None),
            patch("engine.weather._fetch_ecmwf", return_value=88.0),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            result = compute(market)
        assert result is _FAKE_MODEL_RESULT

    def test_only_noaa_succeeds_for_us(self):
        market = _make_market(_PARSED_US)
        with (
            patch("engine.weather._fetch_open_meteo", return_value=None),
            patch("engine.weather._fetch_noaa", return_value={"properties": {}}),
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            result = compute(market)
        assert result is _FAKE_MODEL_RESULT


class TestComputeAllSourcesFailed:
    """All sources return None → compute() returns None."""

    def test_returns_none_when_all_sources_fail(self):
        market = _make_market(_PARSED_US)
        with (
            patch("engine.weather._fetch_open_meteo", return_value=None),
            patch("engine.weather._fetch_noaa", return_value=None),
            patch("engine.weather._fetch_ecmwf", return_value=None),
        ):
            result = compute(market)
        assert result is None

    def test_compute_probability_not_called_when_all_sources_fail(self):
        market = _make_market(_PARSED_US)
        with (
            patch("engine.weather._fetch_open_meteo", return_value=None),
            patch("engine.weather._fetch_noaa", return_value=None),
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability") as mock_cp,
        ):
            compute(market)
        mock_cp.assert_not_called()


class TestComputeInvalidInput:
    """Malformed or missing input returns None without raising."""

    def test_no_parsed_json_returns_none(self):
        market = _make_market(parsed=None)
        result = compute(market)
        assert result is None

    def test_empty_dict_returns_none(self):
        market = _make_market(parsed={})
        result = compute(market)
        assert result is None

    def test_missing_lat_returns_none(self):
        parsed = dict(_PARSED_US)
        del parsed["lat"]
        market = _make_market(parsed=parsed)
        result = compute(market)
        assert result is None

    def test_missing_lon_returns_none(self):
        parsed = dict(_PARSED_US)
        del parsed["lon"]
        market = _make_market(parsed=parsed)
        result = compute(market)
        assert result is None

    def test_missing_metric_returns_none(self):
        parsed = dict(_PARSED_US)
        del parsed["metric"]
        market = _make_market(parsed=parsed)
        result = compute(market)
        assert result is None

    def test_missing_window_start_returns_none(self):
        parsed = dict(_PARSED_US)
        del parsed["window_start"]
        market = _make_market(parsed=parsed)
        result = compute(market)
        assert result is None

    def test_unknown_metric_still_works_if_source_has_data(self):
        """Unknown metric cannot be mapped to OM variable, but ECMWF/NOAA may still work."""
        parsed = dict(_PARSED_US)
        parsed["metric"] = "solar_radiation"  # unmapped
        market = _make_market(parsed=parsed)
        with (
            patch("engine.weather._fetch_noaa", return_value={"properties": {}}),
            patch("engine.weather._fetch_ecmwf", return_value=88.0),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT),
        ):
            result = compute(market)
        # open_meteo skipped (None variable), noaa + ecmwf provide data
        assert result is _FAKE_MODEL_RESULT


class TestComputePassesCorrectArgsToEnsemble:
    """Verify compute() correctly extracts and forwards parsed JSON fields."""

    def test_passes_lat_lon_metric_threshold(self):
        market = _make_market(_PARSED_US)
        with (
            patch("engine.weather._fetch_open_meteo", return_value={"hourly": {}}),
            patch("engine.weather._fetch_noaa", return_value=None),
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT) as mock_cp,
        ):
            compute(market)

        call_kwargs = mock_cp.call_args[1]
        assert call_kwargs["lat"] == pytest.approx(41.88)
        assert call_kwargs["lon"] == pytest.approx(-87.63)
        assert call_kwargs["metric"] == "temperature_2m_max"
        assert call_kwargs["threshold"] == pytest.approx(90.0)
        assert call_kwargs["operator"] == ">"
        assert call_kwargs["forecast_date"] == "2026-06-10"

    def test_default_operator_is_greater_than(self):
        parsed = dict(_PARSED_US)
        del parsed["operator"]
        market = _make_market(parsed=parsed)
        with (
            patch("engine.weather._fetch_open_meteo", return_value={}),
            patch("engine.weather._fetch_noaa", return_value=None),
            patch("engine.weather._fetch_ecmwf", return_value=None),
            patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT) as mock_cp,
        ):
            compute(market)
        assert mock_cp.call_args[1]["operator"] == ">"
