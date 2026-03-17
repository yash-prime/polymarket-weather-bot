"""
trading/trader.py — Live CLOB trading engine.

Wraps the py-clob-client library to provide:
  - place_limit_order(market_id, direction, size, price) → order_id | None
  - cancel_order(order_id)
  - cancel_all_open_orders()
  - cancel_stale_orders(max_age_minutes)

All order outcomes are written to the trades DB table.
All calls: 3 retries, exponential backoff, 15s timeout.
POST_CANCEL_WAIT_SECONDS sleep after cancels to let the CLOB settle.

The CLOB client is initialised once in main.py and passed here.
This module does NOT hold global client state — it receives the client
as a parameter to all public methods (making it testable).

Security note
-------------
ClobClient is initialised with PRIVATE_KEY in main.py startup only.
This module never reads or stores PRIVATE_KEY.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings

if TYPE_CHECKING:
    pass  # ClobClient type annotation only — avoid import at module level

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def place_limit_order(
    clob_client,
    market_id: str,
    direction: str,
    size: float,
    price: float,
    db_path: str | None = None,
) -> str | None:
    """
    Place a limit order on the Polymarket CLOB.

    Parameters
    ----------
    clob_client : Initialised ClobClient instance (from main.py startup).
    market_id   : Polymarket condition ID.
    direction   : "YES" | "NO"
    size        : Dollar amount in USDC.
    price       : Limit price (0.0–1.0).
    db_path     : Override DB path (tests).

    Returns
    -------
    order_id string on success, None on failure.
    Writes outcome to trades table regardless.
    """
    order_id = None
    status = "failed"

    try:
        token_id = _resolve_token_id(clob_client, market_id, direction)
        response = _place_with_retry(clob_client, token_id, size, price)
        order_id = response.get("orderID") or response.get("id")
        status = "open"
        logger.info(
            "trader.place_limit_order: placed %s %s size=%.2f price=%.4f order_id=%s",
            direction, market_id, size, price, order_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "trader.place_limit_order: FAILED %s %s size=%.2f: %s",
            direction, market_id, size, exc,
        )

    _write_trade(market_id, direction, size, price, order_id, status, "live", db_path)
    return order_id if status == "open" else None


def cancel_order(
    clob_client,
    order_id: str,
    db_path: str | None = None,
) -> bool:
    """
    Cancel a single open order.

    Sleeps POST_CANCEL_WAIT_SECONDS after cancellation to let CLOB settle.
    Returns True on success, False on failure.
    """
    try:
        _cancel_with_retry(clob_client, order_id)
        time.sleep(settings.POST_CANCEL_WAIT_SECONDS)
        _update_trade_status(order_id, "cancelled", db_path)
        logger.info("trader.cancel_order: cancelled order %s", order_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("trader.cancel_order: failed to cancel %s: %s", order_id, exc)
        return False


def cancel_all_open_orders(
    clob_client,
    db_path: str | None = None,
) -> int:
    """
    Cancel all open orders known to the CLOB client.

    Used by the kill switch handler. Returns count of cancelled orders.
    """
    try:
        open_orders = clob_client.get_orders(params={"status": "OPEN"})
    except Exception as exc:  # noqa: BLE001
        logger.error("trader.cancel_all_open_orders: failed to fetch open orders: %s", exc)
        return 0

    cancelled = 0
    for order in (open_orders or []):
        order_id = order.get("id") or order.get("orderID")
        if order_id and cancel_order(clob_client, order_id, db_path):
            cancelled += 1

    logger.info("trader.cancel_all_open_orders: cancelled %d orders", cancelled)
    return cancelled


def cancel_stale_orders(
    clob_client,
    max_age_minutes: int | None = None,
    db_path: str | None = None,
) -> int:
    """
    Cancel orders that have been open longer than max_age_minutes.

    Reads stale orders from the DB trades table, then cancels them via CLOB.
    Returns count of cancelled orders.
    """
    max_age = max_age_minutes or settings.STALE_ORDER_MAX_AGE_MIN
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age)

    stale_order_ids = _get_stale_order_ids(cutoff, db_path)
    if not stale_order_ids:
        return 0

    logger.info(
        "trader.cancel_stale_orders: found %d stale orders (older than %d min)",
        len(stale_order_ids), max_age,
    )

    cancelled = 0
    for order_id in stale_order_ids:
        if cancel_order(clob_client, order_id, db_path):
            cancelled += 1

    return cancelled


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_token_id(clob_client, market_id: str, direction: str) -> str:
    """
    Get the YES or NO token ID for a market condition.

    CLOB orders are placed on token IDs, not condition IDs.
    """
    market = clob_client.get_market(market_id)
    tokens = market.get("tokens", [])
    for token in tokens:
        outcome = token.get("outcome", "").upper()
        if outcome == direction.upper():
            return token["token_id"]
    raise ValueError(
        f"Could not find {direction} token for market {market_id}. "
        f"Tokens: {tokens}"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _place_with_retry(clob_client, token_id: str, size: float, price: float) -> dict:
    """Place a GTC limit order with retry."""
    from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore[import]

    order = clob_client.create_order(
        OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",  # We always BUY the outcome token (YES or NO)
        )
    )
    return clob_client.post_order(order, OrderType.GTC)


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=5, max=15),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _cancel_with_retry(clob_client, order_id: str) -> None:
    """Cancel an order with one extra retry and longer timeout."""
    clob_client.cancel(order_id)


def _write_trade(
    market_id: str,
    direction: str,
    size: float,
    price: float,
    order_id: str | None,
    status: str,
    mode: str,
    db_path: str | None,
) -> None:
    """Write trade outcome to the trades table."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO trades (market_id, direction, final_size, price, order_id, status, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (market_id, direction, size, price, order_id, status, mode),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("trader._write_trade: DB write failed: %s", exc)


def _update_trade_status(order_id: str, status: str, db_path: str | None) -> None:
    """Update the status of an existing trade record."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE trades SET status = ?, closed_at = datetime('now') WHERE order_id = ?",
                (status, order_id),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("trader._update_trade_status: DB write failed: %s", exc)


def _get_stale_order_ids(cutoff: datetime, db_path: str | None) -> list[str]:
    """Read open order IDs from DB that were created before the cutoff."""
    try:
        from db.init import get_connection

        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT order_id FROM trades "
                "WHERE status = 'open' AND order_id IS NOT NULL "
                "  AND created_at < ?",
                (cutoff_str,),
            ).fetchall()
        return [row["order_id"] for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("trader._get_stale_order_ids: DB read failed: %s", exc)
        return []
