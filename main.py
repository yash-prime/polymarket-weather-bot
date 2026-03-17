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


def _persist_weak_signal(market, model_result) -> None:
    """Persist model probability to signals table even when edge is too small to trade.

    Only inserts if no signal has been written for this market in the current scan
    window (last SCAN_INTERVAL_MINUTES minutes). This prevents stale weak signals
    from shadowing strong signals written earlier in the same cycle.
    """
    try:
        from db.init import get_connection
        with get_connection() as conn:
            # Skip insert if a fresher signal already exists for this market
            recent = conn.execute(
                """
                SELECT id FROM signals
                WHERE market_id = ?
                  AND created_at >= datetime('now', ? || ' minutes')
                LIMIT 1
                """,
                (market.id, f"-{settings.SCAN_INTERVAL_MINUTES}"),
            ).fetchone()
            if recent:
                return
            conn.execute(
                """
                INSERT INTO signals
                  (market_id, direction, adjusted_edge, model_prob, market_price, raw_kelly_size)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    market.id,
                    "YES" if model_result.probability >= 0.5 else "NO",
                    0.0,
                    model_result.probability,
                    market.yes_price,
                    0.0,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("main._persist_weak_signal: %s", exc)


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
                "SELECT id, question FROM markets "
                "WHERE parse_status IN ('pending', 'failed') LIMIT 50"
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


def _load_open_positions(mode: str) -> list[dict]:
    """Load open positions with market metadata for LLM portfolio manager."""
    positions_table = "positions" if mode == "live" else "paper_positions"
    try:
        from db.init import get_connection
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT p.market_id, p.direction, p.size, p.entry_price,
                       p.unrealized_pnl, m.question, m.yes_price, m.end_date, m.parsed
                FROM {positions_table} p
                JOIN markets m ON m.id = p.market_id
                WHERE p.size > 0
                """,  # noqa: S608
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("main._load_open_positions: DB read failed: %s", exc)
        return []


def _load_portfolio_snap(mode: str) -> dict | None:
    """Load most recent portfolio snapshot for LLM context."""
    try:
        from db.init import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE mode=? ORDER BY snapshot_at DESC LIMIT 1",
                (mode,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("main._load_portfolio_snap: %s", exc)
        return None


def _job_scan(clob_client) -> None:
    """
    Main scan cycle — LLM-driven portfolio management:
      1. Kill-switch check
      2. Fetch markets from Gamma (writes pending to DB)
      3. Score all active markets through weather engine + collect signals
      4. Load current portfolio
      5. Ask LLM: open / close / hold (with risk guardrails as safety net)
      6. Execute LLM actions
      7. Update prices + settle resolved positions
      8. Record scan time
    """
    if _is_halted():
        logger.info("main._job_scan: kill switch is active — skipping cycle")
        return

    mode = settings.TRADING_MODE

    try:
        from engine.weather import compute as weather_compute
        from market.scanner import get_active_markets, job_fetch_markets
        from market.signal import compute_signal
        from trading.llm_manager import analyze_and_decide
        from trading.portfolio_analyzer import build_portfolio_context
        from trading.risk import approve

        # 1. Fetch new markets from Gamma (writes parse_status='pending' to DB)
        new_count = job_fetch_markets()
        logger.info("main._job_scan: fetched %d new markets from Gamma", new_count)

        # 2. Score all active markets — collect signals without trading yet
        markets = get_active_markets()
        logger.info("main._job_scan: %d active markets to score", len(markets))

        new_signals: list[dict] = []
        for market in markets:
            try:
                model_result = weather_compute(market)
                if model_result is None:
                    continue

                signal = compute_signal(market, model_result)
                if signal is None:
                    _persist_weak_signal(market, model_result)
                    continue

                new_signals.append({
                    "market_id": market.id,
                    "direction": signal.direction,
                    "edge": signal.adjusted_edge,
                    "model_prob": signal.model_prob,
                    "market_price": signal.market_price,
                    "kelly_size": signal.raw_kelly_size,
                    "question": market.question,
                })
            except Exception as exc:  # noqa: BLE001
                logger.error("main._job_scan: error scoring market %s: %s", market.id, exc)

        logger.info("main._job_scan: %d tradeable signals collected", len(new_signals))

        # 3. Load current portfolio state
        open_positions = _load_open_positions(mode)
        snap = _load_portfolio_snap(mode)
        portfolio_summary = build_portfolio_context(open_positions, snap)

        # 4. Ask LLM what to do (open / close / hold)
        actions = analyze_and_decide(open_positions, new_signals, portfolio_summary, mode)

        # 5. Execute LLM actions with risk guardrails as safety net
        if mode == "paper":
            from trading.paper_trader import (
                close_position as paper_close,
                place_limit_order as paper_place,
            )
        else:
            from trading.trader import place_limit_order as live_place  # type: ignore[import]

        for action in actions:
            act = action.get("action", "")
            market_id = action.get("market_id", "")

            if act == "close":
                if mode == "paper":
                    paper_close(market_id, reason=action.get("reason", ""))
                else:
                    logger.info("main._job_scan: live close not implemented — skipping %s", market_id[:16])

            elif act == "open":
                # Find the original signal to build a proper Signal object for risk.approve()
                sig_data = next((s for s in new_signals if s["market_id"] == market_id), None)
                if sig_data is None:
                    continue

                from market.models import Signal
                sig = Signal(
                    market_id=market_id,
                    direction=action.get("direction", sig_data["direction"]),
                    raw_kelly_size=float(sig_data["kelly_size"]),
                    adjusted_edge=float(sig_data["edge"]),
                    model_prob=float(sig_data["model_prob"]),
                    market_price=float(sig_data["market_price"]),
                )

                # Safety net: still run through risk guardrails
                approved = approve(sig, mode)
                if approved is None:
                    logger.debug("main._job_scan: risk rejected LLM open for %s", market_id[:16])
                    continue

                # Override size with LLM recommendation if smaller (more conservative)
                final_size = min(float(action.get("size", approved.final_size)), approved.final_size)
                if final_size <= 0:
                    continue

                direction_word = "YES" if sig.direction == "YES" else "NO"
                rationale = (
                    f"Opened {direction_word} — model probability {sig.model_prob:.0%}, "
                    f"market pricing {sig.market_price:.0%}, edge {sig.adjusted_edge:+.0%}. "
                    f"{action.get('reason', '').strip()}"
                ).rstrip(". ") + "."

                try:
                    if mode == "live":
                        live_place(
                            clob_client,
                            market_id,
                            sig.direction,
                            final_size,
                            sig.market_price,
                        )
                    else:
                        paper_place(
                            market_id,
                            sig.direction,
                            final_size,
                            sig.market_price,
                            rationale,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error("main._job_scan: error placing order for %s: %s", market_id[:16], exc)

        # 6. Update paper position prices + settle resolved markets
        if mode == "paper":
            from trading.paper_trader import settle_resolved_positions, update_position_prices
            update_position_prices()
            settled = settle_resolved_positions()
            if settled:
                logger.info("main._job_scan: settled %d resolved paper positions", settled)

        # 7. Cancel stale orders (live only)
        if mode == "live" and clob_client is not None:
            from trading.trader import cancel_stale_orders
            cancel_stale_orders(
                clob_client,
                max_age_minutes=settings.STALE_ORDER_MAX_AGE_MIN,
            )

        # 8. Record scan completion time for dashboard countdown
        try:
            from db.init import get_connection
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO system_config (key, value) VALUES ('last_scan_at', datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            pass

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
