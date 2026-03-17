"""
llm/analyst.py — LLM-powered market analysis functions.

Three tasks:

1. resolution_risk(text) → dict
   Reads market question/resolution criteria and flags ambiguity.
   Routing: Claude Sonnet API → fallback: Ollama if ANTHROPIC_API_KEY unset.

2. narrate_ensemble(model_result) → str
   Plain-English summary of an ensemble ModelResult for the dashboard/Telegram.
   Routing: Ollama always (high frequency, low stakes).

3. trade_commentary(signal, market, model_result) → str
   Human-readable rationale for an executed trade, stored in trades.rationale.
   Routing: Claude Sonnet API → fallback: Ollama if ANTHROPIC_API_KEY unset.

All functions:
  - Return a safe fallback string/dict rather than raising on LLM errors
  - Log warnings when falling back from Claude → Ollama
  - Log warnings when ANTHROPIC_API_KEY is absent
"""
import logging

from config import settings
from engine.models import ModelResult
from market.models import Market, Signal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task 1 — Resolution Risk Analysis
# ---------------------------------------------------------------------------

_RESOLUTION_RISK_SYSTEM = """You are a prediction market resolution analyst.
Given a market question, assess the resolution risk and return ONLY valid JSON:
{
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "reason": "<one-sentence explanation>",
  "recommendation": "<one-sentence trading recommendation>"
}
LOW = clear, objective resolution criteria (e.g. NWS official reading).
MEDIUM = some ambiguity in measurement method or station.
HIGH = vague resolution criteria, multiple interpretations possible."""


def resolution_risk(text: str) -> dict:
    """
    Assess the resolution ambiguity risk for a market question.

    Parameters
    ----------
    text : Market question or resolution criteria text.

    Returns
    -------
    dict with keys: risk_level ("LOW"|"MEDIUM"|"HIGH"), reason, recommendation.
    Falls back to {"risk_level": "MEDIUM", ...} on any error.
    """
    prompt = f"Assess the resolution risk for this prediction market:\n\n{text}"

    result = _try_llm(prompt, _RESOLUTION_RISK_SYSTEM, task="resolution_risk", want_json=True)

    if result and "risk_level" in result:
        return result

    logger.warning("analyst.resolution_risk: all paths failed — defaulting to MEDIUM")
    return {
        "risk_level": "MEDIUM",
        "reason": "Could not assess resolution criteria automatically.",
        "recommendation": "Apply standard edge threshold.",
    }


# ---------------------------------------------------------------------------
# Task 2 — Ensemble Narration
# ---------------------------------------------------------------------------

_NARRATION_SYSTEM = """You are a meteorologist assistant explaining weather forecast
probabilities to a non-expert. Be concise (2-3 sentences). Avoid jargon."""


def narrate_ensemble(model_result: ModelResult) -> str:
    """
    Generate a plain-English summary of an ensemble ModelResult.

    Always uses Ollama (high frequency, low stakes).
    Returns a fallback string on any error.

    Parameters
    ----------
    model_result : The ensemble probability result to narrate.

    Returns
    -------
    A 2-3 sentence English description of the forecast.
    """
    pct = round(model_result.probability * 100)
    ci_low_pct = round(model_result.ci_low * 100)
    ci_high_pct = round(model_result.ci_high * 100)
    sources = ", ".join(model_result.sources) or "unknown sources"
    degraded = ", ".join(model_result.degraded_sources)

    prompt = (
        f"The ensemble weather model estimates a {pct}% probability that the event occurs "
        f"(90% confidence interval: {ci_low_pct}%–{ci_high_pct}%). "
        f"Based on {model_result.members_count} model members from {sources}."
        + (f" Note: {degraded} data unavailable." if degraded else "")
        + "\n\nExplain this forecast in plain English for a trader."
    )

    result = _try_llm(prompt, _NARRATION_SYSTEM, task="narrate_ensemble", want_json=False)
    if result:
        return result

    # Fallback: template-based narration (no LLM needed)
    return (
        f"The model gives this event a {pct}% probability of occurring, "
        f"with a 90% confidence interval of {ci_low_pct}%–{ci_high_pct}%. "
        f"Forecast based on {model_result.members_count} ensemble members."
    )


