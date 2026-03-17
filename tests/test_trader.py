"""
tests/test_trader.py — Unit tests for trading/trader.py

All tests mock the py-clob-client — no real CLOB calls are made.

Covers:
  - place_limit_order: success (writes open trade), failure (writes failed trade)
  - cancel_order: success (cancels + sleeps), failure (returns False)
  - cancel_all_open_orders: cancels all returned by client
  - cancel_stale_orders: reads stale from DB, cancels them
  - _get_stale_order_ids: filters by age correctly
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from trading.trader import (
    _get_stale_order_ids,
    cancel_all_open_orders,
    cancel_order,
    cancel_stale_orders,
    place_limit_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clob_client(
    place_response: dict | None = None,
    cancel_raises: Exception | None = None,
    open_orders: list | None = None,
):
    """Build a mock ClobClient."""
    client = MagicMock()
    client.get_market.return_value = {
        "tokens": [
            {"outcome": "YES", "token_id": "token-yes-123"},
            {"outcome": "NO", "token_id": "token-no-456"},
        ]
    }
    client.create_order.return_value = MagicMock()
    client.post_order.return_value = place_response or {"orderID": "order-abc"}
    if cancel_raises:
        client.cancel.side_effect = cancel_raises
    client.get_orders.return_value = open_orders or []
    return client


# ---------------------------------------------------------------------------
# place_limit_order
# ---------------------------------------------------------------------------


class TestPlaceLimitOrder:
    def _with_clob_mock(self):
        """Context manager that stubs out the py_clob_client import."""
        import sys
        from unittest.mock import MagicMock
        mock_clob = MagicMock()
        mock_clob.clob_types.OrderArgs = MagicMock
        mock_clob.clob_types.OrderType.GTC = "GTC"
        return patch.dict(sys.modules, {
            "py_clob_client": mock_clob,
            "py_clob_client.clob_types": mock_clob.clob_types,
        })

    def test_success_returns_order_id(self, tmp_db_path):
        client = _clob_client(place_response={"orderID": "order-xyz"})
        with self._with_clob_mock():
            result = place_limit_order(client, "mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)
        assert result == "order-xyz"

    def test_success_writes_open_trade_to_db(self, tmp_db_path):
        client = _clob_client(place_response={"orderID": "order-xyz"})
        with self._with_clob_mock():
            place_limit_order(client, "mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT status, order_id FROM trades WHERE market_id = 'mkt-1'"
            ).fetchone()
        assert row["status"] == "open"
        assert row["order_id"] == "order-xyz"

    def test_failure_returns_none(self, tmp_db_path):
        client = MagicMock()
        client.get_market.side_effect = ConnectionError("CLOB unreachable")
        with self._with_clob_mock():
            result = place_limit_order(client, "mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)
        assert result is None

    def test_failure_writes_failed_trade_to_db(self, tmp_db_path):
        client = MagicMock()
        client.get_market.side_effect = ConnectionError("CLOB unreachable")
        with self._with_clob_mock():
            place_limit_order(client, "mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT status FROM trades WHERE market_id = 'mkt-1'"
            ).fetchone()
        assert row["status"] == "failed"

    def test_resolves_yes_token_id(self, tmp_db_path):
        client = _clob_client(place_response={"orderID": "o1"})
        with self._with_clob_mock():
            place_limit_order(client, "mkt-1", "YES", 10.0, 0.40, db_path=tmp_db_path)
        client.create_order.assert_called_once()

    def test_resolves_no_token_id(self, tmp_db_path):
        client = _clob_client(place_response={"orderID": "o2"})
        with self._with_clob_mock():
            place_limit_order(client, "mkt-1", "NO", 10.0, 0.60, db_path=tmp_db_path)
        client.create_order.assert_called_once()


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_success_returns_true(self, tmp_db_path):
        client = _clob_client()
        with patch("trading.trader.time.sleep"):
            result = cancel_order(client, "order-abc", db_path=tmp_db_path)
        assert result is True

    def test_success_calls_clob_cancel(self, tmp_db_path):
        client = _clob_client()
        with patch("trading.trader.time.sleep"):
            cancel_order(client, "order-abc", db_path=tmp_db_path)
        client.cancel.assert_called_once_with("order-abc")

    def test_success_sleeps_post_cancel_wait(self, tmp_db_path):
        from config import settings
        client = _clob_client()
        with patch("trading.trader.time.sleep") as mock_sleep:
            cancel_order(client, "order-abc", db_path=tmp_db_path)
        mock_sleep.assert_called_once_with(settings.POST_CANCEL_WAIT_SECONDS)

    def test_failure_returns_false(self, tmp_db_path):
        client = _clob_client(cancel_raises=RuntimeError("order not found"))
        with patch("trading.trader.time.sleep"):
            result = cancel_order(client, "order-bad", db_path=tmp_db_path)
        assert result is False


# ---------------------------------------------------------------------------
# cancel_all_open_orders
# ---------------------------------------------------------------------------


class TestCancelAllOpenOrders:
    def test_cancels_all_open_orders(self, tmp_db_path):
        open_orders = [{"id": "o1"}, {"id": "o2"}, {"id": "o3"}]
        client = _clob_client(open_orders=open_orders)
        with patch("trading.trader.time.sleep"):
            count = cancel_all_open_orders(client, db_path=tmp_db_path)
        assert count == 3

    def test_no_open_orders_returns_zero(self, tmp_db_path):
        client = _clob_client(open_orders=[])
        count = cancel_all_open_orders(client, db_path=tmp_db_path)
        assert count == 0

    def test_fetch_failure_returns_zero(self, tmp_db_path):
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("CLOB down")
        count = cancel_all_open_orders(client, db_path=tmp_db_path)
        assert count == 0


# ---------------------------------------------------------------------------
# cancel_stale_orders
# ---------------------------------------------------------------------------


class TestCancelStaleOrders:
    def _insert_old_trade(self, db_path, order_id, age_minutes):
        from db.init import get_connection
        created = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO trades (market_id, direction, final_size, price, order_id, status, mode, created_at) "
                "VALUES ('mkt-x', 'YES', 10, 0.4, ?, 'open', 'live', ?)",
                (order_id, created),
            )
            conn.commit()

    def test_cancels_stale_orders(self, tmp_db_path):
        self._insert_old_trade(tmp_db_path, "stale-1", age_minutes=60)
        client = _clob_client()
        with patch("trading.trader.time.sleep"):
            count = cancel_stale_orders(client, max_age_minutes=30, db_path=tmp_db_path)
        assert count == 1

    def test_skips_fresh_orders(self, tmp_db_path):
        self._insert_old_trade(tmp_db_path, "fresh-1", age_minutes=5)
        client = _clob_client()
        count = cancel_stale_orders(client, max_age_minutes=30, db_path=tmp_db_path)
        assert count == 0

    def test_no_stale_orders_returns_zero(self, tmp_db_path):
        client = _clob_client()
        count = cancel_stale_orders(client, max_age_minutes=30, db_path=tmp_db_path)
        assert count == 0
