"""
tests/test_analyst.py — Unit tests for llm/analyst.py

Covers:
  - resolution_risk: Claude success, Ollama fallback, all-paths-failed default
  - narrate_ensemble: Ollama success, fallback template
  - trade_commentary: Claude success, Ollama fallback, template fallback
  - ANTHROPIC_API_KEY absent → warning logged, routes to Ollama
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.models import ModelResult
from llm.analyst import narrate_ensemble, resolution_risk, trade_commentary
from market.models import Market, Signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _model_result() -> ModelResult:
    return ModelResult(
        probability=0.65,
        confidence=0.80,
        ci_low=0.50,
        ci_high=0.80,
        members_count=51,
        sources=["open_meteo_ensemble"],
        degraded_sources=["ecmwf"],
    )


def _market() -> Market:
    return Market(
        id="mkt-1",
        question="Will Chicago exceed 90°F on June 10, 2026?",
        yes_price=0.40,
        end_date=datetime(2026, 6, 10, tzinfo=timezone.utc),
        volume=1000.0,
        parsed={"lat": 41.88, "lon": -87.63},
        parse_status="success",
    )


def _signal() -> Signal:
    return Signal(
        market_id="mkt-1",
        direction="YES",
        raw_kelly_size=0.05,
        adjusted_edge=0.12,
        model_prob=0.65,
        market_price=0.40,
    )


# ---------------------------------------------------------------------------
# resolution_risk
# ---------------------------------------------------------------------------


class TestResolutionRisk:
    def test_claude_success_returns_dict(self):
        claude_response = {
            "risk_level": "LOW",
            "reason": "NWS official reading specified.",
            "recommendation": "Trade at standard threshold.",
        }
        with patch("llm.analyst._try_claude", return_value=claude_response):
            result = resolution_risk("Will Chicago exceed 90°F?")
        assert result["risk_level"] == "LOW"
        assert "reason" in result

    def test_ollama_fallback_when_no_api_key(self):
        ollama_response = {
            "risk_level": "MEDIUM",
            "reason": "Station not specified.",
            "recommendation": "Widen threshold.",
        }
        with patch("llm.analyst._try_claude", return_value=None), \
             patch("llm.analyst._try_ollama_json", return_value=ollama_response):
            result = resolution_risk("Some market question")
        assert result["risk_level"] == "MEDIUM"

    def test_all_paths_fail_returns_medium_default(self):
        with patch("llm.analyst._try_claude", return_value=None), \
             patch("llm.analyst._try_ollama_json", return_value=None):
            result = resolution_risk("Ambiguous question")
        assert result["risk_level"] == "MEDIUM"
        assert "risk_level" in result
        assert "reason" in result
        assert "recommendation" in result

    def test_no_api_key_logs_warning(self, caplog):
        import logging
        with patch("config.settings.ANTHROPIC_API_KEY", ""), \
             patch("llm.analyst._try_ollama_json", return_value={"risk_level": "LOW", "reason": "x", "recommendation": "y"}), \
             caplog.at_level(logging.WARNING, logger="llm.analyst"):
            resolution_risk("Some question")
        assert any("ANTHROPIC_API_KEY" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# narrate_ensemble
# ---------------------------------------------------------------------------


class TestNarrateEnsemble:
    def test_ollama_success_returns_text(self):
        with patch("llm.analyst._try_ollama_text", return_value="The forecast looks sunny."):
            result = narrate_ensemble(_model_result())
        assert result == "The forecast looks sunny."

    def test_ollama_failure_returns_template(self):
        with patch("llm.analyst._try_ollama_text", return_value=None):
            result = narrate_ensemble(_model_result())
        assert "65%" in result
        assert "51" in result  # members_count

    def test_degraded_sources_mentioned_in_prompt(self):
        """Degraded sources are mentioned in the prompt sent to Ollama."""
        mr = _model_result()  # has degraded_sources=["ecmwf"]
        captured_prompt = []

        def fake_ollama(prompt, system, task):
            captured_prompt.append(prompt)
            return "narration"

        with patch("llm.analyst._try_ollama_text", side_effect=fake_ollama):
            narrate_ensemble(mr)

        assert "ecmwf" in captured_prompt[0].lower() or "unavailable" in captured_prompt[0].lower()

    def test_returns_string_not_none(self):
        """narrate_ensemble always returns a non-empty string."""
        with patch("llm.analyst._try_ollama_text", return_value=None):
            result = narrate_ensemble(_model_result())
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# trade_commentary
# ---------------------------------------------------------------------------


class TestTradeCommentary:
    def test_claude_success_returns_text(self):
        with patch("llm.analyst._try_claude", return_value="Strong YES signal due to heatwave."):
            result = trade_commentary(_signal(), _market(), _model_result())
        assert result == "Strong YES signal due to heatwave."

    def test_ollama_fallback_returns_text(self):
        with patch("llm.analyst._try_claude", return_value=None), \
             patch("llm.analyst._try_ollama_text", return_value="Model prob 65%, edge 12%."):
            result = trade_commentary(_signal(), _market(), _model_result())
        assert result == "Model prob 65%, edge 12%."

    def test_template_fallback_when_all_fail(self):
        with patch("llm.analyst._try_claude", return_value=None), \
             patch("llm.analyst._try_ollama_text", return_value=None):
            result = trade_commentary(_signal(), _market(), _model_result())
        assert "YES" in result
        assert "65" in result
        assert "40" in result

    def test_always_returns_string(self):
        with patch("llm.analyst._try_claude", return_value=None), \
             patch("llm.analyst._try_ollama_text", return_value=None):
            result = trade_commentary(_signal(), _market(), _model_result())
        assert isinstance(result, str)
        assert len(result) > 0
