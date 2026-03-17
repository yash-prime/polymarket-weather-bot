"""
trading/portfolio.py — P&L tracking and portfolio snapshots.

Computes:
  - Total equity (open position value + unrealized P&L)
  - Unrealized P&L (mark-to-market via most recent yes_price from markets DB)
  - Realized P&L (sum of closed trades)
  - Daily P&L (since midnight UTC)
  - Daily loss percentage

Writes snapshots to portfolio_snapshots every 5 minutes (APScheduler job).
Reads the correct tables based on mode: "live" or "paper".

get_snapshot(mode) is the public API.
job_portfolio_snapshot() is called by APScheduler.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class PortfolioSnapshot:
    """Current portfolio state."""
    mode: str
    total_equity: float
    unrealized_pnl: float
    realized_pnl: float
    daily_pnl: float
    daily_loss_pct: float
    open_positions: int
    snapshot_at: str  # ISO timestamp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_snapshot(mode: str, db_path: str | None = None) -> PortfolioSnapshot:
    """
    Compute and return a current portfolio snapshot.

    Parameters
    ----------
    mode    : "live" | "paper"
    db_path : Override DB path (tests).

    Returns
    -------
    PortfolioSnapshot with all P&L figures.
    """
    positions_table = "positions" if mode == "live" else "paper_positions"
    trades_table = "trades" if mode == "live" else "paper_trades"

    open_positions, unrealized_pnl = _compute_unrealized(positions_table, mode, db_path)
    realized_pnl = _compute_realized(trades_table, db_path)
    daily_pnl = _compute_daily_pnl(trades_table, db_path)
    total_equity = _compute_total_equity(unrealized_pnl, realized_pnl, mode, db_path)
    daily_loss_pct = _compute_daily_loss_pct(daily_pnl, total_equity)

    return PortfolioSnapshot(
        mode=mode,
        total_equity=total_equity,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=realized_pnl,
        daily_pnl=daily_pnl,
        daily_loss_pct=daily_loss_pct,
        open_positions=open_positions,
        snapshot_at=datetime.now(timezone.utc).isoformat(),
    )


def job_portfolio_snapshot(mode: str = "paper", db_path: str | None = None) -> None:
    """
    APScheduler job: compute and persist a portfolio snapshot every 5 minutes.
    """
    try:
        snapshot = get_snapshot(mode, db_path)
        _write_snapshot(snapshot, db_path)
        logger.info(
            "portfolio.job_portfolio_snapshot: mode=%s equity=%.2f unrealized=%.2f "
            "realized=%.2f daily=%.2f open=%d",
            mode, snapshot.total_equity, snapshot.unrealized_pnl,
            snapshot.realized_pnl, snapshot.daily_pnl, snapshot.open_positions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("portfolio.job_portfolio_snapshot: failed: %s", exc)


# ---------------------------------------------------------------------------
# Internal computation helpers
# ---------------------------------------------------------------------------


def _compute_unrealized(
    positions_table: str, mode: str, db_path: str | None
) -> tuple[int, float]:
    """
    Compute total unrealized P&L across all open positions.

    Mark-to-market: uses the most recent yes_price from markets DB.
    Returns (open_position_count, total_unrealized_pnl).
    """
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT p.market_id, p.direction, p.size, p.entry_price, m.yes_price
                FROM {positions_table} p
                LEFT JOIN markets m ON m.id = p.market_id
                WHERE p.status = 'open'
                """,  # noqa: S608
            ).fetchall()

        if not rows:
            return 0, 0.0

        total_unrealized = 0.0
        for row in rows:
            current_price = row["yes_price"] if row["yes_price"] is not None else row["entry_price"]
            direction = row["direction"]
            size = float(row["size"])
            entry = float(row["entry_price"])

            # P&L = size * (current_price / entry - 1) for YES
            #     = size * ((1 - current_price) / (1 - entry) - 1) for NO
            if direction == "YES":
                pnl = size * (current_price / entry - 1) if entry != 0 else 0.0
            else:
                no_entry = 1.0 - entry
                no_current = 1.0 - current_price
                pnl = size * (no_current / no_entry - 1) if no_entry != 0 else 0.0

            total_unrealized += pnl

        return len(rows), round(total_unrealized, 4)

    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio._compute_unrealized: failed: %s", exc)
        return 0, 0.0


def _compute_realized(trades_table: str, db_path: str | None) -> float:
    """Sum of realized_pnl from all settled (filled) positions."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                f"""
                SELECT SUM(realized_pnl) as realized
                FROM {trades_table}
                WHERE status = 'filled'
                """,  # noqa: S608
            ).fetchone()
        return float(row["realized"] or 0.0)

    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio._compute_realized: failed: %s", exc)
        return 0.0


def _compute_daily_pnl(trades_table: str, db_path: str | None) -> float:
    """Realized P&L from trades closed today (open positions are not losses yet)."""
    try:
        from db.init import get_connection

        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
        with get_connection(db_path) as conn:
            row = conn.execute(
                f"""
                SELECT SUM(
                    CASE WHEN status = 'filled'
                         THEN final_size * (simulated_fill_price - 1.0)
                         ELSE 0.0 END
                ) as realized_today
                FROM {trades_table}
                WHERE closed_at >= ?
                """,  # noqa: S608
                (today_start,),
            ).fetchone()
        return float(row["realized_today"] or 0)

    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio._compute_daily_pnl: failed: %s", exc)
        return 0.0


def _compute_total_equity(
    unrealized_pnl: float,
    realized_pnl: float,
    mode: str,
    db_path: str | None,
) -> float:
    """
    Total equity = STARTING_CAPITAL + realized P&L + unrealized P&L.

    Paper trading always starts at $100.
    """
    STARTING_CAPITAL = 2500.0
    return STARTING_CAPITAL + realized_pnl + unrealized_pnl


def _compute_daily_loss_pct(daily_pnl: float, total_equity: float) -> float:
    """
    Daily loss as a fraction of total equity.

    Returns 0.0 if equity is 0 or daily_pnl is positive (gain, not loss).
    """
    if total_equity <= 0:
        return 0.0
    loss = max(0.0, -daily_pnl)  # Only count losses, not gains
    return round(loss / total_equity, 6)


def _write_snapshot(snapshot: PortfolioSnapshot, db_path: str | None) -> None:
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(mode, total_equity, unrealized_pnl, realized_pnl, daily_pnl, daily_loss_pct, open_positions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.mode,
                    snapshot.total_equity,
                    snapshot.unrealized_pnl,
                    snapshot.realized_pnl,
                    snapshot.daily_pnl,
                    snapshot.daily_loss_pct,
                    snapshot.open_positions,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("portfolio._write_snapshot: DB write failed: %s", exc)
