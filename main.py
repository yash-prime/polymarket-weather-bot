"""
main.py — APScheduler event loop for the Polymarket Weather Bot.

Jobs (AsyncIOScheduler):
  - scan_job          every 15 min   — fetch + score weather markets, place/cancel orders
  - llm_parse_job     every 30 min   — parse pending markets with Ollama/regex
  - ecmwf_ingest_job  every 6 h      — batch GRIB2 download + DB write
  - calibration_job   daily 02:00 UTC — Brier-score weight update
  - portfolio_job     every 5 min    — snapshot P&L to DB
  - stale_cleanup_job every 10 min   — cancel stale CLOB orders

Startup sequence:
  1. validate() settings (raises on missing live-mode secrets)
  2. db.init.init_db() — create schema, insert defaults
  3. health_check_ollama() — sets OLLAMA_DEGRADED if down
  4. health_check_clob()   — warns if CLOB unreachable in live mode
  5. health_check_telegram() — sends startup ping
  6. start scheduler, block until signal

Kill switch: every scan job reads system_config.bot_halted before proceeding.
"""
import asyncio
import logging
import os
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Install private-key redaction on all log handlers immediately after basicConfig
from config.log_filter import install_redacting_formatter  # noqa: E402
install_redacting_formatter()
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Kill-switch helper
# ---------------------------------------------------------------------------


