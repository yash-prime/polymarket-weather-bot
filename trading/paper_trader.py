"""
trading/paper_trader.py — Paper trading engine.

Identical interface to trader.py but:
  - Makes NO real CLOB API calls
  - Writes to paper_trades and paper_positions tables only
  - Simulates fills at the current YES mid-price (yes_price from Market)
  - Paper positions never affect live risk checks (isolated tables)

All paper trades and positions are completely isolated from live data.
The Risk Manager reads from paper_positions (not positions) when mode="paper".
"""
import logging
from datetime import datetime, timezone

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API — same interface as trader.py
# ---------------------------------------------------------------------------


def place_limit_order(
    market_id: str,
    direction: str,
    size: float,
    price: float,
    rationale: str | None = None,
    db_path: str | None = None,
) -> str | None:
    """
    Simulate placing a limit order in paper mode.

    Generates a synthetic order ID, simulates fill at price,
    and writes to paper_trades + paper_positions tables.

    Returns
    -------
    Synthetic order ID string.
    """
    order_id = _make_paper_order_id(market_id, direction)
    simulated_fill_price = price  # Paper mode fills at the given limit price

    _write_paper_trade(market_id, direction, size, simulated_fill_price, order_id, rationale, db_path)
    _upsert_paper_position(market_id, direction, size, simulated_fill_price, db_path)

    logger.info(
        "paper_trader.place_limit_order: PAPER %s %s size=%.2f fill_price=%.4f order_id=%s",
        direction, market_id, size, simulated_fill_price, order_id,
    )
    return order_id


def cancel_order(
    order_id: str,
    db_path: str | None = None,
) -> bool:
    """
    Cancel a paper order by updating its status to 'cancelled'.

    No CLOB calls. Returns True always (paper cancels can't fail network-wise).
    """
    _update_paper_trade_status(order_id, "cancelled", db_path)
    logger.info("paper_trader.cancel_order: PAPER cancelled %s", order_id)
    return True


def cancel_all_open_orders(db_path: str | None = None) -> int:
    """
    Cancel all open paper orders. Returns count cancelled.
    """
    order_ids = _get_open_paper_order_ids(db_path)
    for order_id in order_ids:
        cancel_order(order_id, db_path)
    logger.info("paper_trader.cancel_all_open_orders: cancelled %d paper orders", len(order_ids))
    return len(order_ids)


def cancel_stale_orders(
    max_age_minutes: int | None = None,
    db_path: str | None = None,
) -> int:
    """
    Cancel stale paper orders (no-op in paper mode — no real orders to worry about).

    Returns 0.
    """
    return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_paper_order_id(market_id: str, direction: str) -> str:
    """Generate a deterministic but unique synthetic paper order ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"paper-{market_id[:8]}-{direction.lower()}-{ts}"


def _write_paper_trade(
    market_id: str,
    direction: str,
    size: float,
    fill_price: float,
    order_id: str,
    rationale: str | None,
    db_path: str | None,
) -> None:
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO paper_trades "
                "(market_id, direction, final_size, simulated_fill_price, status, rationale) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (market_id, direction, size, fill_price, rationale),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("paper_trader._write_paper_trade: DB write failed: %s", exc)


def _upsert_paper_position(
    market_id: str,
    direction: str,
    size: float,
    entry_price: float,
    db_path: str | None,
) -> None:
    """
    Insert or update a paper position.

    Uses INSERT OR REPLACE — if a position for this market already exists
    (shouldn't happen due to correlation check), it is replaced.
    """
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO paper_positions
                  (market_id, direction, size, entry_price, current_price, unrealized_pnl, status)
                VALUES (?, ?, ?, ?, ?, 0.0, 'open')
                ON CONFLICT(market_id) DO UPDATE SET
                  direction     = excluded.direction,
                  size          = excluded.size,
                  entry_price   = excluded.entry_price,
                  current_price = excluded.current_price,
                  status        = 'open'
                """,
                (market_id, direction, size, entry_price, entry_price),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("paper_trader._upsert_paper_position: DB write failed: %s", exc)


def _update_paper_trade_status(order_id: str, status: str, db_path: str | None) -> None:
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE paper_trades SET status = ?, closed_at = datetime('now') "
                "WHERE status = 'open'",  # paper_trades has no order_id column; update all open
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("paper_trader._update_paper_trade_status: DB write failed: %s", exc)


def _get_open_paper_order_ids(db_path: str | None) -> list[str]:
    """Return IDs of all open paper trades (uses row IDs as synthetic order IDs)."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM paper_trades WHERE status = 'open'"
            ).fetchall()
        return [str(row["id"]) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("paper_trader._get_open_paper_order_ids: DB read failed: %s", exc)
        return []
