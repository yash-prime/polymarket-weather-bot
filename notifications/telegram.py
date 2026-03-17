"""
notifications/telegram.py — Async non-blocking Telegram alert system.

Architecture:
  - All sends go through an asyncio.Queue consumed by a background task.
  - The public API (`send_alert`, `send_event`) is sync-safe — callers never
    block waiting for HTTP.
  - TELEGRAM_CHAT_ID whitelist is enforced: if the configured chat ID does
    not match the whitelist, messages are dropped with a warning.
  - If Telegram is not configured (no token / chat ID), all sends are no-ops.

Usage:
    from notifications.telegram import send_alert, send_event
    from notifications.events import NotificationEvent

    send_alert("Bot started in paper mode")
    send_event(NotificationEvent.TRADE_PLACED, "YES mkt-abc size=10 price=0.40")

Startup:
    Call `start_background_task()` once inside an asyncio event loop (e.g. in
    main.py after creating the scheduler).  This starts the queue consumer.
    Call `send_startup_ping()` after start to confirm connectivity.
"""
import asyncio
import logging
from typing import Any

from config import settings
from notifications.events import NotificationEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_queue: asyncio.Queue[str | None] = asyncio.Queue()
_background_task: asyncio.Task | None = None
_WHITELIST_CHAT_IDS: frozenset[str] = frozenset(
    settings.TELEGRAM_CHAT_ID.split(",")
) if settings.TELEGRAM_CHAT_ID else frozenset()


# ---------------------------------------------------------------------------
# Public API — sync-safe (enqueue only, never block)
# ---------------------------------------------------------------------------


def send_alert(message: str) -> None:
    """
    Enqueue a plain-text Telegram alert.

    Safe to call from any thread or sync context.  Never blocks.
    No-op if Telegram is not configured.
    """
    if not _is_configured():
        return
    try:
        _queue.put_nowait(message)
    except asyncio.QueueFull:
        logger.warning("telegram.send_alert: queue full — dropping message: %s", message[:80])


def send_event(event: NotificationEvent, detail: str = "") -> None:
    """
    Enqueue a structured event alert.

    Format: "[EVENT_TYPE] detail"
    """
    message = f"[{event.value.upper()}] {detail}".strip()
    send_alert(message)


def send_startup_ping() -> None:
    """Send a startup confirmation message."""
    send_event(
        NotificationEvent.STARTUP,
        f"Weather bot online — mode={settings.TRADING_MODE}",
    )


# ---------------------------------------------------------------------------
# Background consumer
# ---------------------------------------------------------------------------


def start_background_task(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """
    Start the queue consumer background task.

    Must be called inside a running asyncio event loop.
    Idempotent — subsequent calls are no-ops.
    """
    global _background_task  # noqa: PLW0603

    if _background_task is not None and not _background_task.done():
        return  # Already running

    if loop is None:
        loop = asyncio.get_event_loop()

    _background_task = loop.create_task(_consume_queue())
    logger.info("telegram: background send task started")


async def _consume_queue() -> None:
    """
    Background coroutine: drain the queue and send messages to Telegram.

    Runs indefinitely.  Handles per-message errors gracefully.
    """
    while True:
        message = await _queue.get()
        if message is None:
            # Sentinel — shut down
            break
        await _send_telegram(message)
        _queue.task_done()


async def _send_telegram(message: str) -> None:
    """
    Send a single message to the configured Telegram chat.

    Silently drops the message if the chat ID is not in the whitelist.
    """
    chat_id = settings.TELEGRAM_CHAT_ID
    token = settings.TELEGRAM_BOT_TOKEN

    if not chat_id or not token:
        return

    if _WHITELIST_CHAT_IDS and chat_id not in _WHITELIST_CHAT_IDS:
        logger.warning("telegram._send_telegram: chat_id %s not in whitelist — dropping", chat_id)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    try:
        import httpx  # lightweight async HTTP — falls back to aiohttp if absent

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "telegram._send_telegram: HTTP %d — %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except ImportError:
        await _send_telegram_aiohttp(url, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram._send_telegram: send failed: %s", exc)


async def _send_telegram_aiohttp(url: str, payload: dict[str, Any]) -> None:
    """Fallback sender using aiohttp if httpx is not installed."""
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("telegram._send_telegram: HTTP %d — %s", resp.status, text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram._send_telegram_aiohttp: send failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_configured() -> bool:
    """Return True if both token and chat_id are set."""
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


async def stop_background_task() -> None:
    """Gracefully stop the background consumer (send sentinel None)."""
    global _background_task  # noqa: PLW0603
    if _background_task and not _background_task.done():
        await _queue.put(None)
        await _background_task
    _background_task = None
