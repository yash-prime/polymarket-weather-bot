"""
db/init.py — Database initialisation and migration runner.

Responsibilities:
- Create all tables defined in schema.sql on first launch (idempotent via IF NOT EXISTS).
- Enable WAL mode for concurrent dashboard reads alongside main.py writes.
- Insert default system_config rows.
- Apply schema migrations when the schema_version table indicates an older version.

Usage:
    from db.init import init_db
    init_db()          # call once in main.py startup
"""
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).parent
_SCHEMA_FILE = _SCHEMA_DIR / "schema.sql"

# Increment this whenever schema.sql changes incompatibly.
CURRENT_SCHEMA_VERSION = 4


def _get_db_path() -> str:
    """Import here (not at module level) so tests can patch settings before import."""
    from config import settings
    return settings.DB_PATH


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """
    Return a SQLite connection with WAL mode and row_factory set.

    The caller is responsible for closing the connection.
    Use as a context manager for automatic commit/rollback:

        with get_connection() as conn:
            conn.execute("SELECT ...")
    """
    path = db_path or _get_db_path()
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    """
    Initialise the database: create schema and run any pending migrations.

    Safe to call multiple times — all DDL uses IF NOT EXISTS and INSERT OR IGNORE.
    """
    path = db_path or _get_db_path()
    logger.info("Initialising database at %s", path)

    with get_connection(path) as conn:
        _run_schema(conn)
        _run_migrations(conn)

    logger.info("Database initialised (schema version %d)", CURRENT_SCHEMA_VERSION)


def _run_schema(conn: sqlite3.Connection) -> None:
    """Execute schema.sql in full — all statements are idempotent."""
    sql = _SCHEMA_FILE.read_text()
    conn.executescript(sql)
    conn.commit()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    Apply any migrations needed to bring an older DB up to CURRENT_SCHEMA_VERSION.

    Pattern: each migration is a function `_migrate_v{N}` that takes a connection
    and upgrades from version N-1 → N. Add new functions here as the schema evolves.
    """
    row = conn.execute(
        "SELECT MAX(version) as v FROM schema_version"
    ).fetchone()
    current = row["v"] if row and row["v"] is not None else 0

    migrations = {
        # 1: first version — schema.sql handles it entirely
        2: _migrate_v2,
        3: _migrate_v3,
        4: _migrate_v4,
    }

    for version in sorted(migrations.keys()):
        if current < version:
            logger.info("Applying schema migration v%d", version)
            migrations[version](conn)
            conn.execute(
                "INSERT INTO schema_version VALUES (?, datetime('now'))",
                (version,),
            )
            conn.commit()
            current = version


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2: add rationale column to paper_trades."""
    try:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN rationale TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3: add start_date and start_price columns to markets."""
    for col, typedef in [("start_date", "TEXT"), ("start_price", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # column already exists


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4: add realized_pnl column to paper_trades for accurate P&L tracking."""
    try:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN realized_pnl REAL")
    except sqlite3.OperationalError:
        pass  # column already exists


def get(table: str, key: str, db_path: str | None = None) -> str | None:
    """
    Read a single value from a key/value table (system_config).

    Returns the value string, or None if the key does not exist.
    """
    with get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT value FROM {table} WHERE key = ?", (key,)  # noqa: S608
        ).fetchone()
        return row["value"] if row else None


def set(table: str, key: str, value: str, db_path: str | None = None) -> None:
    """
    Upsert a single value in a key/value table (system_config).
    """
    with get_connection(db_path) as conn:
        conn.execute(
            f"INSERT INTO {table} (key, value, updated_at) VALUES (?, ?, datetime('now')) "  # noqa: S608
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value),
        )
        conn.commit()
