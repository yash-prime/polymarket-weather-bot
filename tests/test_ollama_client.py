"""
tests/test_ollama_client.py — Unit tests for llm/ollama_client.py

Covers:
  - health_check: success, connection error, timeout, bad status, model missing
  - generate: success, OLLAMA_DEGRADED fast-fail, timeout, missing response field
  - is_available: reflects OLLAMA_DEGRADED flag
"""
import importlib
from unittest.mock import MagicMock, patch

import pytest
import requests

import llm.ollama_client as client_mod
from llm.ollama_client import (
    OllamaUnavailableError,
    generate,
    health_check,
    is_available,
)


def _reset_degraded():
    """Reset module-level OLLAMA_DEGRADED before each test."""
    client_mod.OLLAMA_DEGRADED = False


@pytest.fixture(autouse=True)
def reset_degraded_flag():
    _reset_degraded()
    yield
    _reset_degraded()


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_success_clears_degraded_flag(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "models": [{"name": "llama3.1:8b"}]
        }
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.get", return_value=mock_resp):
            health_check()

        assert client_mod.OLLAMA_DEGRADED is False

    def test_connection_error_sets_degraded_and_raises(self):
        with patch(
            "llm.ollama_client.requests.get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with pytest.raises(OllamaUnavailableError, match="Cannot connect"):
                health_check()

        assert client_mod.OLLAMA_DEGRADED is True

    def test_timeout_sets_degraded_and_raises(self):
        with patch(
            "llm.ollama_client.requests.get",
            side_effect=requests.exceptions.Timeout(),
        ):
            with pytest.raises(OllamaUnavailableError, match="timed out"):
                health_check()

        assert client_mod.OLLAMA_DEGRADED is True

    def test_http_error_sets_degraded_and_raises(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")

        with patch("llm.ollama_client.requests.get", return_value=mock_resp):
            with pytest.raises(OllamaUnavailableError):
                health_check()

        assert client_mod.OLLAMA_DEGRADED is True

    def test_required_model_missing_sets_degraded_and_raises(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "models": [{"name": "phi3:mini"}, {"name": "mistral:7b"}]
        }
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.get", return_value=mock_resp):
            with pytest.raises(OllamaUnavailableError, match="not found"):
                health_check()

        assert client_mod.OLLAMA_DEGRADED is True

    def test_empty_models_list_sets_degraded_and_raises(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"models": []}
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.get", return_value=mock_resp):
            with pytest.raises(OllamaUnavailableError):
                health_check()

        assert client_mod.OLLAMA_DEGRADED is True

    def test_model_name_partial_match(self):
        """Model version tag after colon is tolerated (e.g. 'llama3.1:8b-instruct')."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "models": [{"name": "llama3.1:8b-instruct-q4_0"}]
        }
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.get", return_value=mock_resp):
            health_check()

        assert client_mod.OLLAMA_DEGRADED is False


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_true_when_not_degraded(self):
        client_mod.OLLAMA_DEGRADED = False
        assert is_available() is True

    def test_false_when_degraded(self):
        client_mod.OLLAMA_DEGRADED = True
        assert is_available() is False


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_returns_response_text(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "  The forecast is sunny.  "}
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.post", return_value=mock_resp):
            result = generate("What is the weather?")

        assert result == "The forecast is sunny."

    def test_strips_whitespace(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "\n  trimmed  \n"}
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.post", return_value=mock_resp):
            result = generate("prompt")

        assert result == "trimmed"

    def test_fast_fail_when_degraded(self):
        client_mod.OLLAMA_DEGRADED = True
        with pytest.raises(OllamaUnavailableError, match="DEGRADED"):
            generate("any prompt")

    def test_raises_value_error_on_missing_response_field(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"unexpected": "format"}
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.post", return_value=mock_resp):
            with pytest.raises(ValueError, match="missing 'response' field"):
                generate("prompt")

    def test_timeout_propagates(self):
        with patch(
            "llm.ollama_client.requests.post",
            side_effect=requests.exceptions.Timeout(),
        ):
            with pytest.raises(requests.exceptions.Timeout):
                generate("prompt")

    def test_system_prompt_included_in_payload(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.post", return_value=mock_resp) as mock_post:
            generate("user prompt", system="You are a weather expert.")

        payload = mock_post.call_args[1]["json"]
        assert payload["system"] == "You are a weather expert."
        assert payload["prompt"] == "user prompt"

    def test_no_system_prompt_omits_key(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status.return_value = None

        with patch("llm.ollama_client.requests.post", return_value=mock_resp) as mock_post:
            generate("user prompt")

        payload = mock_post.call_args[1]["json"]
        assert "system" not in payload
