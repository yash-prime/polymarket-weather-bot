"""
llm/ollama_client.py — Ollama local LLM HTTP client.

Provides a thin wrapper around the Ollama REST API with:
  - Startup health-check: verifies llama3.1:8b is loaded
  - generate(prompt) → str  (30s per-call timeout)
  - OLLAMA_DEGRADED flag: set on health-check failure so the rest of the
    system can continue without narration (degraded mode)

Routing:
  - parser.py and analyst.py import `generate` and `is_available`
  - main.py startup calls `health_check()` — sets OLLAMA_DEGRADED on failure
"""
import logging

import requests

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Set to True when health-check fails at startup. Callers check this before
# attempting generation so we fail fast without waiting 30s on every call.
OLLAMA_DEGRADED: bool = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OllamaUnavailableError(RuntimeError):
    """Raised by health_check() when Ollama or the required model is missing."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def health_check() -> None:
    """
    Verify Ollama is running and llama3.1:8b is loaded.

    Sets the module-level OLLAMA_DEGRADED flag to True on failure.
    Raises OllamaUnavailableError with a clear message if Ollama is unreachable
    or the model is absent.

    Called once at startup by main.py. The bot continues in degraded mode
    (regex-only parsing, no narration) if this raises — it does NOT halt.
    """
    global OLLAMA_DEGRADED  # noqa: PLW0603

    try:
        resp = requests.get(
            f"{settings.OLLAMA_HOST}/api/tags",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.ConnectionError as exc:
        OLLAMA_DEGRADED = True
        raise OllamaUnavailableError(
            f"Cannot connect to Ollama at {settings.OLLAMA_HOST}. "
            "Is `ollama serve` running? Bot will continue with regex-only parsing."
        ) from exc
    except requests.exceptions.Timeout as exc:
        OLLAMA_DEGRADED = True
        raise OllamaUnavailableError(
            f"Ollama health-check timed out at {settings.OLLAMA_HOST}. "
            "Bot will continue with regex-only parsing."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        OLLAMA_DEGRADED = True
        raise OllamaUnavailableError(
            f"Ollama returned unexpected status: {exc}. "
            "Bot will continue with regex-only parsing."
        ) from exc

    # Verify model is available
    models = data.get("models", [])
    model_names = [m.get("name", "") for m in models]
    required = settings.OLLAMA_MODEL

    if not any(required in name for name in model_names):
        OLLAMA_DEGRADED = True
        available = ", ".join(model_names) or "(none)"
        raise OllamaUnavailableError(
            f"Required model '{required}' not found in Ollama. "
            f"Available: {available}. "
            f"Run: ollama pull {required}"
        )

    OLLAMA_DEGRADED = False
    logger.info("Ollama health-check passed — model '%s' is available", required)


def is_available() -> bool:
    """Return True if Ollama is considered healthy (not degraded)."""
    return not OLLAMA_DEGRADED


def generate(prompt: str, system: str | None = None) -> str:
    """
    Send a prompt to the local Ollama model and return the response text.

    Parameters
    ----------
    prompt : User message / question to send.
    system : Optional system prompt to set context for the model.

    Returns
    -------
    The model's response as a stripped string.

    Raises
    ------
    OllamaUnavailableError  if OLLAMA_DEGRADED is True (fast-fail).
    requests.exceptions.RequestException on network/timeout errors.
    ValueError if the response JSON lacks the expected 'response' field.
    """
    if OLLAMA_DEGRADED:
        raise OllamaUnavailableError(
            "Ollama is marked DEGRADED — call health_check() to retry."
        )

    payload: dict = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system

    resp = requests.post(
        f"{settings.OLLAMA_HOST}/api/generate",
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()

    data = resp.json()
    text = data.get("response")
    if text is None:
        raise ValueError(
            f"Ollama /api/generate response missing 'response' field: {data}"
        )

    return text.strip()
