"""
trading/llm_manager.py — LLM-driven portfolio manager.

Called every scan cycle to analyze the current portfolio and new signals,
then decide what to OPEN, CLOSE, or HOLD. Prevents the bot from opening
contradictory positions on mutually exclusive temperature bins.

Uses OpenRouter (preferred) with Ollama fallback — same pattern as llm/parser.py.
On any failure, returns an empty list (hold everything — never crash the scan cycle).
"""
import json
import logging
import re

from config import settings
from trading.portfolio_analyzer import build_portfolio_context, group_positions_by_event

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a portfolio risk manager for a weather prediction market trading bot. "
    "Analyze positions and signals. Return ONLY a JSON array of actions. "
    "No explanation, no markdown — just the JSON array."
)


def analyze_and_decide(
    open_positions: list[dict],
    new_signals: list[dict],
    portfolio_summary: dict,
    mode: str = "paper",
) -> list[dict]:
    """
    Ask the LLM to review the portfolio and new signals, then return actions.

    Parameters
    ----------
    open_positions : From DB — each dict has market_id, direction, size,
                     entry_price, unrealized_pnl, question, yes_price,
                     end_date, parsed.
    new_signals    : From this scan — each dict has market_id, direction,
                     edge, model_prob, market_price, kelly_size, question.
    portfolio_summary : From build_portfolio_context() — equity, deployed,
                        available, realized_pnl.
    mode           : "paper" | "live".

    Returns
    -------
    List of action dicts:
        {"action": "open",  "market_id": "...", "direction": "YES", "size": 25.0, "reason": "..."}
        {"action": "close", "market_id": "...", "reason": "contradictory position"}
    Empty list on any failure (safe default — hold everything).
    """
    if not open_positions and not new_signals:
        return []

    try:
        prompt = _build_prompt(open_positions, new_signals, portfolio_summary)
        raw = _call_llm(prompt)
        if raw is None:
            logger.warning("llm_manager: LLM call returned None — holding all positions")
            return []

        actions = _parse_response(raw)
        actions = _validate_actions(actions, open_positions, new_signals, portfolio_summary)

        logger.info(
            "llm_manager: LLM returned %d actions (open=%d, close=%d)",
            len(actions),
            sum(1 for a in actions if a["action"] == "open"),
            sum(1 for a in actions if a["action"] == "close"),
        )
        for action in actions:
            logger.info(
                "llm_manager: %s %s — %s",
                action["action"].upper(),
                action.get("market_id", "?")[:16],
                action.get("reason", "no reason"),
            )

        return actions

    except Exception as exc:  # noqa: BLE001
        logger.error("llm_manager.analyze_and_decide: unexpected error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_prompt(
    positions: list[dict],
    signals: list[dict],
    summary: dict,
) -> str:
    """Build a token-efficient prompt for the LLM portfolio manager."""
    parts: list[str] = []

    # Portfolio summary
    parts.append("PORTFOLIO:")
    parts.append(
        f"  equity=${summary['equity']:.0f} deployed=${summary['deployed']:.0f} "
        f"available=${summary['available']:.0f} "
        f"realized_pnl=${summary['realized_pnl']:.0f} "
        f"unrealized_pnl=${summary['unrealized_pnl']:.0f} "
        f"open_positions={summary['open_position_count']}"
    )
    parts.append("")

    # Open positions grouped by event (cap at 40 positions for token efficiency)
    if positions:
        groups = group_positions_by_event(positions)
        total_pos = len(positions)
        parts.append(f"OPEN POSITIONS ({total_pos} total, {len(groups)} event groups):")
        shown = 0
        for group_key, group_positions in groups.items():
            parts.append(f"  --- Group: {group_key} ({len(group_positions)} positions) ---")
            if len(group_positions) > 2:
                parts.append("  *** WARNING: >2 positions in mutually exclusive group ***")
            for p in group_positions:
                if shown >= 40:
                    break
                q_short = str(p.get("question", ""))[:70]
                parts.append(
                    f"    id={p['market_id'][:16]} dir={p.get('direction','?')} "
                    f"size=${float(p.get('size', 0)):.0f} entry={float(p.get('entry_price', 0)):.2f} "
                    f"now={float(p.get('yes_price', 0)):.2f} "
                    f"pnl=${float(p.get('unrealized_pnl', 0)):.1f} "
                    f"q=\"{q_short}\""
                )
                shown += 1
            if shown >= 40:
                break
        if total_pos > 40:
            parts.append(f"  ... and {total_pos - 40} more positions (shown 40 worst groups first)")
        parts.append("")
    else:
        parts.append("OPEN POSITIONS: none")
        parts.append("")

    # New signals sorted by absolute edge
    if signals:
        sorted_signals = sorted(signals, key=lambda s: abs(float(s.get("edge", 0))), reverse=True)
        parts.append(f"NEW SIGNALS ({len(sorted_signals)}):")
        for s in sorted_signals[:20]:  # Cap at 20 to stay token-efficient
            q_short = str(s.get("question", ""))[:80]
            parts.append(
                f"  id={s['market_id'][:16]} dir={s.get('direction','?')} "
                f"edge={float(s.get('edge', 0)):+.1%} "
                f"model={float(s.get('model_prob', 0)):.1%} "
                f"market={float(s.get('market_price', 0)):.1%} "
                f"kelly_size=${float(s.get('kelly_size', 0)):.0f} "
                f"q=\"{q_short}\""
            )
        parts.append("")
    else:
        parts.append("NEW SIGNALS: none")
        parts.append("")

    # Instructions
    parts.append("RULES:")
    parts.append("1. Markets with the same city + date + metric are MUTUALLY EXCLUSIVE — only one bin wins.")
    parts.append("2. Maximum 2 positions per event group (city+date+metric).")
    parts.append("3. If a group has >2 positions, close the weakest (worst edge or biggest loss).")
    parts.append("4. For new signals, only open if: (a) no contradicting open position in same group, "
                 "(b) capital available, (c) edge > 8%.")
    parts.append("5. Be conservative — doing nothing is better than opening a bad position.")
    parts.append("6. Return ONLY a valid JSON array of actions. Each action:")
    parts.append('   {"action":"open","market_id":"...","direction":"YES"|"NO","size":25.0,"reason":"..."}')
    parts.append('   {"action":"close","market_id":"...","reason":"..."}')
    parts.append("7. If no action needed, return an empty array: []")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM call — OpenRouter preferred, Ollama fallback
