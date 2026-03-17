"""
llm/openrouter_client.py — OpenRouter LLM client (OpenAI-compatible API).

OpenRouter provides a unified API for hundreds of models including free-tier
options. Uses the OpenAI chat-completions format at:
  https://openrouter.ai/api/v1/chat/completions

When OPENROUTER_API_KEY is set, this client is used instead of both Ollama
and the Anthropic SDK. Priority in the codebase:
  1. OpenRouter  (if OPENROUTER_API_KEY is set)
  2. Anthropic   (if ANTHROPIC_API_KEY is set, for analyst.py only)
  3. Ollama      (local fallback)
  4. Regex       (parser.py only)

Public API:
  is_configured() -> bool
  generate(prompt, system=None) -> str
"""
import logging

import requests

from config import settings

logger = logging.getLogger(__name__)

_CHAT_URL = f"{settings.OPENROUTER_HOST}/chat/completions"


def is_configured() -> bool:
    """Return True if OPENROUTER_API_KEY is set."""
    return bool(settings.OPENROUTER_API_KEY)


def generate(
    prompt: str,
    system: str | None = None,
    max_tokens: int | None = None,
    timeout: int = 30,
) -> str:
    """
    Send a prompt to OpenRouter and return the response text.

    Parameters
    ----------
    prompt : User message.
    system : Optional system prompt.
    max_tokens : Optional maximum number of tokens for the response.
    timeout : Request timeout in seconds (default 30).

    Returns
    -------
    Model response as a stripped string.

    Raises
    ------
    RuntimeError if OpenRouter is not configured.
    requests.exceptions.RequestException on network/HTTP errors.
    ValueError if the response JSON is malformed.
    """
    if not is_configured():
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set — cannot use OpenRouter client."
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/yash-prime/polymarket-weather-bot",
        "X-Title": "Polymarket Weather Bot",
    }

    payload = {
        "model": settings.OPENROUTER_MODEL,
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    resp = requests.post(_CHAT_URL, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"Unexpected OpenRouter response structure: {data}"
        ) from exc

    if text is None:
        raise ValueError(
            f"OpenRouter returned null content — model may have refused the request "
            f"(model={settings.OPENROUTER_MODEL})"
        )

    logger.debug(
        "openrouter.generate: model=%s tokens_used=%s",
        settings.OPENROUTER_MODEL,
        data.get("usage", {}).get("total_tokens", "?"),
    )
    return text.strip()
