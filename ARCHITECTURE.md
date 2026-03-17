# Polymarket Weather Bot — System Architecture

> **Status:** Planning / Pre-development  
> **Last Updated:** 2026-03-17  
> **Author:** Jarvis (AI Architecture Design)

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

if abs(edge) > MIN_EDGE_THRESHOLD (default: 0.08):
    → generate trade signal
    → size via Kelly Criterion
    → execute via CLOB API
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
│  Historical API                                                  │
│  Climate API                                                     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                      WEATHER ENGINE                              │
│  • Fetches forecasts from all sources                            │
│  • Runs ensemble probability distribution                        │
│  • Calibrates against historical accuracy                        │
│  • Output: P(event) + confidence interval                        │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                       LLM LAYER                                  │
│  Ollama (local, Llama 3.1 / Mistral):                            │
│  • Parse market question → structured JSON                       │
│  • Flag resolution ambiguity                                     │
│  • Narrate ensemble output for dashboard                         │
│  Claude API (spot calls, high-stakes):                           │
│  • Complex ambiguity decisions                                   │
│  • Trade rationale generation                                    │
└──────────┬───────────────────────────────┬───────────────────────┘
           │                               │
┌──────────▼──────────┐        ┌───────────▼─────────────┐
│   MARKET SCANNER    │        │     SIGNAL ENGINE        │
│                     │        │                          │
│ Gamma API (public)  │        │ edge = model_prob - mkt  │
│ Find active weather │        │ Kelly Criterion sizing   │
│ markets             │        │ Time-decay adjustment    │
│ Filter: liquidity,  │        │ Liquidity adjustment     │
│ expiry, volume      │        │ Confidence weighting     │
└──────────┬──────────┘        └───────────┬──────────────┘
           └──────────────┬────────────────┘
                          │
               ┌──────────▼──────────┐
               │    RISK MANAGER     │
               │                     │
               │ • Max position/mkt  │
               │ • Daily loss limit  │
               │ • Max open trades   │
               │ • Correlation check │
               │ • Kelly guardrails  │
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
               └──────────┬──────────┘
                          │
┌─────────────────────────▼────────────────────────────────────────┐
│                  STREAMLIT DASHBOARD (24/7)                       │
│                                                                  │
│  Portfolio Overview | Active Markets | Ensemble Charts           │
│  Trade Log | Bot Status | P&L History                           │
│  Kill Switch | Paper Mode | Manual Overrides | Threshold Tuning  │
└─────────────────────────┬────────────────────────────────────────┘
                          │
               ┌──────────▼──────────┐
               │  TELEGRAM ALERTS    │
               │                     │
               │ Trade executed      │
               │ Daily P&L summary   │
               │ Risk warnings       │
               │ Model disagreements │
               └─────────────────────┘
```

---

## 📡 Data Sources

### Tier 1 — Primary Forecast Models (all free, no API key required)

| Source | URL | What it provides | Notes |
|--------|-----|-----------------|-------|
| **Open-Meteo Forecast API** | `open-meteo.com/en/docs` | Hourly forecast, 16 days, global | Primary source |
| **Open-Meteo Ensemble API** | `open-meteo.com/en/docs/ensemble-api` | 50–100 perturbed model members (ECMWF ENS, GFS ENS, ICON ENS) | Core probability engine |
| **Open-Meteo Historical API** | `open-meteo.com/en/docs/historical-weather-api` | Hourly history since 1940 | Model calibration |
| **Open-Meteo Climate API** | `open-meteo.com/en/docs/climate-api` | Long-run climate normals | Context / anomaly detection |
| **NOAA NWS API** | `api.weather.gov` | US official forecasts, alerts | US market authority |
| **ECMWF Open Data** | `ecmwf.int/en/forecasts/datasets/open-data` | Real-time IFS + AIFS (AI model) forecasts | Best global model |

### Tier 2 — Supplementary & Validation (free tiers)

| Source | Free Limit | What it provides |
|--------|-----------|-----------------|
| **Meteostat** (Python lib) | Unlimited | Historical station-level observations |
| **OpenWeatherMap** | 1M calls/month | Current conditions, corroboration |
| **WeatherAPI** | 1M calls/month | Current + 3-day, global |
| **Visual Crossing** | 1000 calls/day | Strong historical data |
| **MET Norway (Yr.no)** | Unlimited | 48h global forecast, no key |
| **Stormglass.io** | 10 calls/day | Marine weather |

### Tier 3 — Contextual / Specialty

| Source | What it provides |
|--------|-----------------|
| **NOAA Storm Reports** | Severe weather event history |
| **FAA METARs / PIREPs** | Real-time airport observations (US) |
| **USGS Streamflow** | Flood-risk context |

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

We combine **multiple ensemble systems** (ECMWF + GFS + ICON) and weight by historical accuracy per region/season.

---

## 🧠 LLM Layer Design

### Purpose
The LLM handles tasks that are brittle to hardcode with regex/rules — especially natural language understanding of market questions.

### Task 1: Market Question Parser
**Input:** Raw Polymarket question string  
**Output:** Structured JSON for the weather engine

```
Input:  "Will the temperature in Chicago exceed 85°F at any point between June 10-15, 2026?"

