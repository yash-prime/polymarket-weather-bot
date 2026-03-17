"""
trading/portfolio_analyzer.py — Position grouping and portfolio context for LLM manager.

Groups open positions by event (same city + date + metric) to detect
mutually exclusive positions. Builds compact portfolio context for the
LLM portfolio manager prompt.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)


def group_positions_by_event(positions: list[dict]) -> dict[str, list[dict]]:
    """
    Group positions by event key: (city, date, metric_base).

    Returns {group_key: [positions...]}.

    Uses parsed JSON from each position to extract city, window_start, metric.
    Two positions are in the same group if they share the same location+date+metric type.
    """
    groups: dict[str, list[dict]] = {}
    for pos in positions:
        key = _extract_group_key(pos)
        groups.setdefault(key, []).append(pos)
    return groups


def build_portfolio_context(positions: list[dict], snap: dict | None) -> dict:
    """
    Build the portfolio summary dict for the LLM prompt.

    Parameters
    ----------
    positions : Open positions with market metadata attached.
    snap      : Most recent portfolio snapshot dict (or None).

    Returns
    -------
    Dict with equity, deployed, available, realized_pnl, unrealized_pnl.
    """
    _CAPITAL_LIMIT = 2500.0

    deployed = sum(float(p.get("size", 0)) for p in positions)
    unrealized = sum(float(p.get("unrealized_pnl", 0)) for p in positions)

    if snap:
        equity = float(snap.get("total_equity", _CAPITAL_LIMIT))
        realized = float(snap.get("realized_pnl", 0))
    else:
        equity = _CAPITAL_LIMIT
        realized = 0.0

    available = max(0.0, _CAPITAL_LIMIT - deployed)

    return {
        "equity": round(equity, 2),
        "deployed": round(deployed, 2),
        "available": round(available, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_position_count": len(positions),
    }


def _extract_group_key(position: dict) -> str:
    """
    Extract a grouping key from a position's parsed data.

    Returns a string like "seattle|2026-03-20|temperature" or
    "unknown|unknown|unknown" if parsing fails.
    """
    parsed = position.get("parsed")
    if parsed is None:
        return f"ungrouped|{position.get('market_id', 'unknown')}"

    # parsed may be a dict or a JSON string
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, ValueError):
            # Try Python repr (ast.literal_eval)
            try:
                import ast
                parsed = ast.literal_eval(parsed)
            except Exception:
                return f"ungrouped|{position.get('market_id', 'unknown')}"

    if not isinstance(parsed, dict):
        return f"ungrouped|{position.get('market_id', 'unknown')}"

    city = str(parsed.get("city", "unknown")).lower().strip()
    window_start = str(parsed.get("window_start", "unknown")).strip()
    metric = str(parsed.get("metric", "unknown")).strip()

    # Normalize metric to base type (temperature, precipitation, wind)
    metric_base = _normalize_metric(metric)

    return f"{city}|{window_start}|{metric_base}"


def _normalize_metric(metric: str) -> str:
    """Collapse specific metric names to base categories."""
    metric_lower = metric.lower()
    if "temperature" in metric_lower or "temp" in metric_lower:
        return "temperature"
    if "precip" in metric_lower or "rain" in metric_lower:
        return "precipitation"
    if "wind" in metric_lower:
        return "wind"
    return metric_lower