# ---------------------------------------------------------------------------
# Task 3 — Trade Commentary
# ---------------------------------------------------------------------------

_COMMENTARY_SYSTEM = """You are a quantitative weather trading analyst.
Provide a 1-2 sentence rationale for a trade decision. Be factual and concise.
Mention the edge, model probability, market price, and key forecast factors."""


def trade_commentary(
    signal: Signal,
    market: Market,
    model_result: ModelResult,
) -> str:
    """
    Generate a plain-English rationale for an executed trade.

    Parameters
    ----------
    signal       : The generated trade signal.
    market       : The Polymarket market being traded.
    model_result : The ensemble result that drove the signal.

    Returns
    -------
    A 1-2 sentence rationale string. Falls back to a template on error.
    """
    prompt = (
        f"Market: {market.question}\n"
        f"Direction: {signal.direction}\n"
        f"Model probability: {signal.model_prob:.1%}\n"
        f"Market price: {signal.market_price:.1%}\n"
        f"Adjusted edge: {signal.adjusted_edge:.1%}\n"
        f"Ensemble members: {model_result.members_count} from {', '.join(model_result.sources)}\n"
        f"\nGenerate a 1-2 sentence trade rationale."
    )

    result = _try_llm(prompt, _COMMENTARY_SYSTEM, task="trade_commentary", want_json=False)

    if isinstance(result, str) and result:
        return result

    # Fallback: template
    return (
        f"Trading {signal.direction} at {signal.market_price:.1%} — "
        f"model gives {signal.model_prob:.1%} probability "
        f"(edge: {signal.adjusted_edge:+.1%})."
    )


# ---------------------------------------------------------------------------
# Internal — Unified LLM router: OpenRouter → Anthropic → Ollama
# ---------------------------------------------------------------------------


def _try_llm(
    prompt: str, system: str, task: str, want_json: bool = False
) -> dict | str | None:
    """
    Call the best available LLM in priority order:
      1. OpenRouter  (if OPENROUTER_API_KEY is set)
      2. Anthropic   (if ANTHROPIC_API_KEY is set)
      3. Ollama      (local fallback)

    Parameters
    ----------
    prompt    : User prompt.
    system    : System prompt.
    task      : Name used in log messages.
    want_json : If True, attempt to parse the response as JSON.

    Returns
    -------
    Parsed dict (if want_json=True and parsing succeeds), str, or None on failure.
    """
    import json as _json
    from llm.parser import _extract_json

    raw: str | None = None

    # 1. OpenRouter
    try:
        from llm.openrouter_client import generate as or_generate, is_configured
        if is_configured():
            raw = or_generate(prompt, system=system)
            logger.debug("analyst.%s: used OpenRouter", task)
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyst.%s: OpenRouter failed: %s", task, exc)
        raw = None

    # 2. Anthropic
    if raw is None and settings.ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            logger.debug("analyst.%s: used Anthropic Claude", task)
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyst.%s: Anthropic failed: %s", task, exc)
            raw = None

    # 3. Ollama
    if raw is None:
        try:
            from llm.ollama_client import generate as ol_generate
            raw = ol_generate(prompt, system=system)
            logger.debug("analyst.%s: used Ollama", task)
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyst.%s: Ollama failed: %s", task, exc)
            return None

    if raw is None:
        return None

    # Parse JSON if requested
    if want_json:
        json_str = _extract_json(raw)
        if json_str:
            try:
                return _json.loads(json_str)
            except _json.JSONDecodeError:
                pass
        logger.warning("analyst.%s: LLM response had no valid JSON", task)
        return None

    return raw
