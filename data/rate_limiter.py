"""
data/rate_limiter.py — Thread-safe per-source hourly call budget tracker.

All data/sources/*.py modules call check_and_record() before making an
external API request. If the budget is exhausted, the call is skipped and
the source is marked as degraded for this scan cycle.

Usage:
    limiter = RateLimiter()
    if not limiter.check_and_record("open_meteo"):
        logger.warning("open_meteo rate limit hit — skipping")
        return None
    # ... proceed with API call
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Thread-safe sliding-window hourly rate limiter.

    Maintains a list of call timestamps per source. On each check, expired
    timestamps (older than 3600s) are pruned. If the remaining count is
    below the source's limit, the call is recorded and allowed; otherwise
    it is denied.
    """

    def __init__(self) -> None:
        self._counts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, source: str) -> bool:
        """
        Check if a call to `source` is within budget, and record it if so.

        Returns True if the call is allowed, False if budget is exhausted.
        """
        from config import settings

        limit = settings.get_rate_limit(source)
        now = time.time()
        window_cutoff = now - 3600.0  # 1-hour sliding window

        with self._lock:
            timestamps = self._counts.get(source, [])
            # Prune timestamps outside the window
            active = [t for t in timestamps if t > window_cutoff]

            if len(active) >= limit:
                logger.warning(
                    "Rate limit hit for source=%s: %d/%d calls in last hour",
                    source,
                    len(active),
                    limit,
                )
                self._counts[source] = active
                return False

            active.append(now)
            self._counts[source] = active
            return True

    def current_usage(self, source: str) -> int:
        """Return the number of calls made to source in the last hour."""
        now = time.time()
        window_cutoff = now - 3600.0
        with self._lock:
            return sum(1 for t in self._counts.get(source, []) if t > window_cutoff)

    def reset(self, source: str | None = None) -> None:
        """
        Clear call history for a source (or all sources if source=None).

        Primarily useful in tests.
        """
        with self._lock:
            if source is None:
                self._counts.clear()
            else:
                self._counts.pop(source, None)
