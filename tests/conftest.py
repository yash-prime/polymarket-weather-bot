"""
tests/conftest.py — Shared fixtures for the test suite.

Provides:
  - tmp_db_path: a temporary SQLite database with full schema applied
  - A pre-initialised DB connection factory helper

All fixtures reset state between tests — no shared mutable state.
"""
import os
import sys

import pytest

# Ensure project root is on sys.path regardless of how pytest is invoked
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set default env so settings imports don't fail validation
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("DB_PATH", ":memory:")


@pytest.fixture()
def tmp_db_path(tmp_path):
    """
    Return the path to a fresh, schema-initialised SQLite database.

    Uses a real file (not :memory:) so that tests can open multiple
    connections, mimicking the production WAL-mode setup.
    """
    db_file = str(tmp_path / "test_trades.db")

    # Patch settings before importing db.init so DB_PATH is used correctly
    import importlib

    import config.settings as settings_mod

    original = settings_mod.DB_PATH
    settings_mod.DB_PATH = db_file

    from db.init import init_db
    init_db(db_path=db_file)

    yield db_file

    settings_mod.DB_PATH = original
