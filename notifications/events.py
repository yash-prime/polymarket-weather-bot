"""
notifications/events.py — NotificationEvent enum.

All Telegram alert types are defined here.  Import this enum anywhere
you need to fire a notification — never use raw strings.
"""
from enum import Enum


class NotificationEvent(str, Enum):
    """
    All notification event types.

    Using str as a mixin means the value can be used directly as a message
    prefix without extra `.value` access.
    """

    # --- Bot lifecycle ---
    STARTUP = "startup"
    SHUTDOWN = "shutdown"

    # --- Job failures ---
    JOB_FAILED = "job_failed"

    # --- Trade events ---
    TRADE_PLACED = "trade_placed"
    TRADE_CANCELLED = "trade_cancelled"
    TRADE_FAILED = "trade_failed"

    # --- Risk events ---
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"

    # --- Market events ---
    MARKET_PARSED = "market_parsed"
    SIGNAL_FOUND = "signal_found"

    # --- System ---
    GENERIC_ALERT = "generic_alert"
