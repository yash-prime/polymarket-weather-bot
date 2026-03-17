# Polymarket Weather Bot

An automated trading bot for [Polymarket](https://polymarket.com/predictions/weather) weather prediction markets. Uses real meteorological ensemble models to compute accurate probabilities and trades when market odds diverge from model output.

## Quick Links

- [Full Architecture & Design Doc](./ARCHITECTURE.md)
- [Polymarket CLOB API Docs](https://docs.polymarket.com)
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)

## Status

> **v1.1** — Live paper trading. LLM-driven portfolio management with rule-based conflict resolution.

---

## How It Works

```
market_price  = Polymarket YES price (0.00–1.00)
model_prob    = weighted ensemble of Open-Meteo + NOAA + ECMWF
edge          = model_prob - market_price

if abs(edge) > 0.08 (8%):
    → Kelly-sized position (0.25x fractional, capped at $50)
    → LLM portfolio review: open / close / hold
    → Rule-based conflict cleanup (guaranteed, no LLM dependency)
    → Execute via paper_trader (or live CLOB API)
```

**Key insight:** Weather markets are mutually exclusive bins — "Seattle high temp 50–51°F", "52–53°F", "54–55°F" etc. Only one can win. The bot tracks event groups and ensures max one position per city+date+metric group.

---

## Architecture

```
DATA SOURCES
  Open-Meteo (50–100 member ensemble, global)
  NOAA NWS   (US coordinates only)
  ECMWF      (6h batch ingest → SQLite snapshot)
        │
        ▼
WEATHER ENGINE
  Weighted ensemble aggregation (Brier-score calibrated)
  Supports: temperature_2m_max/min, precipitation_sum, wind_speed_10m_max
  Range thresholds: "between X and Y" → threshold + threshold_high
        │
        ▼
SIGNAL ENGINE
  edge = model_prob − market_price
  Kelly sizing with time-decay and liquidity adjustment
  Min edge: 8%  |  Max position: $50  |  Max open: 50
        │
        ▼
LLM PORTFOLIO MANAGER  (every 5-min scan)
  OpenRouter (primary) → Ollama (fallback)
  Decides: open new signals / close contradictory positions / hold
  Rule-based fallback: always runs if LLM unavailable —
    closes extras in conflict groups, keeps best P&L position
        │
        ▼
RISK MANAGER (safety net, runs before every open)
  Kill switch · Max positions · Daily loss limit (10%)
  Duplicate market check · Event group limit (max 2 per group)
  Capital limit ($2,500 deployed)
        │
        ▼
PAPER TRADER / LIVE TRADER
  Paper: simulated fills, isolated DB tables, full P&L tracking
  Live:  Polymarket CLOB API (limit orders only)
        │
        ▼
DASHBOARD  (FastAPI + vanilla JS, port 8000)
  Real-time portfolio · Open positions · Trade log (open + closed)
  Intelligence reports · Live bot log · Kill switch
```

---

## Setup

### 1. System Dependencies

**Ubuntu / Debian:**
```bash
sudo apt-get update && sudo apt-get install -y libeccodes-dev eccodes
```

**macOS:**
```bash
brew install eccodes
```

**Automated:**
```bash
bash scripts/setup.sh
```

### 2. Python Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configuration

```bash
cp .env.example .env
# Edit .env — minimum required for paper mode:
#   OPENROUTER_API_KEY=...   (free tier works — openrouter.ai)
#   POLYMARKET_API_KEY=...   (only needed for live mode)
chmod 600 .env
```

Key settings (all have defaults, paper mode works out of the box):

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `MIN_EDGE_THRESHOLD` | `0.08` | Minimum edge to open a position (8%) |
| `KELLY_FRACTION` | `0.25` | Fractional Kelly sizing |
| `MAX_POSITION_USDC` | `50.0` | Max size per position |
| `MAX_OPEN_POSITIONS` | `50` | Max concurrent open positions |
| `DAILY_LOSS_LIMIT_PCT` | `0.10` | Kill switch triggers at 10% daily loss |
| `OPENROUTER_MODEL` | `stepfun/step-3.5-flash:free` | LLM for portfolio manager |

### 4. Run

**Bot** (scans every 5 minutes):
```bash
python main.py
```

**Dashboard** (port 8000):
```bash
uvicorn dashboard.api:app --host 0.0.0.0 --port 8000
```

---

## Dashboard

The web dashboard at `http://localhost:8000` shows:

- **Portfolio** — total equity, deployed capital, realized/unrealized P&L
- **Open Positions** — live positions with current price and P&L
- **Markets** — all active markets with parse status and model scores
- **Trade Log** — all trades (open + closed) with status chips, realized P&L, and close reason
- **Intelligence Reports** — AI-generated portfolio analysis on demand
- **Bot Log** — live log stream with last 200 lines

---

## Risk Controls

- **Kill switch** — set `bot_halted=true` in DB or click HALT in dashboard
- **Daily loss limit** — auto-halts at 10% drawdown (configurable)
- **Event group limit** — max 2 positions per city+date+metric group
- **Capital limit** — stops opening new positions when $2,500 is deployed
- **Rule-based conflict cleanup** — runs every scan; closes extra positions in mutually exclusive groups regardless of LLM availability

---

## Project Structure

```
main.py                  Scan loop (APScheduler, 5-min interval)
config/settings.py       All configuration via env vars
engine/
  weather.py             Top-level compute entry point + caching
  ensemble.py            Weighted probability aggregation
  models.py              ModelResult dataclass
market/
  scanner.py             Gamma API market fetcher
  signal.py              Edge + Kelly sizing
  models.py              Market / Signal dataclasses
trading/
  llm_manager.py         LLM portfolio manager + rule-based fallback
  portfolio_analyzer.py  Group positions by event, build context
  risk.py                All risk guardrails
  paper_trader.py        Paper trading engine
  trader.py              Live CLOB trading engine
llm/
  parser.py              LLM market question parser (+ regex fallback)
  openrouter_client.py   OpenRouter API client
  ollama_client.py       Ollama local LLM client
data/sources/
  open_meteo.py          Open-Meteo ensemble API
  noaa.py                NOAA NWS forecast API
  ecmwf.py               ECMWF snapshot DB reader
dashboard/
  api.py                 FastAPI backend
  templates/index.html   Single-page dashboard UI
db/
  init.py                SQLite schema + connection
```
