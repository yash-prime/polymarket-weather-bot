# Polymarket Weather Bot — System Architecture

> **Status:** Ready for Implementation  
> **Last Updated:** 2026-03-17  
> **Author:** Jarvis (AI Architecture Design) — Reviewed & Hardened v2.0  
> **Readiness Score:** 9 / 10

---

## 📌 Overview

An automated trading bot that exploits pricing inefficiencies in [Polymarket](https://polymarket.com/predictions/weather) weather prediction markets.

**The Edge:** Most market participants price weather markets based on intuition. This bot uses real meteorological ensemble models (the same data professional forecasters use) to compute accurate probabilities, then trades when Polymarket's implied odds diverge significantly from the model's output.

---

## 🎯 Strategy

```
market_price  = Polymarket YES price (0.00–1.00)
model_prob    = ensemble weather model probability
edge          = model_prob - market_price

if abs(adjusted_edge) > MIN_EDGE_THRESHOLD (default: 0.08):
    → generate trade signal
    → size via Kelly Criterion (0.25x fractional)
    → enforce risk guardrails
    → execute via CLOB API (limit orders only)
```

---

## 🏗️ System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                         DATA LAYER                               │
│                                                                  │
│  Open-Meteo     NOAA NWS     ECMWF Open    Meteostat    Others  │
│  Forecast API   api.weather  Data (AIFS)   (Historical)  ...    │
│  Ensemble API   .gov         ecmwf.int     Python lib           │
│  Historical API              [BATCH/6h]                         │
│  Climate API                                                     │
│                                                                  │
│  All sources: 3-retry + exponential backoff + 10s timeout       │
│  Cached in api_cache.db with per-source TTL                     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                      WEATHER ENGINE                              │
│  • Fetches forecasts from all sources (reads ECMWF from DB)      │
│  • Runs ensemble probability distribution (ECMWF+GFS+ICON)       │
│  • Weights sources by Brier-score calibration per region/season  │
│  • Output: ModelResult{probability, confidence, ci_low, ci_high} │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                       LLM LAYER (ASYNC, DECOUPLED)               │
│  Ollama (local, Llama 3.1 8B):                                   │
│  • Parse market question → structured JSON (+ regex fallback)    │
│  • Flag resolution ambiguity                                     │
│  • Narrate ensemble output for dashboard                         │
│  Claude API (spot calls, high-stakes):                           │
│  • Complex ambiguity decisions (fallback: Ollama)               │
│  • Trade rationale generation (fallback: Ollama)                 │
│  ⚠ Runs as background pre-processing job — NOT inline with scan  │
└──────────┬───────────────────────────────┬───────────────────────┘
           │                               │
┌──────────▼──────────┐        ┌───────────▼─────────────┐
│   MARKET SCANNER    │        │     SIGNAL ENGINE        │
│                     │        │                          │
│ Gamma API (public)  │        │ edge = model_prob - mkt  │
│ Find active weather │        │ Kelly Criterion sizing   │
│ markets             │        │ [CORRECTED FORMULA]      │
│ Filter: liquidity,  │        │ Time-decay (clamped)     │
│ expiry, volume      │        │ Liquidity adjustment     │
│ Reads LLM parse     │        │ Confidence weighting     │
│ from DB (async)     │        │                          │
└──────────┬──────────┘        └───────────┬──────────────┘
           └──────────────┬────────────────┘
                          │
               ┌──────────▼──────────┐
               │    RISK MANAGER     │
               │                     │
               │ Reads: positions DB │
               │ • Max position/mkt  │
               │ • Daily loss limit  │
               │ • Max open trades   │
               │ • Correlation check │
               │ • Kelly guardrails  │
               │ • Clamps final size │
               └──────────┬──────────┘
                          │
               ┌──────────▼──────────┐
               │   TRADING ENGINE    │
               │                     │
               │ py-clob-client      │
               │ Polygon (chain 137) │
               │ USDC settlements    │
               │ Limit orders only   │
               │ Stale order cleanup │
               │ POST_CANCEL_WAIT    │
               │ Kill switch aware   │
               └──────────┬──────────┘
                          │
┌─────────────────────────▼────────────────────────────────────────┐
│                  STREAMLIT DASHBOARD (24/7)                       │
│                                                                  │
│  Portfolio Overview | Active Markets | Ensemble Charts           │
│  Trade Log | Bot Status | P&L History                           │
│  Kill Switch | Paper Mode | Manual Overrides | Threshold Tuning  │
│                                                                  │
│  ⚠ Read-only from DB. Controls write to system_config table.    │
└─────────────────────────┬────────────────────────────────────────┘
                          │
               ┌──────────▼──────────┐
               │  TELEGRAM ALERTS    │
               │                     │
               │ Async event queue   │
               │ Non-blocking sends  │
               │ Trade executed      │
               │ Daily P&L summary   │
               │ Risk warnings       │
               │ Model disagreements │
               │ Component degraded  │
               └─────────────────────┘
```

---

## 📡 Data Sources

### Tier 1 — Primary Forecast Models (all free, no API key required)

| Source | URL | What it provides | Cache TTL | Notes |
|--------|-----|-----------------|-----------|-------|
| **Open-Meteo Forecast API** | `open-meteo.com/en/docs` | Hourly forecast, 16 days, global | 1h | Primary source |
| **Open-Meteo Ensemble API** | `open-meteo.com/en/docs/ensemble-api` | 50–100 perturbed model members (ECMWF ENS, GFS ENS, ICON ENS) | 1h | Core probability engine |
| **Open-Meteo Historical API** | `open-meteo.com/en/docs/historical-weather-api` | Hourly history since 1940 | 24h | Model calibration |
| **Open-Meteo Climate API** | `open-meteo.com/en/docs/climate-api` | Long-run climate normals | 24h | Context / anomaly detection |
| **NOAA NWS API** | `api.weather.gov` | US official forecasts, alerts | 1h | US market authority |
| **ECMWF Open Data** | `ecmwf.int/en/forecasts/datasets/open-data` | Real-time IFS + AIFS forecasts | **Batch/6h** | **GRIB2 format — separate ingestion job** |

> ⚠ **ECMWF Note:** ECMWF delivers GRIB2/NetCDF files via a download SDK (`ecmwf-opendata`), NOT a REST API. It runs as a standalone APScheduler job every 6 hours, parses data with `cfgrib`, and writes processed results to the `ecmwf_snapshots` DB table. The main scan loop reads ECMWF from DB only — never calls it live.

### Tier 2 — Supplementary & Validation (free tiers, optional)

| Source | Free Limit | Cache TTL | What it provides |
|--------|-----------|-----------|-----------------| 
| **Meteostat** (Python lib) | Unlimited | 24h | Historical station observations — used by calibration only |
| **OpenWeatherMap** | 1M calls/month | 1h | Current conditions, corroboration |
| **WeatherAPI** | 1M calls/month | 1h | Current + 3-day, global |
| **Visual Crossing** | 1000 calls/day | 24h | Strong historical data |
| **MET Norway (Yr.no)** | Unlimited | 1h | 48h global forecast, no key |

> **Tier 2 sources are optional** — the system functions fully on Tier 1 alone. Tier 2 connectors are future enhancements and not part of MVP scope.

---

## 🔧 Shared Data Contracts

All components exchange typed dataclasses. These are the canonical schemas — every module must import from these definitions, never define its own.

### `ModelResult` — output of Weather Engine

```python
# engine/models.py
from dataclasses import dataclass, field

@dataclass
class ModelResult:
    probability: float          # 0.0–1.0 — P(event occurs)
    confidence: float           # 0.0–1.0 — derived: 1 - (std_dev / 0.5), clamped to [0,1]
    ci_low: float               # Lower bound of 90% confidence interval
    ci_high: float              # Upper bound of 90% confidence interval
    members_count: int          # Number of ensemble members used
    sources: list[str]          # e.g. ["open_meteo_ensemble", "noaa"]
    degraded_sources: list[str] = field(default_factory=list)  # Sources that failed
```

### `Market` — output of Market Scanner

```python
# market/models.py
from dataclasses import dataclass
from datetime import datetime

@dataclass
class Market:
    id: str                     # Gamma/Polymarket market ID
    question: str               # Raw question string from Gamma API
    yes_price: float            # Current YES price (0.0–1.0)
    end_date: datetime          # Resolution date/time (UTC)
    volume: float               # Total USDC volume
    parsed: dict | None         # LLM-parsed structured JSON (None = not yet parsed)
    parse_status: str           # "success" | "regex_fallback" | "failed" | "pending"
    resolution_risk: str | None # "LOW" | "MEDIUM" | "HIGH" | None
```

### `Signal` — output of Signal Engine

```python
# market/models.py
@dataclass
class Signal:
    market_id: str
    direction: str              # "YES" | "NO"
    raw_kelly_size: float       # Kelly output in USDC (before risk clamping)
    adjusted_edge: float        # Final edge after time-decay and liquidity penalty
    model_prob: float           # From ModelResult
    market_price: float         # From Market
```

### `ApprovedSignal` — output of Risk Manager

```python
# trading/models.py
@dataclass
class ApprovedSignal:
    signal: Signal
    final_size: float           # min(raw_kelly_size, MAX_POSITION_USDC)
    mode: str                   # "live" | "paper"
```

### LLM Parsed Market JSON Schema

```json
{
  "city": "Chicago",
  "lat": 41.88,
  "lon": -87.63,
  "metric": "temperature_2m_max",
  "threshold": 85,
  "unit": "fahrenheit",
  "operator": ">",
  "window_start": "2026-06-10",
  "window_end": "2026-06-15",
  "aggregation": "any",
  "resolution_source": "nws_official | metar | model_grid | unknown",
  "parse_status": "success | regex_fallback | failed"
}
```

> If `resolution_source = "unknown"`:  
> • Run Claude resolution risk check  
> • If `risk_level = "HIGH"` → skip market entirely  
> • If `risk_level = "MEDIUM"` → widen edge threshold by +4% for this market  
> • Log all unknowns to `llm_cache` table

---

## 🔍 What is Ensemble Forecasting?

Standard forecast = one model run = one prediction.

**Ensemble forecasting** = run the same model **50+ times** with slightly different initial conditions, representing uncertainty in the atmosphere. The spread of outcomes gives a **true probability distribution**.

```
ECMWF ENS (51 members) example:
  Member 1:  NYC temp max = 88°F  ─┐
  Member 2:  NYC temp max = 91°F   │
  Member 3:  NYC temp max = 86°F   ├─ Distribution
  ...                              │
  Member 51: NYC temp max = 93°F  ─┘

P(temp_max > 90°F on June 15) = count(members > 90) / 51 = 36%
```

We combine **multiple ensemble systems** (ECMWF + GFS + ICON) and weight by historical accuracy per region/season using Brier score calibration.

**Confidence derivation:**
```python
confidence = max(0.0, min(1.0, 1.0 - (ensemble_std_dev / 0.5)))
# ensemble_std_dev = std deviation of probability across all members
# std_dev = 0.0 → confidence = 1.0 (all members agree)
# std_dev = 0.5 → confidence = 0.0 (maximum disagreement)
```

---

## 🧠 LLM Layer Design

### Purpose
The LLM handles tasks that are brittle to hardcode with regex/rules — especially natural language understanding of market questions.

### ⚠ Execution Model: ASYNC, DECOUPLED FROM SCAN LOOP

LLM parsing is **NOT** called inline during the market scan cycle. It runs as a separate APScheduler background job every `LLM_PARSE_INTERVAL_MINUTES` (default: 30). The scanner reads parsed results from the `markets` DB table.

```
Background Job (every 30 min):
  Scanner → fetches raw Gamma markets → writes to markets table (parse_status="pending")
  LLM Job → reads pending rows → parses → updates markets table (parse_status="success"|"failed")

Scan Cycle (every 15 min):
  Scanner → reads markets with parse_status="success" only
```

### Task 1: Market Question Parser
**Input:** Raw Polymarket question string  
**Output:** Structured JSON stored in `markets.parsed` column  
**Fallback chain:**
1. Ollama (Llama 3.1 8B) — primary
2. Regex rule-based parser — for common patterns (`"exceed X°F"`, `"above X inches"`, `"below X°F"`)
3. Mark as `parse_status = "failed"` — market skipped, logged

### Task 2: Resolution Risk Analysis
Reads market resolution criteria and flags ambiguity.
```json
{
  "risk_level": "MEDIUM",
  "reason": "Market says 'official NWS reading' but doesn't specify station",
  "recommendation": "Widen edge threshold before trading"
}
```
**Routing:** Claude Sonnet API → fallback: Ollama if `ANTHROPIC_API_KEY` unset (logged warning).

### Task 3: Ensemble Narration (Dashboard)
Converts raw model data into plain-English summaries for the dashboard and Telegram alerts.  
**Routing:** Ollama always (high frequency, low stakes).

### Task 4: Trade Decision Commentary
For each executed trade, generates a plain-English rationale logged to `trades` DB table.  
**Routing:** Claude Sonnet API → fallback: Ollama if `ANTHROPIC_API_KEY` unset.

### LLM Routing Strategy

| Task | Primary Model | Fallback | Frequency |
|------|--------------|---------|-----------|
| Market question parsing | Ollama (Llama 3.1 8B, local) | Regex rules | Per-market, async batch |
| Ensemble narration | Ollama (local) | None (skip dashboard narration) | Per-scan cycle |
| Resolution risk analysis | Claude Sonnet API | Ollama | Per-new-market |
| Trade rationale | Claude Sonnet API | Ollama | Per-trade |

### Ollama Health Check
On startup, `ollama_client.py` calls `GET http://localhost:11434/api/tags` and verifies `llama3.1:8b` is listed. Raises `OllamaUnavailableError` with clear message if missing. Bot can still run without Ollama (degraded: regex-only parsing, no narration) — logged to Telegram.

---

## 📊 Signal Engine Logic

```python
# market/signal.py
from engine.models import ModelResult
from market.models import Market, Signal
from config.settings import MIN_EDGE_THRESHOLD, KELLY_FRACTION

def compute_signal(market: Market, model_result: ModelResult) -> Signal | None:
    market_price = market.yes_price          # 0.0 – 1.0
    model_prob   = model_result.probability  # 0.0 – 1.0
    confidence   = model_result.confidence   # 0.0 – 1.0 (pre-computed in ensemble.py)

    raw_edge = model_prob - market_price

    # Time decay: reduce confidence further from resolution
    # CLAMPED: prevents negative values on long-dated markets (>45 days)
    days_to_resolve = max(0.0, (market.end_date - datetime.utcnow()).total_seconds() / 86400)
    time_decay = max(0.1, confidence * (1.0 - 0.02 * min(days_to_resolve, 45)))

    # Liquidity penalty: thin books need bigger edge
    liquidity_penalty = 0.02 if market.volume < 1000 else 0.0

    adjusted_edge = raw_edge * time_decay - liquidity_penalty

    if abs(adjusted_edge) >= MIN_EDGE_THRESHOLD:
        direction = "YES" if adjusted_edge > 0 else "NO"

        # ✅ CORRECTED Kelly Criterion for binary prediction markets
        # f* = (b*p - q) / b  where b = (1-price)/price, p = model_prob, q = 1-model_prob
        p = model_prob if direction == "YES" else (1.0 - model_prob)
        q = 1.0 - p
        b = (1.0 - market_price) / market_price if direction == "YES" else market_price / (1.0 - market_price)
        full_kelly = (b * p - q) / b
        raw_kelly_size = max(0.0, KELLY_FRACTION * full_kelly)  # KELLY_FRACTION = 0.25

        return Signal(
            market_id=market.id,
            direction=direction,
            raw_kelly_size=raw_kelly_size,   # fraction of bankroll — Risk Manager applies $cap
            adjusted_edge=adjusted_edge,
            model_prob=model_prob,
            market_price=market_price,
        )

    return None  # no trade
```

---

## ⚖️ Risk Manager Rules

| Rule | Default | Config Key | Description |
|------|---------|-----------|-------------|
| Max position per market | $50 USDC | `MAX_POSITION_USDC` | Single market exposure cap |
| Max open positions | 5 | `MAX_OPEN_POSITIONS` | Concurrent position limit |
| Daily loss limit | 10% of portfolio | `DAILY_LOSS_LIMIT_PCT` | Auto-halt if breached |
| Min edge threshold | 8% | `MIN_EDGE_THRESHOLD` | Minimum required edge |
| Min market volume | $500 USDC | `MIN_MARKET_VOLUME` | Skip illiquid markets |
| Min days to resolve | 0.1 (2.4h) | `MIN_DAYS_TO_RESOLVE` | Skip near-expiry markets |
| Correlation limit | Same event | `CORRELATION_CHECK` | No double-dip same weather event |
| Post-cancel wait | 5s | `POST_CANCEL_WAIT_SECONDS` | Before re-evaluating market after cancel |

**Kelly Criterion:** `f* = (b*p - q) / b` where `b` is binary market odds, `p` = win probability, `q` = 1-p. Use `KELLY_FRACTION = 0.25` (fractional Kelly). Final position size = `min(bankroll * f* * 0.25, MAX_POSITION_USDC)`.

### Risk Manager Data Read Path

```python
# trading/risk.py
def approve(signal: Signal, mode: str) -> ApprovedSignal | None:
    table = "positions" if mode == "live" else "paper_positions"

    # Read from DB — single source of truth, no in-memory state
    open_count = db.query(f"SELECT COUNT(*) FROM {table} WHERE status='open'")
    daily_loss  = portfolio.get_daily_loss_pct(mode)

    if open_count >= MAX_OPEN_POSITIONS:    return None  # reject
    if daily_loss >= DAILY_LOSS_LIMIT_PCT:  return None  # reject + halt
    if has_correlation(signal, table):      return None  # reject

    # Always apply dollar cap as the final step
    final_size = min(signal.raw_kelly_size * get_bankroll(mode), MAX_POSITION_USDC)
    return ApprovedSignal(signal=signal, final_size=final_size, mode=mode)
```

---

## 🔄 Main Orchestrator — Event Loop

### APScheduler Job Schedule

```python
# main.py — startup sequence
def startup():
    validate_env_vars()           # Raise ValueError if any required key missing
    db.init()                     # Run schema.sql, enable WAL mode
    health_check_ollama()         # GET /api/tags, verify llama3.1:8b loaded
    health_check_clob()           # Verify CLOB API connectivity + wallet balance
    health_check_telegram()       # Send startup ping to Telegram chat
    logger.info("All systems nominal. Starting scheduler.")

# APScheduler jobs (AsyncIOScheduler)
scheduler.add_job(job_market_scan,         "interval", minutes=SCAN_INTERVAL_MINUTES,      max_instances=1)
scheduler.add_job(job_llm_parse_pending,   "interval", minutes=LLM_PARSE_INTERVAL_MINUTES, max_instances=1)
scheduler.add_job(job_ecmwf_ingest,        "interval", hours=6,                            max_instances=1)
scheduler.add_job(job_calibration_batch,   "cron",     hour=2, minute=0,                   max_instances=1)
scheduler.add_job(job_portfolio_snapshot,  "interval", minutes=5,                          max_instances=1)
scheduler.add_job(job_stale_order_cleanup, "interval", minutes=STALE_ORDER_CHECK_MINUTES,  max_instances=1)
```

### Main Scan Job Logic

```python
async def job_market_scan():
    # 1. Check kill switch FIRST — abort immediately if halted
    if db.get("system_config", "bot_halted") == "true":
        logger.info("Kill switch active — scan skipped")
        return

    try:
        markets = scanner.get_active_markets()           # Reads parse_status="success" from DB
        for market in markets:
            model_result = weather.compute(market)
            if model_result is None: continue

            signal = signal_engine.compute_signal(market, model_result)
            if signal is None: continue

            approved = risk_manager.approve(signal, mode=TRADING_MODE)
            if approved is None: continue

            trader.place_order(approved)
            notifier.emit(NotificationEvent.TRADE_EXECUTED, approved)

    except Exception as e:
        logger.error(f"Scan job failed: {e}", exc_info=True)
        notifier.emit(NotificationEvent.COMPONENT_DEGRADED, {"component": "scanner", "error": str(e)})
```

### Kill Switch Atomicity

```python
# dashboard writes to DB:
db.set("system_config", "bot_halted", "true")

# main.py checks at top of EVERY scan job:
if db.get("system_config", "bot_halted") == "true":
    trader.cancel_all_open_orders()   # Cancels all in-flight orders via CLOB API
    scheduler.shutdown()
    notifier.emit(NotificationEvent.RISK_WARNING, {"msg": "Kill switch activated"})
    return
```

---

## 🗄️ Database Schema

All data persisted in `db/trades.db` (SQLite, WAL mode). Dashboard is read-only; `main.py` is the sole writer to core tables.

```sql
-- db/schema.sql

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT);

CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    yes_price REAL,
    end_date TEXT,
    volume REAL,
    parsed TEXT,             -- JSON string
    parse_status TEXT DEFAULT 'pending',  -- pending|success|regex_fallback|failed
    resolution_risk TEXT,    -- LOW|MEDIUM|HIGH|NULL
    last_seen TEXT,          -- ISO timestamp
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    direction TEXT,
    adjusted_edge REAL,
    model_prob REAL,
    market_price REAL,
    raw_kelly_size REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    direction TEXT,
    final_size REAL,
    price REAL,
    order_id TEXT,
    status TEXT,             -- open|filled|cancelled|failed
    mode TEXT DEFAULT 'live',
    rationale TEXT,          -- LLM commentary
    created_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    direction TEXT,
    final_size REAL,
    simulated_fill_price REAL,
    status TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT UNIQUE,
    direction TEXT,
    size REAL,
    entry_price REAL,
    current_price REAL,
    unrealized_pnl REAL,
    status TEXT DEFAULT 'open',
    opened_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT UNIQUE,
    direction TEXT,
    size REAL,
    entry_price REAL,
    current_price REAL,
    unrealized_pnl REAL,
    status TEXT DEFAULT 'open',
    opened_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT,
    total_equity REAL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    daily_pnl REAL,
    daily_loss_pct REAL,
    open_positions INTEGER,
    snapshot_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS calibration_weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    region TEXT,
    season TEXT,
    brier_score REAL,
    weight REAL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ecmwf_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lat REAL,
    lon REAL,
    metric TEXT,
    forecast_date TEXT,
    value REAL,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_cache (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    question_hash TEXT PRIMARY KEY,
    question TEXT,
    parsed TEXT,
    parse_status TEXT,
    model TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
-- Default system config rows:
INSERT OR IGNORE INTO system_config VALUES ('bot_halted', 'false', datetime('now'));
INSERT OR IGNORE INTO system_config VALUES ('trading_mode', 'paper', datetime('now'));

CREATE INDEX IF NOT EXISTS idx_markets_parse_status ON markets(parse_status);
CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_api_cache_source ON api_cache(source);
```

---

## ⚙️ Config — All Parameters

`config/settings.py` loads from `.env` via `python-dotenv`. All parameters typed, validated at import.

```python
# config/settings.py
import os
from dotenv import load_dotenv

load_dotenv()

# --- Trading ---
TRADING_MODE              = os.getenv("TRADING_MODE", "paper")   # "paper" | "live"
MIN_EDGE_THRESHOLD        = float(os.getenv("MIN_EDGE_THRESHOLD", "0.08"))
KELLY_FRACTION            = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_POSITION_USDC         = float(os.getenv("MAX_POSITION_USDC", "50.0"))
MAX_OPEN_POSITIONS        = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
DAILY_LOSS_LIMIT_PCT      = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))
MIN_MARKET_VOLUME         = float(os.getenv("MIN_MARKET_VOLUME", "500.0"))
MIN_DAYS_TO_RESOLVE       = float(os.getenv("MIN_DAYS_TO_RESOLVE", "0.1"))
POST_CANCEL_WAIT_SECONDS  = int(os.getenv("POST_CANCEL_WAIT_SECONDS", "5"))
STALE_ORDER_MAX_AGE_MIN   = int(os.getenv("STALE_ORDER_MAX_AGE_MIN", "30"))

# --- Scheduler ---
SCAN_INTERVAL_MINUTES     = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
LLM_PARSE_INTERVAL_MINUTES= int(os.getenv("LLM_PARSE_INTERVAL_MINUTES", "30"))
STALE_ORDER_CHECK_MINUTES = int(os.getenv("STALE_ORDER_CHECK_MINUTES", "10"))

# --- Rate Limits (calls per hour per source) ---
RATE_LIMIT_OPEN_METEO     = int(os.getenv("RATE_LIMIT_OPEN_METEO", "200"))
RATE_LIMIT_NOAA           = int(os.getenv("RATE_LIMIT_NOAA", "100"))
RATE_LIMIT_GAMMA          = int(os.getenv("RATE_LIMIT_GAMMA", "60"))
RATE_LIMIT_CLOB           = int(os.getenv("RATE_LIMIT_CLOB", "120"))

# --- LLM ---
OLLAMA_HOST               = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL              = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
ANTHROPIC_API_KEY         = os.getenv("ANTHROPIC_API_KEY", "")   # Optional

# --- Polymarket ---
CLOB_HOST                 = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID                  = int(os.getenv("CHAIN_ID", "137"))

# --- Secrets (required for live mode) ---
PRIVATE_KEY               = os.getenv("PRIVATE_KEY", "")
POLY_API_KEY              = os.getenv("POLY_API_KEY", "")
POLY_SECRET               = os.getenv("POLY_SECRET", "")
POLY_PASSPHRASE           = os.getenv("POLY_PASSPHRASE", "")

# --- Notifications ---
TELEGRAM_BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID          = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Validation ---
def validate():
    if TRADING_MODE == "live":
        required = {"PRIVATE_KEY": PRIVATE_KEY, "POLY_API_KEY": POLY_API_KEY,
                    "POLY_SECRET": POLY_SECRET, "POLY_PASSPHRASE": POLY_PASSPHRASE}
        for name, val in required.items():
            if not val:
                raise ValueError(f"Required env var {name} is missing for live trading mode")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        import logging; logging.warning("Telegram not configured — alerts disabled")
```

---

## 🔐 Authentication & Keys

### Polymarket CLOB API (Two-Level Auth)

**L1 (Private Key)** — Your Polygon wallet private key. Used to:
- Derive L2 API credentials
- Sign individual orders (non-custodial — key never leaves your server)

**L2 (API Key)** — Derived from L1 via SDK. Used to:
- Place/cancel orders
- Check balances
- Manage positions

```python
# Initialized ONCE in main.py startup — never stored beyond this call
from py_clob_client.client import ClobClient

clob_client = ClobClient(
    host=CLOB_HOST,
    chain_id=CHAIN_ID,
    key=PRIVATE_KEY    # consumed here only
)
creds = clob_client.create_or_derive_api_creds()
# PRIVATE_KEY env var is no longer accessed after this point
```

### Security Policy — Private Key

1. `PRIVATE_KEY` is read from env var **once** at startup inside `main.py`
2. It is passed directly to `ClobClient()` — **never stored in any module-level or instance variable**
3. All logging formatters apply a filter that redacts any string matching `r'0x[0-9a-fA-F]{60,}'`
4. `.env` file permissions: `chmod 600 .env` (warn on startup if file is world-readable)
5. In production, prefer a secrets manager (HashiCorp Vault, AWS Secrets Manager) over a plain `.env` file
6. **Never commit `.env`** — `.gitignore` must include `.env`, `*.db`, `.env.local`

### Required Environment Variables

```env
# .env (never commit — see .env.example for template)

# Required for live trading
PRIVATE_KEY=0x...                    # Polygon wallet private key
POLY_API_KEY=...                     # Derived L2 key
POLY_SECRET=...                      # Derived L2 secret
POLY_PASSPHRASE=...                  # Derived L2 passphrase

# Required for notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Optional — fallback to Ollama if absent
ANTHROPIC_API_KEY=...

# Optional overrides (defaults in settings.py)
TRADING_MODE=paper
OLLAMA_HOST=http://localhost:11434
MIN_EDGE_THRESHOLD=0.08
MAX_POSITION_USDC=50.0
```

---

## 🔁 Retry, Timeout & Error Handling Policy

Applies to **all** external HTTP calls (weather APIs, Gamma API, CLOB API, Telegram, Ollama).

```python
# Implemented via tenacity in all data/sources/*.py and trading/trader.py
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
)
def fetch_with_retry(url, **kwargs):
    return requests.get(url, timeout=10, **kwargs)
```

**On exhausted retries:**
| Component | Behavior |
|-----------|---------|
| Weather API | Mark source as `DEGRADED`, use last cached response, continue with remaining sources |
| CLOB API (place order) | Abort trade, log error, emit `COMPONENT_DEGRADED` Telegram alert |
| CLOB API (cancel order) | Retry once more with 15s timeout; log if still fails |
| Gamma API | Abort scan job, log, emit `COMPONENT_DEGRADED` alert |
| Ollama | Fall back to regex parser; mark LLM as `DEGRADED` in health panel |
| Telegram | Log locally, continue (non-critical) |

---

## 📊 Calibration Engine

**File:** `engine/calibration.py`  
**Run cadence:** Daily at 02:00 UTC via APScheduler  
**Method:** Brier Score per source per region per season

```python
# engine/calibration.py
def run_calibration_batch():
    """
    For each resolved market in the trades table:
    1. Fetch actual weather outcome from Meteostat (historical station data)
    2. Compare to each source's predicted probability at trade time
    3. Compute Brier Score: BS = (forecast_prob - actual_outcome)^2
    4. Aggregate by source + region + season
    5. Convert to weights: weight = 1 / (average_brier_score + epsilon)
    6. Normalize weights to sum = 1 across sources
    7. Store in calibration_weights table
    """
    ...

def get_weights(region: str, season: str) -> dict[str, float]:
    """Returns {source_name: weight} for ensemble.py to use."""
    weights = db.query("SELECT source, weight FROM calibration_weights WHERE region=? AND season=?", ...)
    return weights if weights else {"open_meteo": 1.0}  # uniform default if no calibration data
```

---

## 🛡️ Rate Limiter

```python
# data/rate_limiter.py
class RateLimiter:
    """Thread-safe per-source hourly call budget tracker."""
    _counts: dict[str, list[float]] = {}   # source → list of timestamps

    def check_and_record(self, source: str) -> bool:
        """Returns True if call is allowed, False if budget exhausted."""
        now = time.time()
        window = [t for t in self._counts.get(source, []) if now - t < 3600]
        limit = settings.get_rate_limit(source)
        if len(window) >= limit:
            logger.warning(f"Rate limit hit for {source}: {len(window)}/{limit} calls/hr")
            return False
        window.append(now)
        self._counts[source] = window
        return True
```

---

## 📲 Notification System

### NotificationEvent Enum

```python
# notifications/events.py
from enum import Enum

class NotificationEvent(Enum):
    TRADE_EXECUTED      = "trade_executed"
    DAILY_PNL_SUMMARY   = "daily_pnl_summary"
    RISK_WARNING        = "risk_warning"
    COMPONENT_DEGRADED  = "component_degraded"
    MODEL_DISAGREEMENT  = "model_disagreement"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    BOT_STARTED         = "bot_started"
```

### Async Non-Blocking Design

```python
# notifications/telegram.py
# All sends go through asyncio.Queue — never blocks the main trading loop
event_queue: asyncio.Queue = asyncio.Queue()

async def emit(event: NotificationEvent, payload: dict):
    await event_queue.put((event, payload))

async def _consumer():
    while True:
        event, payload = await event_queue.get()
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=format_event(event, payload))
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")  # Non-fatal
```

---

## 🖥️ Dashboard Design

**Technology:** Streamlit (Python, runs on server, no frontend build step)  
**Data access:** Read-only from SQLite DB. Auto-refreshes every 30 seconds.  
**Controls:** Write to `system_config` DB table only — never to in-memory shared state.

### Panels

1. **Header Bar** — Portfolio value, daily P&L, open positions count, bot status indicator
2. **Active Markets Table** — All tracked markets with: question, market price, model probability, edge, action taken
3. **Ensemble Chart** — Click any market → shows probability distribution across all ensemble members
4. **LLM Analysis Panel** — Plain English model interpretation + resolution risk rating
5. **Trade Log** — Chronological feed of all bot actions with rationale
6. **Bot Status Panel** — Health of each component (scanner, weather engine, LLM, CLOB connection, wallet balance)

### Controls → DB Mapping

| Control | DB Write |
|--------|---------|
| 🔴 Kill Switch | `system_config.bot_halted = "true"` |
| 📄 Paper/Live Toggle | `system_config.trading_mode = "paper"|"live"` |
| ⚙️ Threshold Slider | `system_config.min_edge_threshold = "<value>"` |
| 💰 Position Size | `system_config.max_position_usdc = "<value>"` |
| 🔒 Per-Market Lock | `market_overrides.market_id = "skip"|"force"` |

> `main.py` reads all config from `system_config` DB table at the start of each job — never from in-memory state after startup. This makes dashboard control writes race-condition-free.

---

## 🧪 Paper Trading Architecture

Paper mode is a full first-class mode, not a flag bolted on.

```
TRADING_MODE = "paper"  (set in .env or system_config table)

Risk Manager  → reads from paper_positions  (not positions)
Trader        → PaperTrader (not ClobTrader)
PaperTrader   → writes to paper_trades + paper_positions tables
Portfolio     → get_snapshot(mode="paper")  reads paper_* tables
Dashboard     → displays paper P&L separately from live P&L
Risk Guards   → open position count, daily loss — all from paper_* tables
```

**PaperTrader simulates fills** at the current YES mid-price. No real CLOB calls are made.

---

## 📁 Repository Structure

```
polymarket-weather-bot/
│
├── README.md
├── ARCHITECTURE.md           # This file
├── requirements.txt          # All deps pinned (see Tech Stack section)
├── .env.example              # Template for secrets — commit this
├── .env                      # Never commit — gitignored
│
├── config/
│   └── settings.py           # All parameters + env loading + validation
│
├── data/
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── open_meteo.py     # Forecast + Ensemble + Historical + Climate APIs
│   │   ├── noaa.py           # NWS official API
│   │   ├── ecmwf.py          # ECMWF batch GRIB ingest (independent job)
│   │   └── meteostat.py      # Historical station data (calibration only)
│   ├── cache/
│   │   └── manager.py        # CacheManager — SQLite-backed TTL cache
│   └── rate_limiter.py       # Per-source hourly call budget
│
├── engine/
│   ├── __init__.py
│   ├── models.py             # ModelResult dataclass
│   ├── weather.py            # Main probability computation
│   ├── ensemble.py           # Multi-model aggregation + Brier weighting
│   └── calibration.py        # Daily Brier-score calibration batch job
│
├── llm/
│   ├── __init__.py
│   ├── parser.py             # Market question → structured JSON (+ regex fallback)
│   ├── analyst.py            # Resolution risk + narration + trade commentary
│   └── ollama_client.py      # Local Ollama interface + health check
│
├── market/
│   ├── __init__.py
│   ├── models.py             # Market + Signal + ApprovedSignal dataclasses
│   ├── scanner.py            # Gamma API — find + filter weather markets
│   └── signal.py             # Edge calculation + corrected Kelly signal
│
├── trading/
│   ├── __init__.py
│   ├── models.py             # ApprovedSignal dataclass
│   ├── risk.py               # Guardrails + position checks + size clamping
│   ├── trader.py             # py-clob-client wrapper (live)
│   ├── paper_trader.py       # Paper trading — same interface, writes to paper_* tables
│   └── portfolio.py          # P&L computation + snapshot persistence
│
├── notifications/
│   ├── events.py             # NotificationEvent enum + payload schemas
│   └── telegram.py           # Async non-blocking Telegram alert consumer
│
├── dashboard/
│   └── app.py                # Streamlit UI — read-only DB, controls via system_config
│
├── db/
│   ├── schema.sql            # Full DB schema (see Database Schema section)
│   ├── init.py               # Runs schema.sql on first launch, handles migrations
│   └── trades.db             # SQLite database (gitignored)
│
├── tests/
│   ├── conftest.py           # Fixtures, mocked DB, mocked HTTP clients
│   ├── test_weather.py       # engine/ unit tests (≥90% coverage)
│   ├── test_signal.py        # market/signal.py unit tests
│   ├── test_risk.py          # trading/risk.py unit tests
│   └── test_integration.py   # Full paper-mode E2E with fixture data
│
└── main.py                   # Orchestrator — APScheduler jobs + startup sequence
```

---

## ⚙️ Tech Stack

| Layer | Technology | Version | Reason |
|-------|-----------|---------|--------|
| Language | Python | 3.11+ | py-clob-client requirement |
| Polymarket SDK | `py-clob-client` | `0.18.0` (pin) | Official, maintained |
| Scheduling | `APScheduler` | `3.10.4` (pin) | Cron-like jobs in-process |
| Weather data | Open-Meteo + NOAA + ECMWF | — | Free, no keys, high quality |
| ECMWF SDK | `ecmwf-opendata` | `0.3.3` (pin) | GRIB download + parse |
| GRIB parsing | `cfgrib` | `0.9.10.4` (pin) | Requires `eccodes` binary |
| Retry logic | `tenacity` | `8.2.3` (pin) | Declarative retry/backoff |
| LLM (routine) | Ollama + Llama 3.1 8B | latest | Free, local, fast |
| LLM (decisions) | `anthropic` | `0.25.0` (pin) | Quality when it matters |
| Dashboard | `streamlit` | `1.32.0` (pin) | Pure Python, no build |
| Database | SQLite (WAL mode) | built-in | Trade log + signal history |
| Notifications | `python-telegram-bot` | `21.1.1` (pin) | Alerts |
| Env loading | `python-dotenv` | `1.0.1` (pin) | Secrets from .env |
| HTTP | `requests` | `2.31.0` (pin) | All external calls |
| Data science | `pandas`, `numpy` | `2.1.0`, `1.26` (pin) | Ensemble math, calibration |
| Blockchain | Polygon (chain_id=137) | — | Polymarket's chain |

> ⚠ **System dependency:** `eccodes` binary must be installed before `cfgrib` works.  
> Ubuntu/Debian: `sudo apt install libeccodes-dev`  
> macOS: `brew install eccodes`  
> Document in README and provide a `scripts/setup.sh`.

---

## ⚠️ Key Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------| 
| Geo-IP restrictions (US blocked on Polymarket) | High | Deploy to EU/Asia VPS (e.g., DigitalOcean Amsterdam or Singapore). Startup connectivity check to CLOB API. |
| Market question parsing errors | High | 3-path fallback (Ollama → regex → skip). All `parse_status="failed"` markets skipped automatically. Paper-trade new question formats before live. |
| **Kelly formula error** | **Critical → Fixed** | Corrected formula in `signal.py`: `f* = (b*p - q) / b` with proper binary market odds. Unit tested. |
| **time_decay negative values** | **High → Fixed** | Clamped: `max(0.1, confidence * (1 - 0.02 * min(days, 45)))`. Unit tested with 60-day market. |
| Model overconfidence | Medium | Fractional Kelly (0.25x), strict edge threshold, Brier-score calibration. |
| Thin liquidity / slippage | Medium | Min volume filter; limit orders only (no market orders). |
| Resolution ambiguity | Medium | LLM resolution risk check; skip HIGH risk markets; +4% edge threshold for MEDIUM. |
| USDC on Polygon required | Medium | Fund wallet before live trading. Balance checked on startup. |
| API rate limits | Low | `RateLimiter` class per source per hour. Cached responses as fallback. |
| Daily loss spiral | Low | Hard daily loss limit with auto-halt + kill switch + Telegram alert. |
| Private key leak | Critical | Single-use in `ClobClient()`, log filter applied, `.env` permission check. |

---

## 💰 Infrastructure Cost

| Item | Monthly Cost |
|------|-------------|
| VPS (EU/Asia region, non-US) | ~$5–10 |
| All weather data APIs | $0 (free tiers) |
| Ollama LLM (local on VPS) | $0 |
| Claude API (spot decisions) | ~$1–5 |
| **Total** | **~$6–15/month** |

Capital required: USDC on Polygon for trading positions.

---

## 🚀 Development Phases

### Phase 1 — Foundation & Data (Week 1)
- [ ] Pin `requirements.txt`, write `setup.sh` for system deps
- [ ] `config/settings.py` with all parameters and validation
- [ ] `db/schema.sql` + `db/init.py` with WAL mode and migrations
- [ ] `ModelResult`, `Market`, `Signal`, `ApprovedSignal` dataclasses
- [ ] `CacheManager` SQLite-backed with per-source TTL
- [ ] `RateLimiter` utility
- [ ] `open_meteo.py` with retry + cache
- [ ] `noaa.py` with retry + cache

### Phase 2 — Weather Engine & LLM (Week 2)
- [ ] `ecmwf.py` batch ingest job (GRIB → DB)
- [ ] `meteostat.py` for calibration data
- [ ] `ensemble.py` multi-model aggregation
- [ ] `calibration.py` Brier score daily batch
- [ ] `weather.py` top-level probability output
- [ ] `ollama_client.py` with health-check
- [ ] `parser.py` with Ollama + regex fallback
- [ ] `analyst.py` with Claude/Ollama fallback

### Phase 3 — Signal & Risk (Week 2–3)
- [ ] `scanner.py` Gamma API + async LLM parse trigger
- [ ] `signal.py` with corrected Kelly and clamped time_decay
- [ ] `risk.py` with all guardrails + size clamping
- [ ] `trader.py` CLOB wrapper (live)
- [ ] `paper_trader.py` paper mode
- [ ] `portfolio.py` P&L snapshots

### Phase 4 — Orchestration & Notifications (Week 3)
- [ ] `notifications/events.py` + `telegram.py` async queue
- [ ] `main.py` full APScheduler setup + startup sequence + kill switch
- [ ] Security: log filter, `.env` permission check

### Phase 5 — Dashboard & Testing (Week 4)
- [ ] `dashboard/app.py` all panels + DB controls
- [ ] Unit tests: engine, signal, risk (≥90% coverage)
- [ ] Integration test: paper mode E2E
- [ ] Run paper mode for 1–2 weeks — calibrate thresholds

### Phase 6 — Live Deployment (Week 5+)
- [ ] Deploy to non-US VPS
- [ ] Fund wallet with initial USDC
- [ ] Switch `TRADING_MODE=live` — monitor for 48h
- [ ] Gradual capital scaling

---

## 📚 References

- [Polymarket CLOB API Docs](https://docs.polymarket.com/api-reference/introduction)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [NOAA NWS API](https://www.weather.gov/documentation/services-web-api)
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
- [ecmwf-opendata SDK](https://github.com/ecmwf/ecmwf-opendata)
- [Polymarket Weather Markets](https://polymarket.com/predictions/weather)
- [Brier Score (calibration)](https://en.wikipedia.org/wiki/Brier_score)
- [Kelly Criterion (binary markets)](https://en.wikipedia.org/wiki/Kelly_criterion)