Output: {
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
  "resolution_source": "unknown"
}
```

### Task 2: Resolution Risk Analysis
Reads market resolution criteria and flags ambiguity.
```
Output: {
  "risk_level": "MEDIUM",
  "reason": "Market says 'official NWS reading' but doesn't specify station",
  "recommendation": "Widen edge threshold before trading"
}
```

### Task 3: Ensemble Narration (Dashboard)
Converts raw model data into plain-English summaries for the dashboard and Telegram alerts.

### Task 4: Trade Decision Commentary
For each executed trade, generates a plain-English rationale logged to the DB.

### LLM Routing Strategy

| Task | Model | Why |
|------|-------|-----|
| Market question parsing | Ollama (Llama 3.1 8B, local) | High volume, routine, free |
| Ensemble narration | Ollama (local) | High frequency, low stakes |
| Resolution risk analysis | Claude Sonnet API | Nuanced, low volume |
| Trade rationale | Claude Sonnet API | Logged permanently, quality matters |

---

## 📊 Signal Engine Logic

```python
# Pseudocode
def compute_signal(market, model_result):
    market_price = market.yes_price          # 0.0 – 1.0
    model_prob   = model_result.probability  # 0.0 – 1.0
    confidence   = model_result.confidence   # 0.0 – 1.0
    
    raw_edge = model_prob - market_price
    
    # Time decay: lower confidence further from resolution
    days_to_resolve = (market.end_date - today).days
    time_decay = confidence * (1 - 0.02 * days_to_resolve)  # tunable
    
    # Liquidity penalty: thin books need bigger edge
    liquidity_penalty = 0.02 if market.volume < 1000 else 0
    
    adjusted_edge = raw_edge * time_decay - liquidity_penalty
    
    if abs(adjusted_edge) >= MIN_EDGE_THRESHOLD:  # default 0.08
        direction = "YES" if adjusted_edge > 0 else "NO"
        size = kelly_criterion(adjusted_edge, market_price)
        return Signal(direction, size, adjusted_edge)
    
    return None  # no trade
