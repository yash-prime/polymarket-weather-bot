"""
tests/test_security.py — Security policy tests for TASK-028.

Covers:
  - Private key redaction: 0x hex strings removed from log output
  - RedactingFormatter: scrubs keys in formatted messages and positional args
  - .gitignore: contains .env, *.db, .env.local entries
  - .env.example: lists all required and optional variables
  - settings._check_env_file_permissions: warns on world-readable .env
"""
import logging
import os
import stat
import tempfile
from unittest.mock import patch

import pytest

from config.log_filter import RedactingFormatter, _redact


# ---------------------------------------------------------------------------
# _redact helper
# ---------------------------------------------------------------------------


class TestRedactHelper:
    def test_redacts_64_char_hex_key(self):
        key = "0x" + "a" * 64
        result = _redact(f"private key is {key} here")
        assert key not in result
        assert "[REDACTED_KEY]" in result

    def test_redacts_60_char_hex_key(self):
        key = "0x" + "f" * 60
        assert "[REDACTED_KEY]" in _redact(key)

    def test_redacts_66_char_hex_key(self):
        key = "0x" + "1" * 66
        assert "[REDACTED_KEY]" in _redact(key)

    def test_does_not_redact_short_hex(self):
        short = "0x1234abcd"
        result = _redact(short)
        assert short in result  # Too short to be a private key

    def test_does_not_redact_plain_text(self):
        text = "nothing to redact here"
        assert _redact(text) == text

    def test_multiple_keys_all_redacted(self):
        key1 = "0x" + "a" * 64
        key2 = "0x" + "b" * 64
        result = _redact(f"{key1} and {key2}")
        assert key1 not in result
        assert key2 not in result
        assert result.count("[REDACTED_KEY]") == 2


# ---------------------------------------------------------------------------
# RedactingFormatter
# ---------------------------------------------------------------------------


class TestRedactingFormatter:
    def _make_record(self, msg: str, args=()) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_redacts_key_in_message(self):
        key = "0x" + "c" * 64
        fmt = RedactingFormatter("%(message)s")
        record = self._make_record(f"key={key}")
        output = fmt.format(record)
        assert key not in output
        assert "[REDACTED_KEY]" in output

    def test_redacts_key_in_positional_args(self):
        key = "0x" + "d" * 64
        fmt = RedactingFormatter("%(message)s")
        record = self._make_record("key=%s", args=(key,))
        output = fmt.format(record)
        assert key not in output
        assert "[REDACTED_KEY]" in output

    def test_redacts_key_in_dict_args(self):
        """RedactingFormatter redacts keys in dict-style args."""
        key = "0x" + "e" * 64
        fmt = RedactingFormatter("%(message)s")
        # Build a record manually and set args after construction to bypass
        # Python 3.12's len-1 dict validation in LogRecord.__init__
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg="key=%(k)s", args=None, exc_info=None,
        )
        record.args = {"k": key}
        output = fmt.format(record)
        assert key not in output
        assert "[REDACTED_KEY]" in output

    def test_plain_message_unchanged(self):
        fmt = RedactingFormatter("%(message)s")
        record = self._make_record("hello world")
        output = fmt.format(record)
        assert output == "hello world"


# ---------------------------------------------------------------------------
# .gitignore coverage
# ---------------------------------------------------------------------------


class TestGitignore:
    def _read_gitignore(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", ".gitignore")
        with open(path) as f:
            return f.read()

    def test_env_in_gitignore(self):
        content = self._read_gitignore()
        assert ".env" in content

    def test_db_files_in_gitignore(self):
        content = self._read_gitignore()
        assert "*.db" in content

    def test_env_local_in_gitignore(self):
        content = self._read_gitignore()
        assert ".env.local" in content


# ---------------------------------------------------------------------------
# .env.example completeness
# ---------------------------------------------------------------------------


class TestEnvExample:
    def _read_env_example(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        with open(path) as f:
            return f.read()

    def test_contains_private_key(self):
        assert "PRIVATE_KEY" in self._read_env_example()

    def test_contains_poly_api_key(self):
        assert "POLY_API_KEY" in self._read_env_example()

    def test_contains_trading_mode(self):
        assert "TRADING_MODE" in self._read_env_example()

    def test_contains_telegram_vars(self):
        content = self._read_env_example()
        assert "TELEGRAM_BOT_TOKEN" in content
        assert "TELEGRAM_CHAT_ID" in content

    def test_contains_db_path(self):
        assert "DB_PATH" in self._read_env_example()

    def test_contains_anthropic_api_key(self):
        assert "ANTHROPIC_API_KEY" in self._read_env_example()


# ---------------------------------------------------------------------------
# .env world-readable permission check
# ---------------------------------------------------------------------------


class TestEnvFilePermissions:
    def test_warns_on_world_readable_env(self):
        with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
            env_path = f.name

        try:
            # Make world-readable
            os.chmod(env_path, 0o644)

            from config.settings import _check_env_file_permissions

            with patch("config.settings.logging") as mock_log, \
                 patch("config.settings.os.path.exists", return_value=True), \
                 patch("config.settings.os.stat") as mock_stat:

                mock_stat.return_value.st_mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
                _check_env_file_permissions()

            mock_log.warning.assert_called_once()
            assert "world-readable" in mock_log.warning.call_args[0][0].lower() or \
                   "chmod" in mock_log.warning.call_args[0][0]

        finally:
            os.unlink(env_path)

    def test_no_warning_on_secure_env(self):
        from config.settings import _check_env_file_permissions

        with patch("config.settings.logging") as mock_log, \
             patch("config.settings.os.path.exists", return_value=True), \
             patch("config.settings.os.stat") as mock_stat:

            # 0o600 — owner read/write only
            mock_stat.return_value.st_mode = stat.S_IRUSR | stat.S_IWUSR
            _check_env_file_permissions()

        mock_log.warning.assert_not_called()
