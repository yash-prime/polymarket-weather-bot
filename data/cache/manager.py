"""
data/cache/manager.py — SQLite-backed TTL cache for all external API responses.

All weather/market API calls go through CacheManager to prevent redundant
network calls and to respect rate limits. Cached in the api_cache table.

Usage:
    cache = CacheManager()
    value = cache.get("open_meteo", "ensemble:41.88:-87.63")
    if value is None:
        data = fetch_from_api(...)
        cache.set("open_meteo", "ensemble:41.88:-87.63", data, ttl_seconds=3600)
"""
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class CacheManager:
    """
    Thread-safe SQLite-backed TTL cache.

    Each entry is stored in the api_cache table as (source, key) → JSON value
    with an expiry calculated from fetched_at + ttl_seconds.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()

    def _get_conn(self):
        from db.init import get_connection
        return get_connection(self._db_path)

    def get(self, source: str, key: str) -> Any | None:
        """
        Return the cached value for (source, key), or None on miss/expiry.

        Expired entries are treated as misses (lazy expiry — cleaned up on set).
        """
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT value, fetched_at, ttl_seconds FROM api_cache "
                    "WHERE source = ? AND key = ?",
                    (source, key),
                ).fetchone()

        if row is None:
            logger.debug("Cache MISS: source=%s key=%s", source, key)
            return None

        fetched_at = datetime.fromisoformat(row["fetched_at"]).replace(
            tzinfo=timezone.utc
        )
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()

        if age_seconds > row["ttl_seconds"]:
            logger.debug(
                "Cache EXPIRED: source=%s key=%s age=%.0fs ttl=%ds",
                source,
                key,
                age_seconds,
                row["ttl_seconds"],
            )
            return None

        logger.debug(
            "Cache HIT: source=%s key=%s age=%.0fs ttl=%ds",
            source,
            key,
            age_seconds,
            row["ttl_seconds"],
        )
        return json.loads(row["value"])

    def set(self, source: str, key: str, value: Any, ttl_seconds: int) -> None:
        """
        Store value for (source, key) with the given TTL.

        Overwrites any existing entry for the same (source, key).
        """
        serialized = json.dumps(value)
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO api_cache (source, key, value, fetched_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(source, key) DO UPDATE SET "
                    "value=excluded.value, fetched_at=excluded.fetched_at, "
                    "ttl_seconds=excluded.ttl_seconds",
                    (source, key, serialized, now, ttl_seconds),
                )
                conn.commit()

        logger.debug(
            "Cache SET: source=%s key=%s ttl=%ds",
            source,
            key,
            ttl_seconds,
        )

    def invalidate(self, source: str, key: str) -> None:
        """Remove a specific cache entry."""
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "DELETE FROM api_cache WHERE source = ? AND key = ?",
                    (source, key),
                )
                conn.commit()
        logger.debug("Cache INVALIDATED: source=%s key=%s", source, key)

    def clear_expired(self) -> int:
        """
        Delete all expired entries. Returns the number of rows deleted.

        Call this periodically to keep the cache table from growing unboundedly.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM api_cache "
                    "WHERE datetime(fetched_at, '+' || ttl_seconds || ' seconds') < ?",
                    (now,),
                )
                conn.commit()
                deleted = cursor.rowcount

        if deleted:
            logger.info("Cache purged %d expired entries", deleted)
        return deleted
