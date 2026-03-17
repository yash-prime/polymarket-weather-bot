"""
trading/risk.py — Risk Manager.

All guardrails run before any order is placed. approve() is the only
function called by the trading engine — it either returns an ApprovedSignal
or returns None (trade rejected).

Guardrails (in order checked):
  1. Kill switch — bot_halted in system_config DB
  2. Max open positions — reads positions/paper_positions DB table
  3. Daily loss limit — reads portfolio_snapshots DB table
  4. Same-event correlation — only one open position per market_id
  5. Position sizing — bankroll * kelly_fraction, capped at MAX_POSITION_USDC

All reads use the CORRECT table based on mode ("live" vs "paper").
No in-memory state — DB is the single source of truth.
"""
import logging

from config import settings
from market.models import Signal
from trading.models import ApprovedSignal

logger = logging.getLogger(__name__)


def approve(
    signal: Signal,
    mode: str,
    db_path: str | None = None,
) -> ApprovedSignal | None:
    """
    Run all risk guardrails and return an ApprovedSignal or None.

    Parameters
    ----------
    signal  : Signal produced by the Signal Engine.
    mode    : "live" | "paper" — selects which DB tables to read.
    db_path : Override DB path (used in tests).

    Returns
    -------
    ApprovedSignal if all guardrails pass, None if any check fails.
    """
    positions_table = "positions" if mode == "live" else "paper_positions"

    # --- 1. Kill switch ---
    if _is_halted(db_path):
        logger.info("risk.approve: kill switch active — rejecting signal for %s", signal.market_id)
        return None

    # --- 2. Max open positions ---
    open_count = _count_open_positions(positions_table, db_path)
    if open_count >= settings.MAX_OPEN_POSITIONS:
        logger.info(
            "risk.approve: max open positions reached (%d/%d) — rejecting %s",
            open_count, settings.MAX_OPEN_POSITIONS, signal.market_id,
        )
        return None

    # --- 2b. Capital limit — stop trading when fully deployed ---
    deployed = _get_deployed_capital(positions_table, db_path)
    _CAPITAL_LIMIT = 2500.0
    if deployed >= _CAPITAL_LIMIT:
        logger.info(
            "risk.approve: capital fully deployed (%.2f >= %.2f) — rejecting %s",
            deployed, _CAPITAL_LIMIT, signal.market_id,
        )
        return None

    # --- 3. Daily loss limit ---
    daily_loss_pct = _get_daily_loss_pct(mode, db_path)
    if daily_loss_pct >= settings.DAILY_LOSS_LIMIT_PCT:
        logger.warning(
            "risk.approve: daily loss limit breached (%.2f%% >= %.2f%%) — halting",
            daily_loss_pct * 100, settings.DAILY_LOSS_LIMIT_PCT * 100,
        )
        _set_halted(db_path)
        return None

    # --- 4. Same-event correlation (duplicate market check) ---
    if _has_open_position(signal.market_id, positions_table, db_path):
        logger.info(
            "risk.approve: already have open position in %s — skipping", signal.market_id
        )
        return None

    # --- 5. Size computation ---
    bankroll = _get_bankroll(mode, db_path)
    raw_size = bankroll * signal.raw_kelly_size
    final_size = min(raw_size, settings.MAX_POSITION_USDC)

    if final_size <= 0:
        logger.warning(
            "risk.approve: computed final_size=%.4f for %s — rejecting",
            final_size, signal.market_id,
        )
        return None

    logger.info(
        "risk.approve: APPROVED %s %s final_size=%.2f USDC (bankroll=%.2f kelly=%.4f capped=%s)",
        signal.direction, signal.market_id, final_size, bankroll,
        signal.raw_kelly_size,
        "yes" if raw_size > settings.MAX_POSITION_USDC else "no",
    )

    return ApprovedSignal(signal=signal, final_size=final_size, mode=mode)


# ---------------------------------------------------------------------------
# Internal helpers — all read from DB, no in-memory state
# ---------------------------------------------------------------------------


def _is_halted(db_path: str | None) -> bool:
    """Check the kill switch in system_config table."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'bot_halted'"
            ).fetchone()
        return row is not None and row["value"] == "true"
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk._is_halted: DB read failed: %s — assuming not halted", exc)
        return False


def _set_halted(db_path: str | None) -> None:
    """Set bot_halted=true in system_config (triggered by daily loss breach)."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO system_config (key, value, updated_at) VALUES ('bot_halted', 'true', datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value='true', updated_at=datetime('now')"
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("risk._set_halted: could not write to DB: %s", exc)


def _count_open_positions(table: str, db_path: str | None) -> int:
    """Return the count of currently open positions from the correct table."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM {table} WHERE status = 'open'"  # noqa: S608
            ).fetchone()
        return row["cnt"] if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk._count_open_positions: DB read failed: %s — assuming 0", exc)
        return 0


def _has_open_position(market_id: str, table: str, db_path: str | None) -> bool:
    """Return True if there is already an open position in this market."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT id FROM {table} WHERE market_id = ? AND status = 'open' LIMIT 1",  # noqa: S608
                (market_id,),
            ).fetchone()
        return row is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk._has_open_position: DB read failed: %s — assuming no position", exc)
        return False


def _get_daily_loss_pct(mode: str, db_path: str | None) -> float:
    """
    Read the most recent daily_loss_pct from portfolio_snapshots.

    Returns 0.0 if no snapshot exists yet (safe default — allow trading).
    """
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT daily_loss_pct FROM portfolio_snapshots "
                "WHERE mode = ? "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (mode,),
            ).fetchone()
        return float(row["daily_loss_pct"]) if row and row["daily_loss_pct"] is not None else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk._get_daily_loss_pct: DB read failed: %s — returning 0.0", exc)
        return 0.0


def _get_deployed_capital(table: str, db_path: str | None) -> float:
    """Sum of sizes of all open positions — actual capital currently at risk."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(size), 0) as deployed FROM {table} WHERE status = 'open'",  # noqa: S608
            ).fetchone()
        return float(row["deployed"]) if row else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk._get_deployed_capital: DB read failed: %s — assuming 0", exc)
        return 0.0


def _get_bankroll(mode: str, db_path: str | None) -> float:
    """
    Read current total equity from the most recent portfolio snapshot.

    Falls back to MAX_POSITION_USDC * 10 as a safe conservative estimate
    if no snapshot exists yet.
    """
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT total_equity FROM portfolio_snapshots "
                "WHERE mode = ? "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (mode,),
            ).fetchone()
        if row and row["total_equity"] and float(row["total_equity"]) > 0:
            return float(row["total_equity"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk._get_bankroll: DB read failed: %s", exc)

    # Safe default: assume 10× the max position size as minimum bankroll
    default = settings.MAX_POSITION_USDC * 10
    logger.info("risk._get_bankroll: no snapshot found — using default bankroll %.2f", default)
    return default
