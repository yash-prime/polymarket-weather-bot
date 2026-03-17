# 🌦️ Polymarket Weather Bot

An automated trading bot for [Polymarket](https://polymarket.com/predictions/weather) weather prediction markets. Uses real meteorological ensemble models to compute accurate probabilities and trades when market odds diverge.

## Quick Links

- 📐 [Full Architecture & Design Doc](./ARCHITECTURE.md)
- 🔗 [Polymarket CLOB API Docs](https://docs.polymarket.com)
- 🌍 [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)

## Status

> **v1.0** — All 29 tasks complete. Paper mode ready to run.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the complete system design, data sources, LLM layer, dashboard spec, and development roadmap.

---

## Setup

### 1. System Dependencies (eccodes — required for ECMWF GRIB2 parsing)

**Ubuntu / Debian:**
```bash
sudo apt-get update && sudo apt-get install -y libeccodes-dev eccodes
```

**macOS (Homebrew):**
```bash
brew install eccodes
```

**Automated (all platforms):**
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
# Edit .env — fill in secrets for live mode, or leave defaults for paper mode
chmod 600 .env
```

### 4. Run

**Bot (paper mode by default):**
```bash
python main.py
```

**Dashboard:**
```bash
streamlit run dashboard/app.py
```
