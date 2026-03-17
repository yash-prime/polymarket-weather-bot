"""
tests/test_scanner.py — Unit tests for market/scanner.py

Covers:
  - job_fetch_markets: happy path, rate limit, API failure, no weather markets
  - get_active_markets: returns only parse_status="success", filters near-expiry
  - _filter_weather: keyword matching, tag matching
  - _filter_by_constraints: volume filter, days-to-resolve filter
  - _extract_yes_price: outcomePrices, yes_price field, fallback
  - _write_pending: inserts new markets, INSERT OR IGNORE on duplicates
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from market.scanner import (
    _extract_yes_price,
    _filter_by_constraints,
    _filter_weather,
    get_active_markets,
    job_fetch_markets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future_date(days: int = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_date(days: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_raw_market(**kwargs) -> dict:
    defaults = {
        "id": "mkt-001",
        "question": "Will Chicago exceed 90°F on June 10, 2026?",
        "volume": "1000",
        "endDate": _future_date(10),
        "outcomePrices": ["0.35", "0.65"],
        "tags": [{"slug": "weather"}],
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# _filter_weather
# ---------------------------------------------------------------------------


class TestFilterWeather:
    def test_weather_keyword_in_question(self):
        markets = [_make_raw_market(question="Will it rain in Boston?", tags=[])]
        result = _filter_weather(markets)
        assert len(result) == 1

    def test_weather_tag_matches(self):
        markets = [_make_raw_market(question="Some unrelated text", tags=[{"slug": "weather"}])]
        result = _filter_weather(markets)
        assert len(result) == 1

    def test_non_weather_market_excluded(self):
        markets = [_make_raw_market(question="Will Bitcoin hit $100k?", tags=[{"slug": "crypto"}])]
        result = _filter_weather(markets)
        assert len(result) == 0

    def test_temperature_keyword_matches(self):
        markets = [_make_raw_market(question="Will temperature exceed 100°F?", tags=[])]
        result = _filter_weather(markets)
        assert len(result) == 1

    def test_multiple_markets_filtered_correctly(self):
        markets = [
            _make_raw_market(id="m1", question="Will it snow in Denver?", tags=[]),
            _make_raw_market(id="m2", question="Will Bitcoin rally?", tags=[{"slug": "crypto"}]),
            _make_raw_market(id="m3", question="Will humidity spike?", tags=[]),
        ]
        result = _filter_weather(markets)
        ids = [m["id"] for m in result]
        assert "m1" in ids
        assert "m2" not in ids
        assert "m3" not in ids  # humidity not in keywords


# ---------------------------------------------------------------------------
# _filter_by_constraints
# ---------------------------------------------------------------------------


class TestFilterByConstraints:
    def test_low_volume_excluded(self):
        markets = [_make_raw_market(volume="100")]  # below MIN_MARKET_VOLUME=500
        result = _filter_by_constraints(markets)
        assert len(result) == 0

    def test_sufficient_volume_included(self):
        markets = [_make_raw_market(volume="1000")]
        result = _filter_by_constraints(markets)
        assert len(result) == 1

    def test_near_expiry_excluded(self):
        markets = [_make_raw_market(endDate=_future_date(0))]  # expires today < MIN_DAYS
        result = _filter_by_constraints(markets)
        assert len(result) == 0

    def test_sufficient_days_included(self):
        markets = [_make_raw_market(endDate=_future_date(10))]
        result = _filter_by_constraints(markets)
        assert len(result) == 1

    def test_missing_end_date_excluded(self):
        m = _make_raw_market()
        del m["endDate"]
        result = _filter_by_constraints([m])
        assert len(result) == 0

    def test_past_end_date_excluded(self):
        markets = [_make_raw_market(endDate=_past_date(1))]
        result = _filter_by_constraints(markets)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# _extract_yes_price
# ---------------------------------------------------------------------------


class TestExtractYesPrice:
    def test_from_outcome_prices_first_element(self):
        m = {"outcomePrices": ["0.40", "0.60"]}
        assert _extract_yes_price(m) == pytest.approx(0.40)

    def test_from_yes_price_field(self):
        m = {"yes_price": "0.75"}
        assert _extract_yes_price(m) == pytest.approx(0.75)

    def test_fallback_to_half(self):
        assert _extract_yes_price({}) == pytest.approx(0.5)

    def test_invalid_string_falls_back(self):
        m = {"outcomePrices": ["not-a-number"]}
        assert _extract_yes_price(m) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# job_fetch_markets
# ---------------------------------------------------------------------------


class TestJobFetchMarkets:
    def test_happy_path_inserts_new_markets(self, tmp_db_path):
        raw = [_make_raw_market(id="m1"), _make_raw_market(id="m2", question="Will it rain in LA?")]
        with patch("market.scanner._fetch_from_gamma", return_value=raw):
            count = job_fetch_markets(db_path=tmp_db_path)
        # Both are weather markets with sufficient volume and future end_date
        assert count >= 1

    def test_rate_limit_returns_zero(self, tmp_db_path):
        with patch("market.scanner._RATE_LIMITER.check_and_record", return_value=False):
            count = job_fetch_markets(db_path=tmp_db_path)
        assert count == 0

    def test_api_failure_returns_zero(self, tmp_db_path):
        with patch("market.scanner._fetch_from_gamma", return_value=None):
            count = job_fetch_markets(db_path=tmp_db_path)
        assert count == 0

    def test_no_weather_markets_returns_zero(self, tmp_db_path):
        raw = [_make_raw_market(question="Will Bitcoin hit $100k?", tags=[])]
        with patch("market.scanner._fetch_from_gamma", return_value=raw):
            count = job_fetch_markets(db_path=tmp_db_path)
        assert count == 0

    def test_duplicate_market_not_reinserted(self, tmp_db_path):
        raw = [_make_raw_market(id="m-dup")]
        with patch("market.scanner._fetch_from_gamma", return_value=raw):
            count1 = job_fetch_markets(db_path=tmp_db_path)
            count2 = job_fetch_markets(db_path=tmp_db_path)
        # Second call should insert 0 new rows (INSERT OR IGNORE)
        assert count1 >= 1
        assert count2 == 0


# ---------------------------------------------------------------------------
# get_active_markets
# ---------------------------------------------------------------------------


class TestGetActiveMarkets:
    def test_returns_only_success_markets(self, tmp_db_path):
        from db.init import get_connection

        end = _future_date(10)
        parsed = json.dumps({"lat": 41.88, "lon": -87.63})

        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES ('m1', 'Q1', 0.4, ?, 1000, ?, 'success')",
                (end, parsed),
            )
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES ('m2', 'Q2', 0.5, ?, 1000, NULL, 'pending')",
                (end,),
            )
            conn.commit()

        markets = get_active_markets(db_path=tmp_db_path)
        assert len(markets) == 1
        assert markets[0].id == "m1"

    def test_excludes_near_expiry_markets(self, tmp_db_path):
        from db.init import get_connection

        # Market that expires in 1 hour (< MIN_DAYS_TO_RESOLVE = 0.1 days = 2.4 hours)
        near_expiry = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        parsed = json.dumps({"lat": 41.88, "lon": -87.63})

        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES ('m3', 'Q3', 0.5, ?, 1000, ?, 'success')",
                (near_expiry, parsed),
            )
            conn.commit()

        markets = get_active_markets(db_path=tmp_db_path)
        assert len(markets) == 0

    def test_returns_market_objects_with_correct_types(self, tmp_db_path):
        from db.init import get_connection

        end = _future_date(10)
        parsed = json.dumps({"lat": 41.88, "lon": -87.63})

        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, end_date, volume, parsed, parse_status) "
                "VALUES ('m4', 'Will it rain?', 0.35, ?, 2000, ?, 'success')",
                (end, parsed),
            )
            conn.commit()

        markets = get_active_markets(db_path=tmp_db_path)
        assert len(markets) == 1
        m = markets[0]
        assert m.id == "m4"
        assert isinstance(m.yes_price, float)
        assert isinstance(m.parsed, dict)
        assert m.parsed["lat"] == pytest.approx(41.88)
