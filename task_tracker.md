# Task Tracker — Polymarket Weather Bot

> **Architecture Version:** v2.0  
> **Last Updated:** 2026-03-17  
> **Total Tasks:** 29  
> **Readiness Score:** 9 / 10

---

## Milestone Summary

| Milestone | Tasks | Description |
|-----------|-------|-------------|
| M1 — Foundation | TASK-001–004, TASK-022 | Settings, DB, cache, shared dataclasses, rate limiter |  <!-- FIXED: TASK-022 (RateLimiter) moved here from M7 since data sources in M2 depend on it -->
| M2 — Data Layer | TASK-005–008 | All weather API connectors |
| M3 — Weather Engine | TASK-009–011 | Ensemble aggregation, calibration, probability output |
| M4 — LLM Layer | TASK-012–014 | Ollama client, question parser, analyst |
| M5 — Scanner & Signal | TASK-015–016 | Gamma scanner, edge/Kelly signal |
| M6 — Risk & Trading | TASK-017–020 | Risk manager, live trader, paper trader, portfolio |
| M7 — Orchestration | TASK-021 | main.py event loop |  <!-- FIXED: TASK-022 moved to M1 -->
| M8 — Notifications & Dashboard | TASK-023–024 | Telegram alerts, Streamlit UI |
| M9 — Hardening & Testing | TASK-025–029 | Unit tests, integration test, security, deps |

---

## Task Tracker

