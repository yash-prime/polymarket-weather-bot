"""
market/scanner.py — Gamma API market scanner.

Two responsibilities:

1. job_fetch_markets() — called by APScheduler every SCAN_INTERVAL_MINUTES.
   - Polls the Gamma API for active weather markets
   - Filters by MIN_MARKET_VOLUME and MIN_DAYS_TO_RESOLVE
   - Writes NEW markets to the DB as parse_status="pending"
   - Does NOT call the LLM parser (that runs as a separate job)

2. get_active_markets() — called by job_market_scan() in main.py.
   - Returns List[Market] where parse_status="success" only
   - These are ready for weather engine + signal computation

Gamma API
---------
Public endpoint, no auth required.
Primary:  https://gamma-api.polymarket.com/events  (tag_slug=weather)
          Markets are nested inside events as m["markets"]
Fallback: https://gamma-api.polymarket.com/markets (keyword filter)

Weather tag slugs used by Polymarket:
  weather, temperature, precipitation, new-york-city, dallas, miami,
  seattle, hong-kong, shanghai, seoul, auckland, tel-aviv, taipei

Rate limiting
-------------
Enforced via RateLimiter under source "gamma".

Retry
-----
3 retries, exponential backoff, 10s timeout via tenacity.
"""
import logging
from datetime import datetime, timezone

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings
from data.rate_limiter import RateLimiter
from market.models import Market

logger = logging.getLogger(__name__)

_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
_SOURCE = "gamma"
_RATE_LIMITER = RateLimiter()

# Tag slugs that Polymarket uses for weather events (confirmed from /predictions/weather)
_WEATHER_TAG_SLUGS = [
    "weather",
    "temperature",
    "precipitation",
    "new-york-city",
    "dallas",
    "miami",
    "seattle",
    "hong-kong",
    "shanghai",
    "seoul",
    "auckland",
    "tel-aviv",
    "taipei",
    "climate-science",
]

