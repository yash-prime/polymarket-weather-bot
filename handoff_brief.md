# Claude Code Handoff Brief — Polymarket Weather Bot

> **Architecture Version:** v2.0 (Hardened)  
> **Date:** 2026-03-17  
> **Readiness Score:** 9 / 10

---

## WHAT WE ARE BUILDING

An automated Python trading bot that exploits pricing inefficiencies in Polymarket's weather prediction markets. It fetches real meteorological ensemble data from Open-Meteo (50–100 model members), NOAA, and ECMWF, computes accurate event probabilities using multi-source aggregation and Brier-score calibration, then uses an LLM layer (Ollama local + Claude API) to parse market questions and assess resolution risk. When the model's probability diverges from Polymarket's implied odds by ≥8% (adjusted for time-decay and liquidity), the bot sizes a fractional-Kelly trade, enforces risk guardrails, and executes via the Polymarket CLOB API using `py-clob-client`. A Streamlit dashboard provides real-time monitoring, and Telegram delivers async non-blocking alerts. The primary user is a solo technical trader running this on a non-US VPS.

---

## ARCHITECTURE SUMMARY

| Component | File | Responsibility |
|-----------|------|---------------|
| **Settings** | `config/settings.py` | All typed parameters, `.env` loading, startup validation |
| **DB Init** | `db/init.py` + `db/schema.sql` | SQLite schema creation, WAL mode, migrations |
| **CacheManager** | `data/cache/manager.py` | SQLite-backed TTL cache for all API responses |
| **RateLimiter** | `data/rate_limiter.py` | Per-source hourly call budget enforcement |
| **Open-Meteo** | `data/sources/open_meteo.py` | Forecast, Ensemble, Historical, Climate APIs |
| **NOAA** | `data/sources/noaa.py` | NWS official US forecasts |
| **ECMWF** | `data/sources/ecmwf.py` | Batch GRIB ingest job (every 6h) → writes to `ecmwf_snapshots` DB table |
| **Meteostat** | `data/sources/meteostat.py` | Historical station data for calibration only |
| **Ensemble** | `engine/ensemble.py` | Aggregates multi-source ensemble into weighted `ModelResult` |
| **Calibration** | `engine/calibration.py` | Daily Brier-score batch job; updates `calibration_weights` in DB |
| **Weather** | `engine/weather.py` | Top-level entry point; returns `ModelResult` or `None` |
| **Ollama Client** | `llm/ollama_client.py` | Ollama HTTP wrapper with startup health-check |
| **Parser** | `llm/parser.py` | Market question → structured JSON (Ollama → regex fallback → skip) |
| **Analyst** | `llm/analyst.py` | Resolution risk (Claude/Ollama), narration (Ollama), trade commentary (Claude/Ollama) |
| **Scanner** | `market/scanner.py` | Gamma API market fetch + filter; triggers async LLM parse job |
| **Signal** | `market/signal.py` | Edge calculation with corrected Kelly Criterion + clamped time-decay |
| **Risk Manager** | `trading/risk.py` | All guardrails; reads from `positions`/`paper_positions` DB; clamps final size |
| **Trader (Live)** | `trading/trader.py` | `py-clob-client` wrapper; place/cancel/stale-cleanup with retries |
| **Trader (Paper)** | `trading/paper_trader.py` | Same interface as trader.py; writes to `paper_*` tables; no CLOB calls |
| **Portfolio** | `trading/portfolio.py` | Unrealized + realized P&L; writes snapshots to `portfolio_snapshots` |
| **Notifications** | `notifications/telegram.py` | `asyncio.Queue` consumer; non-blocking; `NotificationEvent` enum |
| **Dashboard** | `dashboard/app.py` | Streamlit; read-only DB; controls write to `system_config` table |
| **Orchestrator** | `main.py` | APScheduler jobs, startup checks, kill switch check per job |

---

## KNOWN ISSUES — RESOLVED IN v2.0

All issues below were present in v1.0 and are now fixed in the updated `ARCHITECTURE.md`. Implement exactly as specified — do not revert to v1.0 patterns.

1. **[CRITICAL — FIXED] Kelly Criterion formula was wrong.**
   - Old: `kelly_criterion(adjusted_edge, market_price)` — passing edge as `p`
   - Correct: `b = (1 - price) / price`, `f* = 0.25 * (b*p - q) / b` where `p = model_prob`
   - See `market/signal.py` full pseudocode in ARCHITECTURE.md

2. **[CRITICAL — FIXED] Private key had no scope restriction or log filtering.**
   - `PRIVATE_KEY` consumed once in `ClobClient()` at startup — never stored again
   - Log formatter must redact regex `r'0x[0-9a-fA-F]{60,}'`
   - `.env` permission checked on startup (`chmod 600` warning)