def _is_halted(db_path: str | None = None) -> bool:
    """Return True if bot_halted flag is set in system_config."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'bot_halted'"
            ).fetchone()
        return row is not None and str(row["value"]).lower() in ("1", "true")
    except Exception as exc:  # noqa: BLE001
        logger.error("main._is_halted: DB read failed: %s", exc)
        return False  # Default to not halted if DB is unreachable


# ---------------------------------------------------------------------------
# Job: LLM parse pending markets
# ---------------------------------------------------------------------------


def _job_llm_parse() -> None:
    """Parse markets with parse_status='pending' using Ollama/regex."""
    try:
        from db.init import get_connection
        from llm.parser import parse

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, question FROM markets WHERE parse_status = 'pending' LIMIT 20"
            ).fetchall()

        if not rows:
            return

        logger.info("main._job_llm_parse: parsing %d pending markets", len(rows))
        for row in rows:
            result = parse(row["question"])
            new_status = result.get("parse_status", "failed")
            with get_connection() as conn:
                conn.execute(
                    "UPDATE markets SET parse_status = ?, parsed = ? WHERE id = ?",
                    (
                        new_status,
                        str(result) if new_status == "success" else None,
                        row["id"],
                    ),
                )
                conn.commit()

    except Exception as exc:  # noqa: BLE001
        logger.error("main._job_llm_parse: failed: %s", exc)


# ---------------------------------------------------------------------------
# Job: Scan markets + signal + risk + trade
# ---------------------------------------------------------------------------


def _job_scan(clob_client) -> None:
    """
    Main scan cycle:
      1. Kill-switch check
      2. Fetch markets from Gamma (writes pending to DB)
      3. Score active (parse_status=success) markets through weather engine
      4. Generate signal → risk approve → place order (paper or live)
      5. Cancel stale orders
    """
    if _is_halted():
        logger.info("main._job_scan: kill switch is active — skipping cycle")
        return

    mode = settings.TRADING_MODE

    try:
        from engine.weather import compute as weather_compute
        from market.scanner import get_active_markets, job_fetch_markets
        from market.signal import compute_signal
        from trading.risk import approve

        # 1. Fetch new markets from Gamma (writes parse_status='pending' to DB)
        new_count = job_fetch_markets()
        logger.info("main._job_scan: fetched %d new markets from Gamma", new_count)

        # 2. Process active (parsed) markets
        markets = get_active_markets()
        logger.info("main._job_scan: %d active markets to score", len(markets))

        for market in markets:
            try:
                model_result = weather_compute(market)
                if model_result is None:
                    continue

                signal = compute_signal(market, model_result)
                if signal is None:
                    continue

                approved = approve(signal, mode)
                if approved is None:
                    continue

                # Place order
                if mode == "live":
                    from trading.trader import place_limit_order
                    place_limit_order(
                        clob_client,
                        approved.signal.market_id,
                        approved.signal.direction,
                        approved.final_size,
                        approved.signal.market_price,
                    )
                else:
                    from trading.paper_trader import place_limit_order as paper_place
                    paper_place(
                        approved.signal.market_id,
                        approved.signal.direction,
                        approved.final_size,
                        approved.signal.market_price,
                    )

            except Exception as exc:  # noqa: BLE001
                logger.error("main._job_scan: error processing market %s: %s", market.id, exc)

        # 3. Cancel stale orders (live only)
        if mode == "live" and clob_client is not None:
            from trading.trader import cancel_stale_orders
            cancel_stale_orders(
                clob_client,
                max_age_minutes=settings.STALE_ORDER_MAX_AGE_MIN,
            )

    except Exception as exc:  # noqa: BLE001
        logger.error("main._job_scan: top-level failure: %s", exc)
        _send_alert(f"scan job failed: {exc}")


# ---------------------------------------------------------------------------
# Job: ECMWF batch ingest
# ---------------------------------------------------------------------------


def _job_ecmwf_ingest() -> None:
    try:
        from data.sources.ecmwf import run_ecmwf_ingest
        run_ecmwf_ingest()
    except Exception as exc:  # noqa: BLE001
        logger.error("main._job_ecmwf_ingest: failed: %s", exc)


# ---------------------------------------------------------------------------
# Job: Calibration
# ---------------------------------------------------------------------------


def _job_calibration() -> None:
    try:
        from engine.calibration import run_calibration_batch
        run_calibration_batch()
    except Exception as exc:  # noqa: BLE001
        logger.error("main._job_calibration: failed: %s", exc)


# ---------------------------------------------------------------------------
# Job: Stale order cleanup (paper mode no-op)
# ---------------------------------------------------------------------------


def _job_stale_cleanup(clob_client) -> None:
    if settings.TRADING_MODE != "live" or clob_client is None:
        return
    try:
        from trading.trader import cancel_stale_orders
        cancel_stale_orders(clob_client, max_age_minutes=settings.STALE_ORDER_MAX_AGE_MIN)
    except Exception as exc:  # noqa: BLE001
        logger.error("main._job_stale_cleanup: failed: %s", exc)


# ---------------------------------------------------------------------------
# Startup health checks
# ---------------------------------------------------------------------------


def health_check_ollama() -> None:
    """Verify Ollama is available; set OLLAMA_DEGRADED if not."""
    try:
        from llm.ollama_client import health_check
        health_check()
        logger.info("main.health_check_ollama: Ollama OK")
    except Exception as exc:  # noqa: BLE001
        logger.warning("main.health_check_ollama: Ollama unavailable — bot continues degraded: %s", exc)


def health_check_clob(clob_client) -> bool:
    """
    Verify CLOB connectivity.  Returns True if healthy.
    Only warns — does not abort startup.
    """
    if clob_client is None:
        if settings.TRADING_MODE == "live":
            logger.warning("main.health_check_clob: no CLOB client in live mode")
        return False
    try:
        clob_client.get_ok()
        logger.info("main.health_check_clob: CLOB OK")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("main.health_check_clob: CLOB unreachable: %s", exc)
        return False


def health_check_telegram() -> None:
    """Send startup ping via Telegram if configured."""
    try:
        _send_alert("Weather bot started (mode=%s)" % settings.TRADING_MODE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("main.health_check_telegram: failed: %s", exc)


def _send_alert(message: str) -> None:
    """Best-effort Telegram alert — never raises."""
    try:
        from notifications.telegram import send_alert
        send_alert(message)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CLOB client factory
# ---------------------------------------------------------------------------


def _build_clob_client():
    """
    Build and return a ClobClient for live mode.
    PRIVATE_KEY is consumed here and not stored beyond this call.
    Returns None in paper mode or on any error.
    """
    if settings.TRADING_MODE != "live":
        return None

    try:
        from py_clob_client.client import ClobClient as _ClobClient  # noqa: PLC0415
        from py_clob_client.clob_types import ApiCreds  # noqa: PLC0415

        client = _ClobClient(
            host=settings.CLOB_HOST,
            key=settings.PRIVATE_KEY,  # consumed once
            chain_id=settings.CHAIN_ID,
            creds=ApiCreds(
                api_key=settings.POLY_API_KEY,
                api_secret=settings.POLY_SECRET,
                api_passphrase=settings.POLY_PASSPHRASE,
            ),
        )
        return client
    except Exception as exc:  # noqa: BLE001
        logger.error("main._build_clob_client: failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    # 1. Validate settings
    settings.validate()
    logger.info("main: settings validated (mode=%s)", settings.TRADING_MODE)

    # 2. Init DB
    from db.init import init_db
    init_db()
    logger.info("main: database initialised")

    # 3. Build CLOB client (live only — private key consumed once here)
    clob_client = _build_clob_client()

    # 4. Health checks
    health_check_ollama()
    health_check_clob(clob_client)
    health_check_telegram()

    # 5. Build scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")

    # scan every 15 min
    scheduler.add_job(
        lambda: _job_scan(clob_client),
        IntervalTrigger(minutes=settings.SCAN_INTERVAL_MINUTES),
        id="scan_job",
        name="Market scan + signal + trade",
        max_instances=1,
        coalesce=True,
    )

    # LLM parse every 30 min
    scheduler.add_job(
        _job_llm_parse,
        IntervalTrigger(minutes=settings.LLM_PARSE_INTERVAL_MINUTES),
        id="llm_parse_job",
        name="LLM market parser",
        max_instances=1,
        coalesce=True,
    )

    # ECMWF ingest every 6 h
    scheduler.add_job(
        _job_ecmwf_ingest,
        IntervalTrigger(hours=6),
        id="ecmwf_ingest_job",
        name="ECMWF batch GRIB ingest",
        max_instances=1,
        coalesce=True,
    )

    # Calibration daily at 02:00 UTC
    scheduler.add_job(
        _job_calibration,
        CronTrigger(hour=2, minute=0),
        id="calibration_job",
        name="Brier score calibration",
        max_instances=1,
        coalesce=True,
    )

    # Portfolio snapshot every 5 min
    scheduler.add_job(
        lambda: __import__("trading.portfolio", fromlist=["job_portfolio_snapshot"])
        .job_portfolio_snapshot(settings.TRADING_MODE),
        IntervalTrigger(minutes=5),
        id="portfolio_job",
        name="Portfolio P&L snapshot",
        max_instances=1,
        coalesce=True,
    )

    # Stale order cleanup every 10 min
    scheduler.add_job(
        lambda: _job_stale_cleanup(clob_client),
        IntervalTrigger(minutes=settings.STALE_ORDER_CHECK_MINUTES),
        id="stale_cleanup_job",
        name="Stale order cleanup",
        max_instances=1,
        coalesce=True,
    )

    # 6. Start
    scheduler.start()
    logger.info("main: scheduler started with %d jobs", len(scheduler.get_jobs()))

    # 6a. Run all jobs immediately at startup — dashboard shows live data from launch
    loop = asyncio.get_event_loop()
    logger.info("main: startup — fetching markets...")
    await loop.run_in_executor(None, lambda: _job_scan(clob_client))
    logger.info("main: startup — parsing markets with LLM...")
    await loop.run_in_executor(None, _job_llm_parse)
    logger.info("main: startup — snapshotting portfolio...")
    await loop.run_in_executor(
        None,
        lambda: __import__("trading.portfolio", fromlist=["job_portfolio_snapshot"])
                .job_portfolio_snapshot(settings.TRADING_MODE),
    )

    # 7. Block until SIGINT / SIGTERM
    stop_event = asyncio.Event()

    def _on_signal(*_):
        logger.info("main: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, _on_signal)
    loop.add_signal_handler(signal.SIGTERM, _on_signal)

    await stop_event.wait()

    scheduler.shutdown(wait=False)
    logger.info("main: scheduler stopped — goodbye")


if __name__ == "__main__":
    asyncio.run(main())