| ID | Milestone | Task | Done When | Depends On | Risk Flag | Status | Assignee |
|----|-----------|------|-----------|------------|-----------|--------|----------|
| TASK-001 | M1 — Foundation | `requirements.txt` + `config/settings.py` | All 20+ params typed with defaults; `python-dotenv` loads `.env`; `validate()` raises `ValueError` for missing live-mode secrets; `python -c "from config import settings; settings.validate()"` passes in paper mode | none | ⚠ CRITICAL: private key scope | DONE | |
| TASK-002 | M1 — Foundation | DB schema + `db/init.py` | `schema.sql` defines all 14 tables (markets, signals, trades, paper_trades, positions, paper_positions, portfolio_snapshots, calibration_weights, ecmwf_snapshots, api_cache, llm_cache, system_config, schema_version, market_overrides); WAL mode enabled; `init.py` runs on first launch; default `system_config` rows inserted | none | | DONE | |  <!-- FIXED: was "14 tables" but listed only 13; added missing market_overrides table (referenced by Dashboard Controls) to make count correct at 14 -->
| TASK-003 | M1 — Foundation | `CacheManager` | `get(source, key)` returns cached value or `None` on miss/expiry; `set(source, key, value, ttl_seconds)` writes to `api_cache` table; thread-safe; per-source TTL defaults in `settings.py`; unit tests cover hit, miss, expiry | TASK-002 | | DONE | |
| TASK-004 | M1 — Foundation | Shared dataclasses | `engine/models.py` defines `ModelResult(probability, confidence, ci_low, ci_high, members_count, sources, degraded_sources)`; `market/models.py` defines `Market` and `Signal`; `trading/models.py` defines `ApprovedSignal`; all typed; unit tests verify field types and defaults; importable project-wide | none | | DONE | |  <!-- FIXED: ApprovedSignal moved to trading/models.py (canonical location per architecture); was listed under market/models.py -->
| TASK-005 | M2 — Data Layer | `open_meteo.py` — all 4 APIs | Fetches Forecast, Ensemble, Historical, Climate endpoints; returns typed dicts; `CacheManager` used (ensemble TTL=1h, historical TTL=24h); `tenacity` retry (3x, exp backoff, 10s timeout); unit tests mock HTTP; `RateLimiter` check before each call | TASK-003, TASK-022 | | DONE | |  <!-- FIXED: added TASK-022 (RateLimiter) as dependency; done condition requires RateLimiter but it was missing from deps -->
| TASK-006 | M2 — Data Layer | `noaa.py` — NWS API | Fetches NWS forecast + alert endpoints; returns typed dicts; cached (TTL=1h); `tenacity` retry (3x); 503 returns `None` with warning; `RateLimiter` enforced; unit tests included | TASK-003, TASK-022 | | DONE | |  <!-- FIXED: added TASK-022 (RateLimiter) as dependency -->
| TASK-007 | M2 — Data Layer | `ecmwf.py` — batch GRIB ingest | Downloads GRIB2 via `ecmwf-opendata`; parses with `cfgrib`; extracts grid-point values; writes to `ecmwf_snapshots` DB table; runs as standalone 6h APScheduler job; NOT called inline from scan loop; unit test with fixture GRIB file | TASK-002, TASK-003, TASK-022 | ⚠ HIGH: ECMWF is batch not REST | DONE | |  <!-- FIXED: added TASK-022 (RateLimiter) as dependency -->
| TASK-008 | M2 — Data Layer | `meteostat.py` — historical stations | Wraps `meteostat` library for lat/lon/date-range historical observations; returns `pd.DataFrame(date, tmax, tmin, prcp, wspd)`; used by `calibration.py` only; cached (TTL=24h); unit tests with mocked data | TASK-003 | | DONE | |
| TASK-009 | M3 — Weather Engine | `ensemble.py` — multi-model aggregation | Takes Open-Meteo + NOAA + ECMWF DB snapshot outputs; weights by `calibration_weights` (uniform default if no data); computes aggregate probability + `confidence = max(0, min(1, 1 - std_dev/0.5))`; returns `ModelResult`; unit tests with fixture arrays | TASK-004, TASK-005, TASK-007 | ⚠ HIGH: confidence field was undefined | DONE | |
| TASK-010 | M3 — Weather Engine | `calibration.py` — Brier score tuning | Daily 02:00 UTC APScheduler job; reads resolved markets from `trades`; fetches actuals from Meteostat; computes Brier score per source+region+season; converts to `weight = 1/(BS + epsilon)`; normalizes to sum=1; writes to `calibration_weights`; `get_weights(region, season)` returns dict; unit tests verify Brier score math | TASK-002, TASK-008, TASK-009 | | DONE | |
| TASK-011 | M3 — Weather Engine | `weather.py` — probability output | Given `Market` with valid `parsed` JSON, calls `ensemble.py`; returns `ModelResult` or `None` if zero sources available; logs missing sources; adds NOAA as supplementary check for US lat/lon; unit tests: normal case, single-source fallback, all-sources-failed | TASK-009, TASK-006 | | TODO | |
| TASK-012 | M4 — LLM Layer | `ollama_client.py` — client + health-check | `generate(prompt) → str`; on init: GET `/api/tags`, verify `llama3.1:8b` in list, raise `OllamaUnavailableError` if absent; 30s per-call timeout; unit tests mock HTTP; sets `OLLAMA_DEGRADED` flag on failure (bot continues without narration) | TASK-001 | | TODO | |
| TASK-013 | M4 — LLM Layer | `parser.py` — question parser + regex fallback | `parse(question_str) → dict`; path 1: Ollama JSON parse; path 2: regex for common patterns (`"exceed X°F"`, `"above X inches"`, `"below X°F"`); path 3: `parse_status="failed"` (market skipped); all successes cached in `llm_cache` by `sha256(question)`; unit tests: normal, regex fallback, failure case | TASK-012, TASK-002 | ⚠ HIGH: Ollama fallback was undefined | TODO | |
| TASK-014 | M4 — LLM Layer | `analyst.py` — resolution risk + narration + commentary | `resolution_risk(text) → dict`: Claude if key set, else Ollama; `narrate_ensemble(ModelResult) → str`: Ollama always; `trade_commentary(signal, market, model_result) → str`: Claude→Ollama fallback; all functions tested with mocked API clients; if `ANTHROPIC_API_KEY` absent, Ollama used with WARNING log | TASK-012, TASK-004 | ⚠ HIGH: Claude fallback was undefined | TODO | |
| TASK-015 | M5 — Scanner & Signal | `scanner.py` — Gamma API market scanner | Polls Gamma API; filters: `volume >= MIN_MARKET_VOLUME`, `days_to_resolve >= MIN_DAYS_TO_RESOLVE`, category="weather"; writes new markets to `markets` table as `parse_status="pending"`; does NOT call LLM inline; `RateLimiter` enforced; returns `list[Market]` with `parse_status="success"` for scan cycle; unit tests with fixture API responses | TASK-001, TASK-002, TASK-022 | ⚠ HIGH: LLM was inline in scan loop | TODO | |  <!-- FIXED: removed TASK-013 (parser) dependency; scanner does NOT call LLM — it writes pending markets to DB. Added TASK-022 (RateLimiter) which scanner uses -->
| TASK-016 | M5 — Scanner & Signal | `signal.py` — edge + corrected Kelly | `compute_signal(market, model_result) → Signal | None`; `days_to_resolve = max(0, ...)` enforced; `time_decay = max(0.1, confidence * (1 - 0.02 * min(days, 45)))`; Kelly: `b=(1-price)/price`, `f*=0.25*(b*p-q)/b`; returns `Signal` or `None`; unit tests: zero edge, negative time_decay protection, Kelly direction correctness, threshold boundary, 60-day market | TASK-004, TASK-001 | ⚠ CRITICAL: Kelly formula was wrong; HIGH: time_decay overflow | TODO | |
| TASK-017 | M6 — Risk & Trading | `risk.py` — Risk Manager | `approve(signal, mode) → ApprovedSignal | None`; reads `positions`/`paper_positions` per mode (never in-memory); rejects if: open_count >= MAX, daily_loss >= limit, same-event correlation; `final_size = min(bankroll * kelly_fraction, MAX_POSITION_USDC)`; unit tests: all rejection conditions, size clamping, paper vs live isolation | TASK-016, TASK-002 | ⚠ HIGH: read path undefined; HIGH: Kelly not clamped | TODO | |
| TASK-018 | M6 — Risk & Trading | `trader.py` — CLOB live wrapper | `place_limit_order(market_id, direction, size, price) → order_id`; `cancel_order(order_id)`; `cancel_all_open_orders()`; `cancel_stale_orders(max_age_minutes)`; all calls: 3 retries, 15s timeout; POST_CANCEL_WAIT_SECONDS sleep after cancels; all outcomes written to `trades` DB; unit tests mock `py-clob-client` | TASK-001, TASK-002, TASK-017 | ⚠ HIGH: kill switch atomicity; stale order re-trigger | TODO | |
| TASK-019 | M6 — Risk & Trading | `paper_trader.py` — paper mode | Same interface as `trader.py`; writes to `paper_trades` + `paper_positions`; simulates fill at current YES mid-price; no CLOB API calls; unit tests verify paper positions never affect live risk checks | TASK-018 | ⚠ HIGH: paper mode was unarchitected | TODO | |
| TASK-020 | M6 — Risk & Trading | `portfolio.py` — P&L tracking | `get_snapshot(mode) → PortfolioSnapshot`; reads `positions`/`paper_positions` (by mode); computes: total equity, unrealized P&L (mark-to-market via Gamma latest price), realized P&L, daily P&L, daily loss pct; writes to `portfolio_snapshots` every 5 min; unit tests with fixture trade data | TASK-019, TASK-002 | | TODO | |
| TASK-021 | M7 — Orchestration | `main.py` — APScheduler event loop | `AsyncIOScheduler` with jobs: scan (15min), llm_parse (30min), ecmwf_ingest (6h), calibration (daily 02:00 UTC), portfolio_snapshot (5min), stale_cleanup (10min); startup: validate env, db.init(), health_check_ollama(), health_check_clob(), health_check_telegram(); kill switch checked at top of every scan job; all jobs wrapped in try/except with Telegram alert on failure | TASK-011, TASK-015, TASK-016, TASK-017, TASK-018, TASK-020 | ⚠ CRITICAL: orchestrator was undefined; HIGH: kill switch | TODO | |
| TASK-022 | M1 — Foundation | `RateLimiter` utility | Thread-safe per-source hourly budget; `check_and_record(source) → bool`; per-source limits in `settings.py`; integrated into all `data/sources/*.py` modules; unit tests verify budget enforcement and reset after 1h | TASK-001 | ⚠ MEDIUM: no rate limit tracking | DONE | |  <!-- FIXED: moved from M7 (Orchestration) to M1 (Foundation) since data sources in M2 depend on it -->
| TASK-023 | M8 — Notifications | `telegram.py` — async non-blocking alerts | `NotificationEvent` enum defined in `notifications/events.py`; all sends via `asyncio.Queue` consumed by background task; never blocks main loop; `TELEGRAM_CHAT_ID` whitelist enforced; `send_startup_ping()` called at bot start; unit tests mock Telegram bot API | TASK-001 | ⚠ HIGH: events undefined; sync blocking risk | TODO | |
| TASK-024 | M8 — Dashboard | `dashboard/app.py` — Streamlit UI | All panels: header bar, active markets table, ensemble chart, LLM analysis, trade log, bot status; reads exclusively from SQLite DB; controls write to `system_config` table (kill switch, mode toggle, threshold slider, position size, per-market lock); auto-refreshes every 30s; unit test: verify no direct state mutation | TASK-020, TASK-002, TASK-014 | ⚠ HIGH: dashboard data source undefined; MEDIUM: config write race | TODO | |
| TASK-025 | M9 — Hardening | Unit tests — `engine/` | `tests/test_weather.py` achieves ≥90% coverage of `engine/weather.py`, `engine/ensemble.py`, `engine/calibration.py`; cases: single-source, zero-members, confidence=0, all-sources degraded; all HTTP and DB mocked via `conftest.py` fixtures | TASK-011, TASK-010 | | TODO | |
| TASK-026 | M9 — Hardening | Unit tests — `signal.py` + `risk.py` | `tests/test_signal.py`: zero edge, correct Kelly direction (YES+NO), time_decay clamp at 60 days, threshold boundary; `tests/test_risk.py`: all rejection conditions, size clamping, paper vs live table isolation | TASK-016, TASK-017 | ⚠ CRITICAL: Kelly formula; HIGH: time_decay | TODO | |
| TASK-027 | M9 — Hardening | Integration test — paper E2E | `tests/test_integration.py` runs full pipeline in paper mode with fixture Gamma response + mocked Open-Meteo data; verifies: market scanned → LLM parsed → weather computed → signal generated → risk approved → paper trade placed → portfolio updated → `portfolio_snapshots` table written; zero real API calls | TASK-021, TASK-019 | | TODO | |  <!-- FIXED: removed TASK-024 (dashboard) dependency; E2E test does not test dashboard -->
| TASK-028 | M9 — Hardening | Security: private key policy | `PRIVATE_KEY` consumed once in `ClobClient()` in `main.py` — not stored beyond call; log formatter redacts `r'0x[0-9a-fA-F]{60,}'`; startup warns if `.env` is world-readable; `.env.example` lists all required + optional vars with descriptions; `.gitignore` includes `.env`, `*.db`, `.env.local` | TASK-001 | ⚠ CRITICAL: private key handling | TODO | |
| TASK-029 | M9 — Hardening | Pin all `requirements.txt` deps | All packages pinned to versions in ARCHITECTURE.md Tech Stack table; `pip check` passes with no conflicts; README documents `eccodes` system dep install commands for Ubuntu + macOS; `scripts/setup.sh` created for automated system dep install | none | ⚠ HIGH: unpinned py-clob-client | TODO | |

---

## Critical Path

```
TASK-001 → TASK-022 → TASK-002 → TASK-003 → TASK-005 → TASK-009 → TASK-011
                    ↘                                                       ↘
                     TASK-004 → TASK-016 → TASK-017 → TASK-018 → TASK-021
                                                    ↗
                     TASK-015 ──────────────────────
```
<!-- FIXED: added TASK-022 after TASK-001 in critical path; removed TASK-013→TASK-015 link since scanner doesn't depend on parser -->

Estimated critical path length: **14 tasks** from TASK-001 to TASK-021.  <!-- FIXED: was 13, now 14 with TASK-022 added -->

---

## Risk Flag Legend

| Symbol | Meaning |
|--------|---------|
| ⚠ CRITICAL | Was a CRITICAL severity finding in Phase 1 audit |
| ⚠ HIGH | Was a HIGH severity finding in Phase 1 audit |
| ⚠ MEDIUM | Was a MEDIUM severity finding in Phase 1 or Phase 2 audit |
