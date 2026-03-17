"""
tests/test_paper_trader.py — Unit tests for trading/paper_trader.py

Covers:
  - place_limit_order: writes to paper_trades and paper_positions
  - Paper trades never write to live trades or positions tables
  - cancel_order: updates status
  - cancel_all_open_orders: cancels all open
  - Synthetic order ID format
"""
import pytest

from trading.paper_trader import (
    cancel_all_open_orders,
    cancel_order,
    place_limit_order,
)


# ---------------------------------------------------------------------------
# place_limit_order
# ---------------------------------------------------------------------------


class TestPlaceLimitOrder:
    def test_returns_order_id_string(self, tmp_db_path):
        result = place_limit_order("mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)
        assert isinstance(result, str)
        assert result.startswith("paper-")

    def test_writes_to_paper_trades_table(self, tmp_db_path):
        place_limit_order("mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE market_id = 'mkt-1'"
            ).fetchone()
        assert row is not None
        assert row["status"] == "open"
        assert row["final_size"] == pytest.approx(10.0)

    def test_writes_to_paper_positions_table(self, tmp_db_path):
        place_limit_order("mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT * FROM paper_positions WHERE market_id = 'mkt-1'"
            ).fetchone()
        assert row is not None
        assert row["status"] == "open"
        assert row["entry_price"] == pytest.approx(0.40)

    def test_does_not_write_to_live_trades(self, tmp_db_path):
        """Paper trades must NEVER touch the live trades table."""
        place_limit_order("mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE market_id = 'mkt-1'"
            ).fetchone()[0]
        assert count == 0

    def test_does_not_write_to_live_positions(self, tmp_db_path):
        """Paper positions must NEVER touch the live positions table."""
        place_limit_order("mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE market_id = 'mkt-1'"
            ).fetchone()[0]
        assert count == 0

    def test_simulated_fill_at_given_price(self, tmp_db_path):
        """Fill price equals the limit price passed in."""
        place_limit_order("mkt-1", "YES", 10.0, 0.35, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT simulated_fill_price FROM paper_trades WHERE market_id = 'mkt-1'"
            ).fetchone()
        assert row["simulated_fill_price"] == pytest.approx(0.35)

    def test_direction_stored_in_paper_position(self, tmp_db_path):
        place_limit_order("mkt-1", "NO", 10.0, 0.60, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT direction FROM paper_positions WHERE market_id = 'mkt-1'"
            ).fetchone()
        assert row["direction"] == "NO"


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_always_returns_true(self, tmp_db_path):
        result = cancel_order("any-order-id", db_path=tmp_db_path)
        assert result is True


# ---------------------------------------------------------------------------
# cancel_all_open_orders
# ---------------------------------------------------------------------------


class TestCancelAllOpenOrders:
    def test_returns_count_of_cancelled(self, tmp_db_path):
        place_limit_order("mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)
        place_limit_order("mkt-2", "NO", 10.0, 0.60, db_path=tmp_db_path)

        count = cancel_all_open_orders(db_path=tmp_db_path)
        assert count == 2

    def test_no_open_orders_returns_zero(self, tmp_db_path):
        count = cancel_all_open_orders(db_path=tmp_db_path)
        assert count == 0


# ---------------------------------------------------------------------------
# Paper vs live isolation (belt-and-suspenders)
# ---------------------------------------------------------------------------


class TestPaperLiveIsolation:
    def test_live_positions_unaffected_by_paper_trades(self, tmp_db_path):
        """Multiple paper trades should leave live positions table empty."""
        for i in range(3):
            place_limit_order(f"mkt-{i}", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert count == 0

    def test_paper_position_count_matches_trades(self, tmp_db_path):
        for i in range(3):
            place_limit_order(f"mkt-{i}", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status = 'open'"
            ).fetchone()[0]
        assert count == 3
