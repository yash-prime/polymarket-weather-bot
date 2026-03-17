"""
tests/test_portfolio.py — Unit tests for trading/portfolio.py

Covers:
  - get_snapshot returns a PortfolioSnapshot dataclass
  - _compute_unrealized: open positions with mark-to-market P&L
  - _compute_unrealized: no open positions returns (0, 0.0)
  - _compute_daily_loss_pct: zero equity, positive pnl, negative pnl
  - job_portfolio_snapshot: writes a row to portfolio_snapshots
  - Paper vs live table isolation
"""
import pytest

from trading.portfolio import (
    PortfolioSnapshot,
    _compute_daily_loss_pct,
    _compute_unrealized,
    get_snapshot,
    job_portfolio_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_open_position(db_path, market_id, direction, size, entry_price, yes_price=None):
    """Insert an open position and optionally update the markets table yes_price."""
    from db.init import get_connection

    with get_connection(db_path) as conn:
        # Ensure a markets row exists so the JOIN returns yes_price
        conn.execute(
            "INSERT OR IGNORE INTO markets (id, question, yes_price, parse_status) "
            "VALUES (?, 'Test?', ?, 'success')",
            (market_id, yes_price if yes_price is not None else entry_price),
        )
        conn.execute(
            "INSERT INTO positions (market_id, direction, size, entry_price, status) "
            "VALUES (?, ?, ?, ?, 'open')",
            (market_id, direction, size, entry_price),
        )
        conn.commit()


def _insert_open_paper_position(db_path, market_id, direction, size, entry_price, yes_price=None):
    from db.init import get_connection

    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO markets (id, question, yes_price, parse_status) "
            "VALUES (?, 'Test?', ?, 'success')",
            (market_id, yes_price if yes_price is not None else entry_price),
        )
        conn.execute(
            "INSERT INTO paper_positions (market_id, direction, size, entry_price, status) "
            "VALUES (?, ?, ?, ?, 'open')",
            (market_id, direction, size, entry_price),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    def test_returns_portfolio_snapshot(self, tmp_db_path):
        result = get_snapshot("paper", db_path=tmp_db_path)
        assert isinstance(result, PortfolioSnapshot)

    def test_mode_field_set_correctly(self, tmp_db_path):
        result = get_snapshot("paper", db_path=tmp_db_path)
        assert result.mode == "paper"

    def test_live_mode_field(self, tmp_db_path):
        result = get_snapshot("live", db_path=tmp_db_path)
        assert result.mode == "live"

    def test_snapshot_at_is_iso_string(self, tmp_db_path):
        result = get_snapshot("paper", db_path=tmp_db_path)
        # Should parse as a valid datetime
        from datetime import datetime
        dt = datetime.fromisoformat(result.snapshot_at)
        assert dt is not None

    def test_zero_open_positions_when_no_trades(self, tmp_db_path):
        result = get_snapshot("paper", db_path=tmp_db_path)
        assert result.open_positions == 0

    def test_open_positions_counted(self, tmp_db_path):
        _insert_open_paper_position(tmp_db_path, "m-1", "YES", 10.0, 0.40)
        _insert_open_paper_position(tmp_db_path, "m-2", "YES", 10.0, 0.50)
        result = get_snapshot("paper", db_path=tmp_db_path)
        assert result.open_positions == 2


# ---------------------------------------------------------------------------
# _compute_unrealized
# ---------------------------------------------------------------------------


class TestComputeUnrealized:
    def test_no_positions_returns_zero(self, tmp_db_path):
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 0
        assert pnl == 0.0

    def test_yes_position_profit(self, tmp_db_path):
        """YES position profits when current price > entry price."""
        _insert_open_position(tmp_db_path, "m-1", "YES", 100.0, 0.40, yes_price=0.50)
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 1
        # P&L = 100 * (0.50 - 0.40) = 10.0
        assert pnl == pytest.approx(10.0, abs=1e-3)

    def test_yes_position_loss(self, tmp_db_path):
        """YES position loses when current price < entry price."""
        _insert_open_position(tmp_db_path, "m-1", "YES", 100.0, 0.60, yes_price=0.40)
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 1
        # P&L = 100 * (0.40 - 0.60) = -20.0
        assert pnl == pytest.approx(-20.0, abs=1e-3)

    def test_no_position_profit(self, tmp_db_path):
        """NO position profits when current price < entry price (short YES)."""
        _insert_open_position(tmp_db_path, "m-1", "NO", 100.0, 0.60, yes_price=0.40)
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 1
        # P&L = 100 * (0.60 - 0.40) = 20.0
        assert pnl == pytest.approx(20.0, abs=1e-3)

    def test_no_position_loss(self, tmp_db_path):
        """NO position loses when current price > entry price."""
        _insert_open_position(tmp_db_path, "m-1", "NO", 100.0, 0.40, yes_price=0.60)
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 1
        # P&L = 100 * (0.40 - 0.60) = -20.0
        assert pnl == pytest.approx(-20.0, abs=1e-3)

    def test_multiple_positions_aggregated(self, tmp_db_path):
        """Multiple positions have their P&L summed."""
        _insert_open_position(tmp_db_path, "m-1", "YES", 100.0, 0.40, yes_price=0.50)
        _insert_open_position(tmp_db_path, "m-2", "YES", 100.0, 0.50, yes_price=0.50)
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 2
        # m-1: +10, m-2: 0 → total = 10
        assert pnl == pytest.approx(10.0, abs=1e-3)

    def test_paper_positions_use_paper_table(self, tmp_db_path):
        """Paper positions only read from paper_positions, not positions."""
        _insert_open_paper_position(tmp_db_path, "m-1", "YES", 50.0, 0.30, yes_price=0.50)
        count_live, _ = _compute_unrealized("positions", "live", tmp_db_path)
        count_paper, pnl_paper = _compute_unrealized("paper_positions", "paper", tmp_db_path)
        assert count_live == 0
        assert count_paper == 1
        assert pnl_paper == pytest.approx(10.0, abs=1e-3)  # 50 * (0.50 - 0.30)

    def test_yes_price_fallback_to_entry_price(self, tmp_db_path):
        """When yes_price is NULL in markets, falls back to entry_price (zero P&L)."""
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO positions (market_id, direction, size, entry_price, status) "
                "VALUES ('m-orphan', 'YES', 100.0, 0.50, 'open')"
            )
            conn.commit()
        # No markets row → yes_price is NULL → falls back to entry_price → pnl=0
        count, pnl = _compute_unrealized("positions", "live", tmp_db_path)
        assert count == 1
        assert pnl == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# _compute_daily_loss_pct
