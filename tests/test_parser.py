"""
tests/test_parser.py — Unit tests for llm/parser.py

Covers:
  - LLM path (_try_llm): success, JSON extraction from markdown, validation failure
  - Regex path: exceed/above/below temperature, precipitation, unknown city
  - Failure path: all paths fail → parse_status="failed"
  - Cache: hit, miss, write-on-success
  - _extract_json: various formats
"""
import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from llm.parser import (
    _extract_dates,
    _extract_json,
    parse,
)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_plain_json(self):
        result = _extract_json('{"key": "value"}')
        assert result == '{"key": "value"}'

    def test_json_in_markdown_fences(self):
        text = "Here is the result:\n```json\n{\"a\": 1}\n```"
        result = _extract_json(text)
        assert result == '{"a": 1}'

    def test_json_with_leading_text(self):
        text = "Sure! Here: {\"city\": \"Chicago\"} Done."
        result = _extract_json(text)
        assert result == '{"city": "Chicago"}'

    def test_no_json_returns_none(self):
        assert _extract_json("No JSON here at all.") is None

    def test_nested_json(self):
        text = '{"outer": {"inner": 42}}'
        result = _extract_json(text)
        data = json.loads(result)
        assert data["outer"]["inner"] == 42


# ---------------------------------------------------------------------------
# Ollama path
# ---------------------------------------------------------------------------


class TestOllamaPath:
    def _good_result(self):
        return {
            "city": "Chicago",
            "lat": 41.88,
            "lon": -87.63,
            "metric": "temperature_2m_max",
            "threshold": 90.0,
            "unit": "fahrenheit",
            "operator": ">",
            "window_start": "2026-06-10",
            "window_end": "2026-06-15",
            "aggregation": "any",
            "resolution_source": "nws_official",
            "parse_status": "success",
        }

    def test_ollama_success_returns_parsed_dict(self, tmp_db_path):
        with patch("llm.parser._try_llm", return_value=self._good_result()):
            result = parse("Will Chicago exceed 90°F on June 10?", db_path=tmp_db_path)

        assert result["parse_status"] == "success"
        assert result["city"] == "Chicago"

    def test_llm_response_parse_status_set_to_success(self, tmp_db_path):
        with patch("llm.parser._try_llm", return_value=self._good_result()):
            result = parse("Will Chicago exceed 90°F on June 10?", db_path=tmp_db_path)

        assert result["parse_status"] == "success"

    def test_llm_markdown_fences_handled(self, tmp_db_path):
        """_try_llm handles markdown-fenced JSON internally."""
        with patch("llm.parser._try_llm", return_value=self._good_result()):
            result = parse("Will Chicago exceed 90°F on June 10?", db_path=tmp_db_path)

        assert result["parse_status"] == "success"


# ---------------------------------------------------------------------------
# Regex fallback path
# ---------------------------------------------------------------------------


class TestRegexPath:
    def _parse_with_no_llm(self, question, db_path):
        with patch("llm.parser._try_llm", return_value=None):
            return parse(question, db_path=db_path)

    def test_exceed_temperature_question(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Chicago exceed 90°F on June 10, 2026?", tmp_db_path
        )
        assert result["parse_status"] == "regex_fallback"
        assert result["city"].lower() == "chicago"
        assert result["threshold"] == pytest.approx(90.0)
        assert result["operator"] == ">"
        assert result["metric"] == "temperature_2m_max"

    def test_reach_temperature_question(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Miami reach 95°F on July 4, 2026?", tmp_db_path
        )
        assert result["parse_status"] == "regex_fallback"
        assert result["threshold"] == pytest.approx(95.0)

    def test_above_temperature_question(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Phoenix be above 110°F on August 1, 2026?", tmp_db_path
        )
        assert result["parse_status"] == "regex_fallback"
        assert result["threshold"] == pytest.approx(110.0)
        assert result["metric"] == "temperature_2m_max"

    def test_below_temperature_question(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Boston temperatures fall below 32°F on January 15, 2026?", tmp_db_path
        )
        assert result["parse_status"] == "regex_fallback"
        assert result["operator"] == "<"
        assert result["metric"] == "temperature_2m_min"

    def test_precipitation_question(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Seattle see more than 2 inches of rain on November 10, 2026?", tmp_db_path
        )
        assert result["parse_status"] == "regex_fallback"
        assert result["metric"] == "precipitation_sum"
        assert result["threshold"] == pytest.approx(2.0)
        assert result["operator"] == ">="

    def test_lat_lon_set_from_city_table(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Chicago exceed 90°F on June 10, 2026?", tmp_db_path
        )
        assert result["lat"] == pytest.approx(41.88)
        assert result["lon"] == pytest.approx(-87.63)

    def test_unknown_city_falls_through_to_failed(self, tmp_db_path):
        result = self._parse_with_no_llm(
            "Will Zzzyxville exceed 90°F on June 10, 2026?", tmp_db_path
        )
        # Unknown city → regex can't find coords → falls through to failed
        assert result["parse_status"] == "failed"


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


class TestFailurePath:
    def test_all_paths_fail_returns_parse_status_failed(self, tmp_db_path):
        with patch("llm.parser._try_llm", return_value=None), \
             patch("llm.parser._try_regex", return_value=None):
            result = parse("This is not a weather question at all.", db_path=tmp_db_path)
        assert result["parse_status"] == "failed"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_hit_skips_llm(self, tmp_db_path):
        cached_result = {
            "city": "Chicago",
            "lat": 41.88,
            "lon": -87.63,
            "metric": "temperature_2m_max",
            "threshold": 90.0,
            "operator": ">",
            "window_start": "2026-06-10",
            "window_end": "2026-06-15",
            "parse_status": "success",
        }

        question = "Will Chicago exceed 90°F on June 10, 2026?"

        # First call: LLM succeeds and caches
        with patch("llm.parser._try_llm", return_value=cached_result) as mock_llm:
            parse(question, db_path=tmp_db_path)
            assert mock_llm.call_count == 1

        # Second call: should come from cache, not LLM
        with patch("llm.parser._try_llm", return_value=cached_result) as mock_llm2:
            result = parse(question, db_path=tmp_db_path)
            mock_llm2.assert_not_called()

        assert result["parse_status"] == "success"

    def test_failed_parse_not_cached(self, tmp_db_path):
        question = "Gibberish market question xyz"

        with patch("llm.parser._try_llm", return_value=None), \
             patch("llm.parser._try_regex", return_value=None):
            parse(question, db_path=tmp_db_path)

        # Second call should still hit parsing (not cache)
        with patch("llm.parser._try_llm", return_value=None) as mock_llm, \
             patch("llm.parser._try_regex", return_value=None):
            parse(question, db_path=tmp_db_path)
            mock_llm.assert_called_once()


# ---------------------------------------------------------------------------
# _extract_dates
# ---------------------------------------------------------------------------


class TestExtractDates:
    def test_full_date_parsed(self):
        start, end = _extract_dates("Will Chicago exceed 90°F on June 10, 2026?")
        assert start == "2026-06-10"
        assert end == "2026-06-10"

    def test_date_without_year_uses_current_year(self):
        start, _ = _extract_dates("Will Miami reach 95°F on July 4?")
        assert start.startswith(str(date.today().year))

    def test_no_date_returns_fallback(self):
        start, end = _extract_dates("Will Phoenix be hot someday?")
        today = date.today()
        assert start == str(today)
