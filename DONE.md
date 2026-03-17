# DONE — Polymarket Weather Bot v1.0

All 29 tasks complete. 314 tests passing.

## Milestones

| Milestone | Tasks | Status |
|-----------|-------|--------|
| M1 — Foundation | TASK-001–004, TASK-022 | ✅ DONE |
| M2 — Data Layer | TASK-005–008 | ✅ DONE |
| M3 — Weather Engine | TASK-009–011 | ✅ DONE |
| M4 — LLM Layer | TASK-012–014 | ✅ DONE |
| M5 — Scanner & Signal | TASK-015–016 | ✅ DONE |
| M6 — Risk & Trading | TASK-017–020 | ✅ DONE |
| M7 — Orchestration | TASK-021 | ✅ DONE |
| M8 — Notifications & Dashboard | TASK-023–024 | ✅ DONE |
| M9 — Hardening & Testing | TASK-025–029 | ✅ DONE |

## What was built

- **Ensemble weather engine** — Open-Meteo (50–100 members) + NOAA NWS + ECMWF GRIB2, weighted by Brier-score calibration
- **LLM parsing layer** — Ollama (Llama 3.1 8B) → regex fallback → parse_status="failed", cached by SHA-256
- **Corrected Kelly Criterion** — `b = (1-price)/price` for YES, `0.25x` fractional, clamped time decay
- **Paper/live isolation** — completely separate DB tables, risk manager reads correct table per mode
- **APScheduler event loop** — 6 jobs: scan (15min), LLM parse (30min), ECMWF (6h), calibration (02:00 UTC), portfolio (5min), stale cleanup (10min)
- **Kill switch** — `bot_halted` in `system_config`, checked at the top of every scan cycle
- **Streamlit dashboard** — 6 panels, all controls write to `system_config` DB, never mutate in-memory state
- **Async Telegram alerts** — `asyncio.Queue` consumer, `NotificationEvent` enum, CHAT_ID whitelist
- **Private key redaction** — `RedactingFormatter` scrubs `0x[0-9a-fA-F]{60,}` from all log output
- **314 unit + integration tests** — 98% coverage on engine/, full paper E2E pipeline test

## Test results

```
314 passed in 3.99s
```

## How to run

```bash
bash scripts/setup.sh      # install eccodes system dep
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
python main.py             # paper mode by default
streamlit run dashboard/app.py
```
