"""
config/log_filter.py — Log formatter that redacts private keys from log output.

Usage:
    from config.log_filter import RedactingFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s — %(message)s"))

    # Or install on the root logger:
    logging.getLogger().handlers[0].setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s — %(message)s")
    )

Redaction pattern:
    Matches Ethereum-style private keys: 0x followed by 60–66 hex characters.
    These are replaced with "[REDACTED_KEY]" in all log output.

This module is imported by main.py before any other logging is configured.
"""
import logging
import re

_PRIVATE_KEY_RE = re.compile(r"0x[0-9a-fA-F]{60,66}")
_REDACTED = "[REDACTED_KEY]"


class RedactingFormatter(logging.Formatter):
    """
    A logging.Formatter subclass that scrubs private key patterns from all
    log records before they are emitted.

    Applies redaction to both the formatted message and the record's args
    to handle cases where the private key is in positional log arguments.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Redact in the args before interpolation (handles %s-style logging)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )

        formatted = super().format(record)
        return _redact(formatted)


def _redact(text: str) -> str:
    """Replace all private-key-shaped strings in text with [REDACTED_KEY]."""
    return _PRIVATE_KEY_RE.sub(_REDACTED, text)


def install_redacting_formatter(fmt: str | None = None) -> None:
    """
    Install RedactingFormatter on all handlers of the root logger.

    Call this once at startup (before any log messages are emitted).
    """
    if fmt is None:
        fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"

    formatter = RedactingFormatter(fmt)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(formatter)