```

---

## ⚖️ Risk Manager Rules

| Rule | Default | Description |
|------|---------|-------------|
| Max position per market | $50 USDC | Single market exposure cap |
| Max open positions | 5 | Concurrent position limit |
| Daily loss limit | 10% of portfolio | Auto-halt if breached |
| Min edge threshold | 8% | Minimum required edge |
| Min market volume | $500 | Skip illiquid markets |
| Min days to resolve | 0.1 (2.4h) | Skip near-expiry markets |
| Correlation limit | — | No double exposure to same weather event |

**Kelly Criterion:** `f* = (bp - q) / b` where `b` = odds, `p` = win probability, `q` = 1-p. Use fractional Kelly (0.25x) to reduce variance.

---

## 🖥️ Dashboard Design

**Technology:** Streamlit (Python, runs on server, no frontend build step)

### Panels

1. **Header Bar** — Portfolio value, daily P&L, open positions count, bot status indicator
2. **Active Markets Table** — All tracked markets with: question, market price, model probability, edge, action taken
3. **Ensemble Chart** — Click any market → shows probability distribution across all ensemble members
4. **LLM Analysis Panel** — Plain English model interpretation + resolution risk rating
5. **Trade Log** — Chronological feed of all bot actions with rationale
6. **Bot Status Panel** — Health of each component (scanner, weather engine, LLM, CLOB connection, wallet balance)

### Controls
- 🔴 **Emergency Kill Switch** — halt all trading immediately
- 📄 **Paper Trading Mode** — simulate without real money (for testing)
- ⚙️ **Threshold Slider** — adjust min edge in real-time
- 💰 **Position Size Control** — adjust max per-trade USDC
- 🔒 **Per-Market Lock** — manually override bot decision on any market

---

## 📁 Repository Structure

```
polymarket-weather-bot/
│
├── README.md
├── ARCHITECTURE.md           # This file
├── requirements.txt
├── .env.example              # Template for secrets
│
├── config/
│   └── settings.py           # All tunable parameters
│
├── data/
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── open_meteo.py     # Forecast + Ensemble + Historical
│   │   ├── noaa.py           # NWS official API
│   │   ├── ecmwf.py          # ECMWF Open Data
│   │   └── meteostat.py      # Historical station data
│   └── cache/                # SQLite cache for API responses
│
├── engine/
│   ├── __init__.py
│   ├── weather.py            # Main probability computation
│   ├── ensemble.py           # Multi-model aggregation + weighting
│   └── calibration.py        # Historical accuracy tuning per region
│
├── llm/
│   ├── __init__.py
│   ├── parser.py             # Market question → structured JSON
│   ├── analyst.py            # Resolution risk + narration
│   └── ollama_client.py      # Local Ollama interface
│
├── market/
│   ├── __init__.py
│   ├── scanner.py            # Gamma API — find weather markets
│   └── signal.py             # Edge calculation + signal generation
│
├── trading/
│   ├── __init__.py
│   ├── risk.py               # Kelly sizing + guardrails
│   ├── trader.py             # py-clob-client wrapper
│   └── portfolio.py          # P&L tracking
│
├── dashboard/
│   └── app.py                # Streamlit UI (run with: streamlit run dashboard/app.py)
│
├── notifications/
│   └── telegram.py           # Telegram alert integration
│
├── db/
│   ├── schema.sql            # DB schema
│   └── trades.db             # SQLite (gitignored)
│
├── tests/
│   ├── test_weather.py
│   ├── test_signal.py
│   └── test_risk.py
│
└── main.py                   # Orchestrator — main event loop
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
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,            # Polygon mainnet
    key=os.getenv("PRIVATE_KEY")
)
creds = client.create_or_derive_api_creds()
```

### Required Environment Variables

```env
# .env (never commit this)
PRIVATE_KEY=0x...                    # Polygon wallet private key
POLY_API_KEY=...                     # Derived L2 key
POLY_SECRET=...                      # Derived L2 secret
POLY_PASSPHRASE=...                  # Derived L2 passphrase
TELEGRAM_BOT_TOKEN=...               # For alerts
TELEGRAM_CHAT_ID=...                 # Your Telegram ID
ANTHROPIC_API_KEY=...                # Claude API (optional, for high-stakes calls)
OLLAMA_HOST=http://localhost:11434   # Local LLM
```

---

## ⚙️ Tech Stack

| Layer | Technology | Reason |
|-------|-----------|--------|
| Language | Python 3.11+ | Official Polymarket SDK is Python |
| Polymarket SDK | `py-clob-client` | Official, maintained |
| Scheduling | APScheduler | Cron-like jobs within Python process |
| Weather data | Open-Meteo + NOAA + ECMWF | Free, no keys, high quality |
| LLM (routine) | Ollama + Llama 3.1 8B | Free, local, fast |
| LLM (decisions) | Claude Sonnet API | Quality when it matters |
| Dashboard | Streamlit | Pure Python, no frontend build |
| Database | SQLite → Postgres (scale) | Trade log + signal history |
| Blockchain | Polygon (chain_id=137) | Polymarket's chain |
| Notifications | Telegram Bot API | Already configured |

---

## ⚠️ Key Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Geo-IP restrictions (US blocked on Polymarket) | High | Use non-US VPS or VPN |
| Market question parsing errors | High | LLM + human review for new question formats; paper trade new types first |
| Model overconfidence | Medium | Fractional Kelly (0.25x), strict edge threshold |
| Thin liquidity / slippage | Medium | Min volume filter; limit orders only (no market orders) |
| Resolution ambiguity | Medium | LLM resolution risk check; skip HIGH risk markets |
| USDC on Polygon required | Medium | Fund wallet before live trading |
| API rate limits | Low | Caching layer; staggered polling intervals |
| Daily loss spiral | Low | Hard daily loss limit with auto-halt |

---

## 💰 Infrastructure Cost

| Item | Monthly Cost |
|------|-------------|
| VPS (existing server) | $0 additional |
| All weather data APIs | $0 (free tiers) |
| Ollama LLM (local) | $0 |
| Claude API (spot decisions) | ~$1–5 |
| **Total** | **~$1–5/month** |

Capital required: USDC on Polygon for trading positions.

---

## 🚀 Development Phases

### Phase 1 — Data & Parsing (Week 1–2)
- [ ] Set up all weather data source connectors
- [ ] Implement Open-Meteo Ensemble probability calculator
- [ ] Build LLM market question parser (Ollama)
- [ ] Unit tests for weather engine

### Phase 2 — Signal & Risk (Week 2–3)
- [ ] Market scanner (Gamma API)
- [ ] Signal engine with edge calculation
- [ ] Risk manager with Kelly sizing
- [ ] Paper trading mode

### Phase 3 — Trading Engine (Week 3–4)
- [ ] Polymarket CLOB integration (py-clob-client)
- [ ] Order placement, cancellation, monitoring
- [ ] Portfolio P&L tracking

### Phase 4 — Dashboard (Week 4)
- [ ] Streamlit dashboard with all panels
- [ ] Kill switch + manual overrides
- [ ] Telegram alert integration

### Phase 5 — Live Testing (Week 5+)
- [ ] Run in paper mode for 1–2 weeks
- [ ] Calibrate edge thresholds against real markets
- [ ] Gradual capital deployment

---

## 📚 References

- [Polymarket CLOB API Docs](https://docs.polymarket.com/api-reference/introduction)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [NOAA NWS API](https://www.weather.gov/documentation/services-web-api)
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
- [Polymarket Weather Markets](https://polymarket.com/predictions/weather)
