"""
tests/test_dashboard.py — Unit tests for dashboard/app.py

Per TASK-024 done-when: "unit test: verify no direct state mutation"
All dashboard state changes must go through system_config DB writes —
never mutate in-memory state directly.

Covers:
  - _read_system_config reads all key/value pairs from DB
  - _write_system_config writes to DB (not to settings module)
  - settings.* attributes are unchanged after _write_system_config call
  - _get_active_markets returns only parse_status='success' markets
  - _get_recent_trades reads from correct table for mode
  - _get_portfolio_snapshot returns latest row for mode
  - _set_market_lock writes to market_overrides table
"""
import pytest

# dashboard/app.py imports streamlit at module level, which requires a server.
# We mock it before importing to avoid the Streamlit runtime requirement.
import sys
from unittest.mock import MagicMock

# Stub out streamlit so we can import the dashboard module without a running server
_st_mock = MagicMock()
sys.modules["streamlit"] = _st_mock

from dashboard import app  # noqa: E402


# ---------------------------------------------------------------------------
# _read_system_config
# ---------------------------------------------------------------------------


class TestReadSystemConfig:
    def test_returns_dict(self, tmp_db_path):
        result = _call_with_db(app._read_system_config, tmp_db_path)
        assert isinstance(result, dict)

    def test_reads_default_rows(self, tmp_db_path):
        """init_db inserts default rows; they should be readable."""
        result = _call_with_db(app._read_system_config, tmp_db_path)
        # bot_halted should exist as a default
        assert "bot_halted" in result

    def test_returns_empty_dict_on_error(self):
        """Returns empty dict if DB is unreachable — never raises."""
        import unittest.mock as mock
        with mock.patch("db.init.get_connection", side_effect=RuntimeError("no db")):
            result = app._read_system_config()
        assert result == {}


# ---------------------------------------------------------------------------
# _write_system_config — no direct state mutation
# ---------------------------------------------------------------------------


class TestWriteSystemConfig:
    def test_writes_to_db_not_settings(self, tmp_db_path):
        """
        Writing a config value must update the DB row,
        NOT mutate the settings module's in-memory attribute.
        """
        from config import settings
        original = settings.MIN_EDGE_THRESHOLD

        _call_with_db(app._write_system_config, tmp_db_path, "min_edge_threshold", "0.99")

        # In-memory settings must be unchanged
        assert settings.MIN_EDGE_THRESHOLD == original

    def test_value_persisted_in_db(self, tmp_db_path):
        _call_with_db(app._write_system_config, tmp_db_path, "custom_key", "custom_val")
        cfg = _call_with_db(app._read_system_config, tmp_db_path)
        assert cfg.get("custom_key") == "custom_val"

    def test_upsert_updates_existing_row(self, tmp_db_path):
        _call_with_db(app._write_system_config, tmp_db_path, "bot_halted", "1")
        _call_with_db(app._write_system_config, tmp_db_path, "bot_halted", "0")
        cfg = _call_with_db(app._read_system_config, tmp_db_path)
        assert cfg["bot_halted"] == "0"

    def test_kill_switch_does_not_mutate_settings(self, tmp_db_path):
        from config import settings
        _call_with_db(app._write_system_config, tmp_db_path, "bot_halted", "1")
        # No settings attribute bot_halted — if this fails it means someone added
        # direct mutation, which is the bug we're guarding against
        assert not hasattr(settings, "BOT_HALTED") or settings.BOT_HALTED is not True


# ---------------------------------------------------------------------------
# _get_active_markets
# ---------------------------------------------------------------------------


class TestGetActiveMarkets:
    def test_returns_only_success_markets(self, tmp_db_path):
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, parse_status) "
                "VALUES ('m-ok', 'Will it rain?', 0.6, 'success')"
            )
            conn.execute(
                "INSERT INTO markets (id, question, yes_price, parse_status) "
                "VALUES ('m-pending', 'Temp exceed?', 0.4, 'pending')"
            )
            conn.commit()

        markets = _call_with_db(app._get_active_markets, tmp_db_path)
        ids = [m["id"] for m in markets]
        assert "m-ok" in ids
        assert "m-pending" not in ids

    def test_empty_when_no_markets(self, tmp_db_path):
        result = _call_with_db(app._get_active_markets, tmp_db_path)
        assert result == []


# ---------------------------------------------------------------------------
# _get_recent_trades
# ---------------------------------------------------------------------------


class TestGetRecentTrades:
    def test_live_reads_trades_table(self, tmp_db_path):
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO trades (market_id, direction, final_size, price, order_id, status, mode) "
                "VALUES ('m-1', 'YES', 10, 0.4, 'ord-1', 'open', 'live')"
            )
            conn.commit()
        trades = _call_with_db(app._get_recent_trades, tmp_db_path, "live")
        assert len(trades) == 1
        assert trades[0]["market_id"] == "m-1"

    def test_paper_reads_paper_trades_table(self, tmp_db_path):
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO paper_trades (market_id, direction, final_size, simulated_fill_price, status) "
                "VALUES ('m-paper', 'YES', 10, 0.4, 'open')"
            )
            conn.commit()
        paper = _call_with_db(app._get_recent_trades, tmp_db_path, "paper")
        live = _call_with_db(app._get_recent_trades, tmp_db_path, "live")
        assert len(paper) == 1
        assert len(live) == 0


# ---------------------------------------------------------------------------
# _get_portfolio_snapshot
# ---------------------------------------------------------------------------


class TestGetPortfolioSnapshot:
    def test_returns_none_when_empty(self, tmp_db_path):
        result = _call_with_db(app._get_portfolio_snapshot, tmp_db_path, "paper")
        assert result is None

    def test_returns_latest_snapshot(self, tmp_db_path):
        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(mode, total_equity, unrealized_pnl, realized_pnl, daily_pnl, daily_loss_pct, open_positions) "
                "VALUES ('paper', 500.0, 10.0, 5.0, 2.0, 0.004, 2)"
            )
            conn.commit()
        snap = _call_with_db(app._get_portfolio_snapshot, tmp_db_path, "paper")
        assert snap is not None
        assert snap["total_equity"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# _set_market_lock
# ---------------------------------------------------------------------------


class TestSetMarketLock:
    def test_writes_lock_to_market_overrides(self, tmp_db_path):
        _call_with_db(app._set_market_lock, tmp_db_path, "mkt-abc", True)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT action FROM market_overrides WHERE market_id = 'mkt-abc'"
            ).fetchone()
        assert row is not None
        assert row["action"] == "skip"

    def test_unlock_deletes_override_row(self, tmp_db_path):
        _call_with_db(app._set_market_lock, tmp_db_path, "mkt-abc", True)
        _call_with_db(app._set_market_lock, tmp_db_path, "mkt-abc", False)

        from db.init import get_connection
        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT action FROM market_overrides WHERE market_id = 'mkt-abc'"
            ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# Helpers — inject tmp_db_path via monkeypatching get_connection
# ---------------------------------------------------------------------------


def _call_with_db(fn, db_path, *args, **kwargs):
    """
    Call a dashboard helper function with a patched DB connection
    pointing at tmp_db_path.
    """
    import unittest.mock as mock
    from db.init import get_connection as real_gc

    def patched_gc(path=None):
        return real_gc(db_path)

    with mock.patch("dashboard.app._get_connection", patched_gc):
        return fn(*args, **kwargs)