# ---------------------------------------------------------------------------


class TestComputeDailyLossPct:
    def test_zero_equity_returns_zero(self):
        assert _compute_daily_loss_pct(-100.0, 0.0) == 0.0

    def test_positive_pnl_returns_zero(self):
        """Gains are not counted as losses."""
        assert _compute_daily_loss_pct(50.0, 1000.0) == 0.0

    def test_negative_pnl_computes_fraction(self):
        result = _compute_daily_loss_pct(-100.0, 1000.0)
        assert result == pytest.approx(0.1, abs=1e-6)

    def test_full_equity_loss(self):
        result = _compute_daily_loss_pct(-1000.0, 1000.0)
        assert result == pytest.approx(1.0, abs=1e-6)

    def test_small_loss(self):
        result = _compute_daily_loss_pct(-5.0, 1000.0)
        assert result == pytest.approx(0.005, abs=1e-6)


# ---------------------------------------------------------------------------
# job_portfolio_snapshot
# ---------------------------------------------------------------------------


class TestJobPortfolioSnapshot:
    def test_writes_row_to_portfolio_snapshots(self, tmp_db_path):
        job_portfolio_snapshot(mode="paper", db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE mode = 'paper'"
            ).fetchone()
        assert row is not None

    def test_snapshot_mode_stored_correctly(self, tmp_db_path):
        job_portfolio_snapshot(mode="paper", db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT mode FROM portfolio_snapshots LIMIT 1"
            ).fetchone()
        assert row["mode"] == "paper"

    def test_multiple_snapshots_accumulate(self, tmp_db_path):
        job_portfolio_snapshot(mode="paper", db_path=tmp_db_path)
        job_portfolio_snapshot(mode="paper", db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM portfolio_snapshots"
            ).fetchone()[0]
        assert count == 2

    def test_does_not_raise_on_empty_db(self, tmp_db_path):
        """Should succeed even with no positions or trades."""
        job_portfolio_snapshot(mode="paper", db_path=tmp_db_path)  # no exception


# ---------------------------------------------------------------------------
# Paper vs live isolation
# ---------------------------------------------------------------------------


class TestPaperLiveIsolation:
    def test_live_snapshot_reads_live_positions(self, tmp_db_path):
        _insert_open_position(tmp_db_path, "m-live", "YES", 50.0, 0.40, yes_price=0.60)
        live_snap = get_snapshot("live", db_path=tmp_db_path)
        paper_snap = get_snapshot("paper", db_path=tmp_db_path)
        assert live_snap.open_positions == 1
        assert paper_snap.open_positions == 0

    def test_paper_snapshot_reads_paper_positions(self, tmp_db_path):
        _insert_open_paper_position(tmp_db_path, "m-paper", "YES", 50.0, 0.40, yes_price=0.60)
        live_snap = get_snapshot("live", db_path=tmp_db_path)
        paper_snap = get_snapshot("paper", db_path=tmp_db_path)
        assert live_snap.open_positions == 0
        assert paper_snap.open_positions == 1