# Keywords for fallback keyword-based filtering on /markets endpoint
_WEATHER_KEYWORDS = (
    "weather", "temperature", "rain", "snow", "hurricane", "tornado",
    "wind", "frost", "heat", "cold", "precipitation", "celsius", "fahrenheit",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def job_fetch_markets(db_path: str | None = None) -> int:
    """
    Fetch active weather markets from Gamma API and write new ones to DB.

    Called by APScheduler every SCAN_INTERVAL_MINUTES.
    Writes new markets with parse_status="pending".
    Returns the number of new markets written.

    Does NOT call the LLM parser — parsing runs as a separate job.
    """
    if not _RATE_LIMITER.check_and_record(_SOURCE):
        logger.warning("scanner: Gamma rate limit hit — skipping fetch")
        return 0

    raw_markets = _fetch_from_gamma()
    if raw_markets is None:
        return 0

    weather_markets = _filter_weather(raw_markets)
    filtered = _filter_by_constraints(weather_markets)

    new_count = _write_pending(filtered, db_path)
    logger.info(
        "scanner.job_fetch_markets: %d raw → %d weather → %d passed filter → %d new in DB",
        len(raw_markets), len(weather_markets), len(filtered), new_count,
    )
    return new_count


def get_active_markets(db_path: str | None = None) -> list[Market]:
    """
    Return markets from DB with parse_status="success", ready for the scan cycle.

    Called by job_market_scan() in main.py.
    """
    from db.init import get_connection

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, question, yes_price, end_date, volume, parsed, parse_status, resolution_risk
            FROM markets
            WHERE parse_status = 'success'
              AND end_date > datetime('now')
            ORDER BY volume DESC
            """
        ).fetchall()

    markets: list[Market] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            import json

            end_date = datetime.fromisoformat(row["end_date"].replace("Z", "+00:00"))
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)

            days_to_resolve = (end_date - now).total_seconds() / 86400
            if days_to_resolve < settings.MIN_DAYS_TO_RESOLVE:
                continue

            parsed = json.loads(row["parsed"]) if row["parsed"] else None

            markets.append(
                Market(
                    id=row["id"],
                    question=row["question"],
                    yes_price=float(row["yes_price"]) if row["yes_price"] is not None else 0.5,
                    end_date=end_date,
                    volume=float(row["volume"]) if row["volume"] is not None else 0.0,
                    parsed=parsed,
                    parse_status=row["parse_status"],
                    resolution_risk=row["resolution_risk"],
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scanner.get_active_markets: error loading market %s: %s", row["id"], exc)

    return markets


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _fetch_raw(url: str, params: dict) -> list:
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # Gamma API may return {"markets": [...]} or just [...]
    if isinstance(data, list):
        return data
    return data.get("markets", data.get("data", []))


def _fetch_from_gamma() -> list | None:
    """
    Fetch active weather markets from Gamma API.

    Strategy:
    1. Query /events endpoint for each weather tag slug — markets are nested
       inside events as event["markets"]. This is the primary path and matches
       what polymarket.com/predictions/weather shows.
    2. Fall back to /markets endpoint with keyword filtering if events returns
       nothing useful.

    Returns a flat list of market dicts, or None on total failure.
    """
    markets: list[dict] = []
    seen_ids: set[str] = set()

    # --- Primary: /events endpoint per weather tag slug ---
    for slug in _WEATHER_TAG_SLUGS:
        try:
            params = {
                "active": "true",
                "closed": "false",
                "tag_slug": slug,
                "limit": 100,
            }
            events = _fetch_raw(_GAMMA_EVENTS_URL, params)
            for event in events:
                for m in event.get("markets", []):
                    mid = str(m.get("id") or m.get("conditionId") or "")
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        markets.append(m)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scanner: events fetch failed for slug=%s: %s", slug, exc)

    if markets:
        logger.info("scanner: fetched %d markets via /events endpoint", len(markets))
        return markets

    # --- Fallback: /markets endpoint (keyword filter applied downstream) ---
    logger.info("scanner: /events returned nothing, falling back to /markets")
    try:
        params = {
            "active": "true",
            "closed": "false",
            "limit": 200,
        }
        return _fetch_raw(_GAMMA_MARKETS_URL, params)
    except Exception as exc:  # noqa: BLE001
        logger.error("scanner: Gamma API fetch failed on both endpoints: %s", exc)
        return None


def _filter_weather(markets: list) -> list:
    """
    Keep only markets whose question contains weather-related keywords.

    Gamma API doesn't reliably expose a "weather" tag in all response shapes,
    so keyword filtering is the reliable fallback.
    """
    result = []
    for m in markets:
        question = (m.get("question") or m.get("title") or "").lower()
        tags = [t.get("slug", "").lower() for t in m.get("tags", [])]
        is_weather = (
            any(kw in question for kw in _WEATHER_KEYWORDS)
            or any(kw in t for kw in _WEATHER_KEYWORDS for t in tags)
        )
        if is_weather:
            result.append(m)
    return result


def _filter_by_constraints(markets: list) -> list:
    """
    Apply MIN_MARKET_VOLUME and MIN_DAYS_TO_RESOLVE filters.

    Skips markets that cannot produce a valid end_date.
    """
    now = datetime.now(timezone.utc)
    result = []
    for m in markets:
        try:
            volume = float(m.get("volume", 0) or 0)
            if volume < settings.MIN_MARKET_VOLUME:
                continue

            end_date_str = m.get("endDate") or m.get("end_date") or m.get("endDateIso")
            if not end_date_str:
                continue
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)

            days_to_resolve = (end_date - now).total_seconds() / 86400
            if days_to_resolve < settings.MIN_DAYS_TO_RESOLVE:
                continue

            result.append(m)
        except Exception as exc:  # noqa: BLE001
            logger.debug("scanner._filter_by_constraints: skipping malformed market: %s", exc)

    return result


def _write_pending(markets: list, db_path: str | None) -> int:
    """
    Write new markets to the DB with parse_status="pending".

    Uses INSERT OR IGNORE so existing markets are not overwritten
    (they may have been parsed already or have updated prices).
    Returns the number of rows newly inserted.
    """
    from db.init import get_connection

    if not markets:
        return 0

    rows: list[tuple] = []
    now_str = datetime.now(timezone.utc).isoformat()
    for m in markets:
        market_id = m.get("id") or m.get("conditionId") or m.get("marketId")
        if not market_id:
            continue

        question = m.get("question") or m.get("title") or ""
        yes_price = _extract_yes_price(m)
        end_date_str = (
            m.get("endDate") or m.get("end_date") or m.get("endDateIso") or ""
        )
        volume = float(m.get("volume", 0) or 0)

        rows.append((market_id, question, yes_price, end_date_str, volume, now_str))

    if not rows:
        return 0

    with get_connection(db_path) as conn:
        result = conn.executemany(
            """
            INSERT OR IGNORE INTO markets
              (id, question, yes_price, end_date, volume, parse_status, last_seen)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            rows,
        )
        # Also update last_seen for existing markets (to track still-active ones)
        conn.executemany(
            "UPDATE markets SET last_seen = ?, yes_price = ? WHERE id = ?",
            [(now_str, row[2], row[0]) for row in rows],
        )
        conn.commit()
        return result.rowcount


def _extract_yes_price(market: dict) -> float:
    """
    Extract the YES token price from various Gamma API response shapes.

    outcomePrices may be a JSON string '["0.38", "0.62"]' or an actual list.
    YES token is always the first element.
    Falls back to 0.5 (50/50) if no price is available.
    """
    import json as _json

    prices = market.get("outcomePrices")

    # Gamma API returns outcomePrices as a JSON-encoded string — parse it
    if isinstance(prices, str):
        try:
            prices = _json.loads(prices)
        except Exception:
            prices = None

    if prices and isinstance(prices, list) and len(prices) >= 1:
        try:
            return float(prices[0])
        except (ValueError, TypeError):
            pass

    # lastTradePrice or bestAsk as fallback
    for field in ("lastTradePrice", "bestAsk", "bestBid"):
        val = market.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass

    # Flat yes_price field
    yes_price = market.get("yes_price") or market.get("yesPrice")
    if yes_price is not None:
        try:
            return float(yes_price)
        except (ValueError, TypeError):
            pass

    return 0.5
