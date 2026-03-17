"""
tests/test_risk.py — Unit tests for trading/risk.py

Covers all rejection conditions per ARCHITECTURE.md:
  - Kill switch active → reject
  - Max open positions reached → reject
  - Daily loss limit breached → reject + auto-halt
  - Duplicate market (correlation) → reject
  - Approved: returns ApprovedSignal with correct final_size
  - Size clamping: final_size <= MAX_POSITION_USDC
  - Paper vs live table isolation: paper positions don't affect live checks
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from config import settings
from market.models import Signal
from trading.models import ApprovedSignal
from trading.risk import approve


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(
    market_id: str = "mkt-1",
    direction: str = "YES",
    raw_kelly_size: float = 0.05,
    adjusted_edge: float = 0.12,
) -> Signal:
    return Signal(
        market_id=market_id,
        direction=direction,
        raw_kelly_size=raw_kelly_size,
        adjusted_edge=adjusted_edge,
        model_prob=0.65,
        market_price=0.40,
    )


def _seed_portfolio_snapshot(db_path, mode, total_equity=1000.0, daily_loss_pct=0.0):
    from db.init import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(mode, total_equity, unrealized_pnl, realized_pnl, daily_pnl, daily_loss_pct, open_positions) "
            "VALUES (?, ?, 0, 0, 0, ?, 0)",
            (mode, total_equity, daily_loss_pct),
        )
        conn.commit()


def _seed_position(db_path, table, market_id="mkt-1", status="open"):
    from db.init import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            f"INSERT INTO {table} (market_id, direction, size, entry_price, current_price, unrealized_pnl, status) "
            "VALUES (?, 'YES', 10.0, 0.4, 0.4, 0.0, ?)",
            (market_id, status),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Rejection: kill switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_halted_rejects_signal(self, tmp_db_path):
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "UPDATE system_config SET value='true' WHERE key='bot_halted'"
            )
            conn.commit()

        result = approve(_signal(), mode="paper", db_path=tmp_db_path)
        assert result is None

    def test_not_halted_proceeds(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        result = approve(_signal(), mode="paper", db_path=tmp_db_path)
        assert result is not None


# ---------------------------------------------------------------------------
# Rejection: max open positions
# ---------------------------------------------------------------------------


class TestMaxOpenPositions:
    def test_rejects_when_at_max_positions(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        # Fill paper_positions up to MAX_OPEN_POSITIONS
        for i in range(settings.MAX_OPEN_POSITIONS):
            _seed_position(tmp_db_path, "paper_positions", market_id=f"mkt-filler-{i}")

        result = approve(_signal(market_id="mkt-new"), mode="paper", db_path=tmp_db_path)
        assert result is None

    def test_allows_when_below_max_positions(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        # One position below max
        for i in range(settings.MAX_OPEN_POSITIONS - 1):
            _seed_position(tmp_db_path, "paper_positions", market_id=f"mkt-filler-{i}")

        result = approve(_signal(market_id="mkt-new"), mode="paper", db_path=tmp_db_path)
        assert result is not None


# ---------------------------------------------------------------------------
# Rejection: daily loss limit
# ---------------------------------------------------------------------------


class TestDailyLossLimit:
    def test_rejects_when_daily_loss_limit_breached(self, tmp_db_path):
        # Set daily loss at or above limit
        _seed_portfolio_snapshot(tmp_db_path, "paper", daily_loss_pct=settings.DAILY_LOSS_LIMIT_PCT)

        result = approve(_signal(), mode="paper", db_path=tmp_db_path)
        assert result is None

    def test_sets_bot_halted_on_loss_limit(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper", daily_loss_pct=settings.DAILY_LOSS_LIMIT_PCT)
        approve(_signal(), mode="paper", db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key='bot_halted'"
            ).fetchone()
        assert row["value"] == "true"

    def test_allows_when_below_daily_loss_limit(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper", daily_loss_pct=0.05)
        result = approve(_signal(), mode="paper", db_path=tmp_db_path)
        assert result is not None


# ---------------------------------------------------------------------------
# Rejection: correlation (duplicate market)
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_rejects_duplicate_market(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        _seed_position(tmp_db_path, "paper_positions", market_id="mkt-1")

        result = approve(_signal(market_id="mkt-1"), mode="paper", db_path=tmp_db_path)
        assert result is None

    def test_allows_different_market(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        _seed_position(tmp_db_path, "paper_positions", market_id="mkt-1")

        result = approve(_signal(market_id="mkt-2"), mode="paper", db_path=tmp_db_path)
        assert result is not None

    def test_closed_position_does_not_block(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        _seed_position(tmp_db_path, "paper_positions", market_id="mkt-1", status="closed")

        result = approve(_signal(market_id="mkt-1"), mode="paper", db_path=tmp_db_path)
        assert result is not None


# ---------------------------------------------------------------------------
# Size clamping
# ---------------------------------------------------------------------------


class TestSizeClamping:
    def test_final_size_capped_at_max_position(self, tmp_db_path):
        """raw_size > MAX_POSITION_USDC → final_size == MAX_POSITION_USDC."""
        # Bankroll = 1000, kelly = 0.20 → raw_size = 200 > 50
        _seed_portfolio_snapshot(tmp_db_path, "paper", total_equity=1000.0)
        result = approve(_signal(raw_kelly_size=0.20), mode="paper", db_path=tmp_db_path)
        assert result is not None
        assert result.final_size == pytest.approx(settings.MAX_POSITION_USDC)

    def test_final_size_not_capped_when_below_max(self, tmp_db_path):
        """raw_size < MAX_POSITION_USDC → final_size == raw_size."""
        # Bankroll = 1000, kelly = 0.02 → raw_size = 20 < 50
        _seed_portfolio_snapshot(tmp_db_path, "paper", total_equity=1000.0)
        result = approve(_signal(raw_kelly_size=0.02), mode="paper", db_path=tmp_db_path)
        assert result is not None
        assert result.final_size == pytest.approx(1000.0 * 0.02)

    def test_final_size_never_exceeds_max_position_usdc(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper", total_equity=100000.0)
        result = approve(_signal(raw_kelly_size=0.5), mode="paper", db_path=tmp_db_path)
        assert result is not None
        assert result.final_size <= settings.MAX_POSITION_USDC


# ---------------------------------------------------------------------------
# Paper vs live isolation
# ---------------------------------------------------------------------------


class TestPaperLiveIsolation:
    def test_paper_positions_do_not_affect_live_count(self, tmp_db_path):
        """Filling paper_positions should not block live mode trades."""
        _seed_portfolio_snapshot(tmp_db_path, "live")
        # Fill paper positions to max
        for i in range(settings.MAX_OPEN_POSITIONS):
            _seed_position(tmp_db_path, "paper_positions", market_id=f"paper-{i}")

        result = approve(_signal(market_id="mkt-live-new"), mode="live", db_path=tmp_db_path)
        assert result is not None

    def test_live_positions_do_not_affect_paper_count(self, tmp_db_path):
        """Filling live positions should not block paper mode trades."""
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        for i in range(settings.MAX_OPEN_POSITIONS):
            _seed_position(tmp_db_path, "positions", market_id=f"live-{i}")

        result = approve(_signal(market_id="mkt-paper-new"), mode="paper", db_path=tmp_db_path)
        assert result is not None

    def test_approved_signal_mode_matches_input(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        result = approve(_signal(), mode="paper", db_path=tmp_db_path)
        assert result is not None
        assert result.mode == "paper"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_approved_signal_has_signal_attached(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        sig = _signal()
        result = approve(sig, mode="paper", db_path=tmp_db_path)
        assert result is not None
        assert result.signal is sig

    def test_approved_signal_final_size_is_float(self, tmp_db_path):
        _seed_portfolio_snapshot(tmp_db_path, "paper")
        result = approve(_signal(), mode="paper", db_path=tmp_db_path)
        assert isinstance(result.final_size, float)
        assert result.final_size > 0
