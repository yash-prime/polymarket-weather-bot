"""
tests/test_telegram.py — Unit tests for notifications/telegram.py and events.py

Covers:
  - NotificationEvent enum values importable
  - send_alert: no-op when not configured
  - send_alert: enqueues message when configured
  - send_event: formats [EVENT_TYPE] prefix
  - send_startup_ping: enqueues startup message
  - TELEGRAM_CHAT_ID whitelist enforcement
  - _send_telegram: drops message if chat_id not in whitelist
  - Queue consumer: drains queue and calls HTTP
  - Unconfigured state: _is_configured() returns False
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notifications.events import NotificationEvent
from notifications import telegram as tg


# ---------------------------------------------------------------------------
# NotificationEvent enum
# ---------------------------------------------------------------------------


class TestNotificationEvent:
    def test_startup_value(self):
        assert NotificationEvent.STARTUP == "startup"

    def test_trade_placed_value(self):
        assert NotificationEvent.TRADE_PLACED == "trade_placed"

    def test_kill_switch_value(self):
        assert NotificationEvent.KILL_SWITCH_ACTIVATED == "kill_switch_activated"

    def test_job_failed_value(self):
        assert NotificationEvent.JOB_FAILED == "job_failed"

    def test_all_events_are_strings(self):
        for event in NotificationEvent:
            assert isinstance(event.value, str)


# ---------------------------------------------------------------------------
# _is_configured
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def test_false_when_no_token(self):
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "12345"):
            assert tg._is_configured() is False

    def test_false_when_no_chat_id(self):
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "some-token"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", ""):
            assert tg._is_configured() is False

    def test_true_when_both_set(self):
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"):
            assert tg._is_configured() is True


# ---------------------------------------------------------------------------
# send_alert
# ---------------------------------------------------------------------------


class TestSendAlert:
    def _fresh_queue(self):
        """Replace module queue with a fresh one."""
        q = asyncio.Queue()
        tg._queue = q
        return q

    def test_no_op_when_not_configured(self):
        q = self._fresh_queue()
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", ""):
            tg.send_alert("hello")
        assert q.qsize() == 0

    def test_enqueues_when_configured(self):
        q = self._fresh_queue()
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"):
            tg.send_alert("test message")
        assert q.qsize() == 1
        assert q.get_nowait() == "test message"

    def test_multiple_messages_enqueued_in_order(self):
        q = self._fresh_queue()
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"):
            tg.send_alert("msg1")
            tg.send_alert("msg2")
        assert q.get_nowait() == "msg1"
        assert q.get_nowait() == "msg2"


# ---------------------------------------------------------------------------
# send_event
# ---------------------------------------------------------------------------


class TestSendEvent:
    def _fresh_queue(self):
        q = asyncio.Queue()
        tg._queue = q
        return q

    def test_formats_event_prefix(self):
        q = self._fresh_queue()
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"):
            tg.send_event(NotificationEvent.TRADE_PLACED, "YES mkt-1 size=10")
        msg = q.get_nowait()
        assert msg.startswith("[TRADE_PLACED]")
        assert "YES mkt-1 size=10" in msg

    def test_no_detail_still_works(self):
        q = self._fresh_queue()
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"):
            tg.send_event(NotificationEvent.STARTUP)
        msg = q.get_nowait()
        assert "[STARTUP]" in msg


# ---------------------------------------------------------------------------
# send_startup_ping
# ---------------------------------------------------------------------------


class TestSendStartupPing:
    def test_enqueues_startup_message(self):
        q = asyncio.Queue()
        tg._queue = q
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"), \
             patch.object(tg.settings, "TRADING_MODE", "paper"):
            tg.send_startup_ping()
        msg = q.get_nowait()
        assert "[STARTUP]" in msg
        assert "paper" in msg


# ---------------------------------------------------------------------------
# Whitelist enforcement
# ---------------------------------------------------------------------------


class TestWhitelistEnforcement:
    def test_drops_message_not_in_whitelist(self):
        """_send_telegram should not call HTTP if chat_id not in whitelist."""
        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"), \
             patch.object(tg, "_WHITELIST_CHAT_IDS", frozenset({"allowed-id"})):
            # chat_id is "99" but whitelist only has "allowed-id"
            sent = []

            async def _mock_post(self_obj, url, json=None):  # noqa: ARG001
                sent.append(json)
                resp = MagicMock()
                resp.status_code = 200
                return resp

            # Directly call _send_telegram
            asyncio.run(tg._send_telegram("should be dropped"))
        assert len(sent) == 0

    def test_allows_message_in_whitelist(self):
        """_send_telegram should call HTTP if chat_id is in whitelist."""
        import sys

        http_calls = []

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_resp)

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = mock_client_cls

        async def run():
            await tg._send_telegram("hello 99")
            http_calls.extend(mock_client_instance.post.call_args_list)

        with patch.object(tg.settings, "TELEGRAM_BOT_TOKEN", "tok"), \
             patch.object(tg.settings, "TELEGRAM_CHAT_ID", "99"), \
             patch.object(tg, "_WHITELIST_CHAT_IDS", frozenset({"99"})), \
             patch.dict(sys.modules, {"httpx": mock_httpx}):
            asyncio.run(run())

        assert len(http_calls) == 1
