"""
market/signal.py — Edge calculation and trade signal generation.

Implements the corrected Kelly Criterion for binary prediction markets.

Kelly formula (corrected, ARCHITECTURE.md v2.0):
    b = (1 - price) / price  for YES  (payout per unit risked)
    b = price / (1 - price)  for NO
    f* = (b * p - q) / b     where p = win prob, q = 1-p
    raw_kelly_size = max(0.0, KELLY_FRACTION * f*)

Time-decay (clamped, ARCHITECTURE.md v2.0):
    days = max(0.0, days_to_resolution)
    time_decay = max(0.1, confidence * (1.0 - 0.02 * min(days, 45)))
    → clamped to [0.1, 1.0] — never negative, never zero

Adjusted edge:
    adjusted_edge = raw_edge * time_decay - liquidity_penalty
    liquidity_penalty = 0.02 if volume < 1000 else 0.0

Signal generated only when:
    abs(adjusted_edge) >= MIN_EDGE_THRESHOLD (default 8%)
"""
import logging
from datetime import datetime, timezone

from config import settings
from engine.models import ModelResult
from market.models import Market, Signal

logger = logging.getLogger(__name__)


def compute_signal(market: Market, model_result: ModelResult) -> Signal | None:
    """
    Compute a trade signal for a market given model probability estimates.

    Parameters
    ----------
    market       : Market with yes_price and end_date set.
    model_result : Ensemble probability output.

    Returns
    -------
    Signal if abs(adjusted_edge) >= MIN_EDGE_THRESHOLD, else None.
    """
    market_price = market.yes_price       # P(YES) implied by market
    model_prob = model_result.probability  # P(YES) from ensemble model
    confidence = model_result.confidence   # [0, 1] — agreement across members

    raw_edge = model_prob - market_price

    # Time decay: reduce signal strength for far-future markets
    # Clamped: prevents negative values on long-dated markets (>45 days)
    now = datetime.now(timezone.utc)
    end_date = market.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    days_to_resolve = max(0.0, (end_date - now).total_seconds() / 86400)
    time_decay = max(0.1, confidence * (1.0 - 0.02 * min(days_to_resolve, 45)))

    # Liquidity penalty: thin books need bigger edge to trade profitably
    liquidity_penalty = 0.02 if market.volume < 1000 else 0.0

    adjusted_edge = raw_edge * time_decay - liquidity_penalty

    if abs(adjusted_edge) < settings.MIN_EDGE_THRESHOLD:
        logger.debug(
            "signal: market %s — edge %.4f below threshold %.4f — no trade",
            market.id, adjusted_edge, settings.MIN_EDGE_THRESHOLD,
        )
        return None

    direction = "YES" if adjusted_edge > 0 else "NO"

    # Corrected Kelly Criterion for binary prediction markets
    # p = probability of our side winning
    # b = net payout per unit risked (binary market odds)
    if direction == "YES":
        p = model_prob
        b = (1.0 - market_price) / market_price if market_price > 0 else 0.0
    else:
        p = 1.0 - model_prob
        b = market_price / (1.0 - market_price) if market_price < 1.0 else 0.0

    q = 1.0 - p

    if b <= 0:
        logger.warning(
            "signal: market %s — degenerate odds (b=%.4f, price=%.4f) — skipping",
            market.id, b, market_price,
        )
        return None

    full_kelly = (b * p - q) / b
    raw_kelly_size = max(0.0, settings.KELLY_FRACTION * full_kelly)

    logger.debug(
        "signal: market %s direction=%s edge=%.4f kelly=%.4f time_decay=%.4f",
        market.id, direction, adjusted_edge, raw_kelly_size, time_decay,
    )

    return Signal(
        market_id=market.id,
        direction=direction,
        raw_kelly_size=raw_kelly_size,
        adjusted_edge=adjusted_edge,
        model_prob=model_prob,
        market_price=market_price,
    )