# ---------------------------------------------------------------------------


def _call_llm(prompt: str) -> str | None:
    """
    Call the LLM via OpenRouter (preferred) or Ollama (fallback).

    Returns the raw response text, or None on failure.
    """
    raw = None

    # Try OpenRouter first
    try:
        from llm.openrouter_client import is_configured
        if is_configured():
            raw = _call_openrouter(prompt)
            if raw is not None:
                logger.debug("llm_manager: used OpenRouter (%s)", settings.OPENROUTER_MODEL)
                return raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_manager: OpenRouter failed: %s — trying Ollama", exc)

    # Fallback to Ollama
    try:
        from llm.ollama_client import generate
        raw = generate(prompt, system=_SYSTEM_PROMPT)
        logger.debug("llm_manager: used Ollama")
        return raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_manager: Ollama also failed: %s", exc)

    return None


def _call_openrouter(prompt: str) -> str | None:
    """Direct OpenRouter call with custom max_tokens and timeout for portfolio analysis."""
    import requests as _requests

    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/yash-prime/polymarket-weather-bot",
        "X-Title": "Polymarket Weather Bot",
    }

    payload = {
        "model": settings.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2048,
    }

    url = f"{settings.OPENROUTER_HOST}/chat/completions"
    resp = _requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
        if text is None:
            logger.warning("llm_manager: OpenRouter returned null content (model may have refused)")
            return None
        return text.strip()
    except (KeyError, IndexError):
        logger.warning("llm_manager: unexpected OpenRouter response: %s", str(data)[:200])
        return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_response(raw: str) -> list[dict]:
    """
    Extract a JSON array from the LLM response.

    Handles:
    - Clean JSON array
    - Markdown-wrapped (```json ... ```)
    - Extra text before/after the array
    - Partial/malformed JSON (returns empty list)
    """
    if not raw or not raw.strip():
        return []

    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    text = text.rstrip("`").strip()

    # Find the JSON array
    json_str = _extract_json_array(text)
    if json_str is None:
        logger.warning("llm_manager: no JSON array found in response (len=%d)", len(raw))
        return []

    try:
        result = json.loads(json_str)
        if not isinstance(result, list):
            logger.warning("llm_manager: LLM returned non-array JSON type: %s", type(result).__name__)
            return []
        return result
    except json.JSONDecodeError as exc:
        logger.warning("llm_manager: JSON parse failed: %s", exc)
        return []


def _extract_json_array(text: str) -> str | None:
    """Find the first complete [...] block in text."""
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_actions(
    actions: list[dict],
    positions: list[dict],
    signals: list[dict],
    summary: dict,
) -> list[dict]:
    """
    Filter out invalid or dangerous actions.

    - Ensure required fields exist
    - Reject opens that exceed available capital
    - Reject opens for unknown market_ids (not in signals)
    - Reject closes for unknown market_ids (not in positions)
    """
    valid: list[dict] = []
    open_market_ids = {p["market_id"] for p in positions}
    signal_market_ids = {s["market_id"] for s in signals}
    available = float(summary.get("available", 0))
    running_spend = 0.0

    for action in actions:
        if not isinstance(action, dict):
            continue

        act = action.get("action", "").lower()
        market_id = action.get("market_id", "")

        if not act or not market_id:
            logger.debug("llm_manager: skipping action with missing fields: %s", action)
            continue

        if act == "close":
            if market_id not in open_market_ids:
                logger.debug("llm_manager: skipping close for unknown position: %s", market_id[:16])
                continue
            valid.append({
                "action": "close",
                "market_id": market_id,
                "reason": action.get("reason", "LLM recommendation"),
            })

        elif act == "open":
            if market_id not in signal_market_ids:
                logger.debug("llm_manager: skipping open for market not in signals: %s", market_id[:16])
                continue

            size = float(action.get("size", 0))
            if size <= 0:
                continue

            # Cap size at available capital
            if running_spend + size > available:
                remaining = available - running_spend
                if remaining < 5.0:  # minimum viable trade
                    logger.debug("llm_manager: skipping open — insufficient capital")
                    continue
                size = remaining

            direction = action.get("direction", "YES").upper()
            if direction not in ("YES", "NO"):
                direction = "YES"

            running_spend += size
            valid.append({
                "action": "open",
                "market_id": market_id,
                "direction": direction,
                "size": round(size, 2),
                "reason": action.get("reason", "LLM recommendation"),
            })
        else:
            logger.debug("llm_manager: unknown action type: %s", act)

    return valid