3. **[HIGH — FIXED] `time_decay` formula produced negative values on long-dated markets.**
   - Old: `time_decay = confidence * (1 - 0.02 * days_to_resolve)` — goes negative for >50 days
   - Correct: `max(0.1, confidence * (1.0 - 0.02 * min(days_to_resolve, 45)))`

4. **[HIGH — FIXED] LLM parsing was inline in the scan loop (blocked on Ollama latency).**
   - LLM parsing now runs as a separate APScheduler job every 30 min
   - Scanner reads `parse_status="success"` markets from DB — never calls LLM directly

5. **[HIGH — FIXED] Risk Manager had no defined read path for live positions.**
   - Risk Manager reads from `positions` (live) or `paper_positions` (paper) DB tables
   - All writes to these tables are from Trader/PaperTrader only

6. **[HIGH — FIXED] `model_result.confidence` was undefined — field existed but derivation was missing.**
   - `confidence = max(0.0, min(1.0, 1.0 - (ensemble_std_dev / 0.5)))` — computed in `ensemble.py`

7. **[HIGH — FIXED] Kelly output was not explicitly clamped to `MAX_POSITION_USDC`.**
   - Risk Manager always applies `final_size = min(raw_kelly_size_in_usdc, MAX_POSITION_USDC)` as last step

8. **[HIGH — FIXED] ECMWF treated as a synchronous REST API — it is a GRIB batch download.**
   - ECMWF runs as an independent 6h job, writes to `ecmwf_snapshots` DB
   - Weather Engine reads ECMWF from DB only

9. **[HIGH — FIXED] Kill switch had no atomic guarantee.**
   - Kill switch: checked at top of every scan job iteration via `system_config` DB table
   - On `bot_halted=true`: calls `cancel_all_open_orders()` before stopping

10. **[HIGH — FIXED] No retry/timeout policy defined anywhere.**
    - All HTTP calls: 3 retries, exponential backoff (1s→10s), 10s timeout via `tenacity`
    - Per-component degraded behavior defined (see ARCHITECTURE.md Retry Policy section)

---

## TASK EXECUTION ORDER

```
TASK-001 → TASK-002 → TASK-003 → TASK-004 → TASK-005 →
TASK-006 → TASK-007 → TASK-008 → TASK-009 → TASK-010 →
TASK-011 → TASK-012 → TASK-013 → TASK-014 → TASK-015 →
TASK-016 → TASK-017 → TASK-018 → TASK-019 → TASK-020 →
TASK-021 → TASK-022 → TASK-023 → TASK-024 → TASK-025 →
TASK-026 → TASK-027 → TASK-028 → TASK-029
```

Pin dependencies and build the foundation first (TASK-001 through TASK-005). Never merge a task that touches financial calculation (signal, risk, trader) without its unit test passing.

---

## START HERE

**TASK-001 — `requirements.txt` + `config/settings.py`**

Done when: `requirements.txt` has all dependencies pinned to exact versions listed in ARCHITECTURE.md Tech Stack table. `config/settings.py` defines all 20+ parameters with typed defaults using `python-dotenv`. `settings.validate()` raises `ValueError` with a clear message if any required live-mode secret is missing. `python -c "from config import settings; settings.validate()"` runs without error in paper mode.

Build this first because every other module imports from `config.settings` — an unstructured config causes inconsistent parameter names across components, which is the most common source of integration bugs in multi-file projects.

---

## CONSTRAINTS

| Constraint | Detail |
|-----------|--------|
| Language | Python 3.11+ (strict) |
| Orders | Limit orders ONLY on CLOB — no market orders ever |
| Kelly fraction | 0.25x maximum — never increase without backtested evidence |
| Secrets | Via `.env` + `python-dotenv` only — never hardcoded, never logged |
| Blockchain | Polygon mainnet only (`chain_id=137`) |
| Dashboard | Read-only from DB — no shared in-memory state with `main.py` |
| Paper/Live isolation | Completely separate DB tables (`positions` vs `paper_positions`, `trades` vs `paper_trades`) |
| ECMWF | Batch ingest job only — never called inline from scan loop |
| System dep | `eccodes` binary must be installed before `cfgrib` — document in README + `setup.sh` |
| VPS region | Non-US (EU or Asia) to comply with Polymarket geo-restrictions |
| DB concurrency | WAL mode enabled; Dashboard is read-only; `main.py` is sole writer to core tables |
| Logging | All log formatters must redact private key pattern `r'0x[0-9a-fA-F]{60,}'` |
| Default mode | Bot starts in `TRADING_MODE=paper` — must be explicitly changed to `live` |
