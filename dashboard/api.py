"""
dashboard/api.py — FastAPI dashboard backend.

Endpoints:
  GET  /                → serve HTML dashboard
  GET  /api/status      → bot status, kill switch, trading mode
  GET  /api/markets     → active markets with weather + LLM data
  GET  /api/trades      → recent paper/live trades
  GET  /api/portfolio   → portfolio snapshot + open positions
  GET  /api/weights     → ensemble calibration weights
  GET  /api/logs        → last 50 lines of bot.log
  POST /api/control     → update kill switch / mode / thresholds
"""
import json
import os
import sys
from pathlib import Path

# Add project root to path so imports work
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Polymarket Weather Bot Dashboard")

# ── Path to bot log ──────────────────────────────────────────────────────────
_BOT_LOG = Path.home() / "bot.log"
_DB_PATH = _ROOT / "db" / "trades.db"


def _db():
    from db.init import get_connection
    return get_connection(str(_DB_PATH))


# ── HTML ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text())


# ── API: Status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'bot_halted'"
            ).fetchone()
            halted = row and row["value"] == "1"

            mode_row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'trading_mode'"
            ).fetchone()
            mode = mode_row["value"] if mode_row else "paper"

            edge_row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'min_edge_threshold'"
            ).fetchone()
            edge = float(edge_row["value"]) if edge_row else 0.08

            size_row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'max_position_usdc'"
            ).fetchone()
            size = float(size_row["value"]) if size_row else 50.0

        return {
            "halted": halted,
            "mode": mode,
            "min_edge_threshold": edge,
            "max_position_usdc": size,
        }
    except Exception as e:
        return {"halted": False, "mode": "paper", "min_edge_threshold": 0.08, "max_position_usdc": 50.0, "error": str(e)}


# ── API: Markets ──────────────────────────────────────────────────────────────

@app.get("/api/markets")
async def api_markets():
    try:
        with _db() as conn:
            rows = conn.execute("""
                SELECT
                    m.id, m.question, m.yes_price, m.volume,
                    m.end_date, m.start_date, m.start_price,
                    m.parse_status, m.parsed,
                    lc.parsed AS llm_parsed,
                    lc.model AS llm_model
                FROM markets m
                LEFT JOIN llm_cache lc ON lc.question = m.question
                ORDER BY m.volume DESC
                LIMIT 50
            """).fetchall()

        markets = []
        for r in rows:
            parsed = {}
            if r["parsed"]:
                try:
                    parsed = json.loads(r["parsed"])
                except Exception:
                    pass

            llm_data = {}
            if r["llm_parsed"]:
                try:
                    llm_data = json.loads(r["llm_parsed"])
                except Exception:
                    pass

            # Pull latest weather signal if available
            model_prob = None
            edge = None
            weather_summary = None
            try:
                with _db() as conn2:
                    sig = conn2.execute("""
                        SELECT model_prob, adjusted_edge, market_price
                        FROM signals WHERE market_id = ? ORDER BY created_at DESC LIMIT 1
                    """, (r["id"],)).fetchone()
                    if sig:
                        model_prob = sig["model_prob"]
                        edge = sig["adjusted_edge"]
                        if model_prob is not None:
                            weather_summary = f"{round(model_prob*100)}% model prob"
            except Exception:
                pass

            markets.append({
                "id": r["id"],
                "question": r["question"],
                "yes_price": r["yes_price"],
                "no_price": (1.0 - float(r["yes_price"])) if r["yes_price"] is not None else None,
                "start_price": r["start_price"],
                "start_date": r["start_date"],
                "volume": r["volume"],
                "end_date": r["end_date"],
                "parse_status": r["parse_status"],
                "city": parsed.get("city") or llm_data.get("city", "—"),
                "metric": parsed.get("metric") or llm_data.get("metric", "—"),
                "threshold": parsed.get("threshold") or llm_data.get("threshold"),
                "operator": parsed.get("operator") or llm_data.get("operator", ""),
                "lat": parsed.get("lat") or llm_data.get("lat"),
                "lon": parsed.get("lon") or llm_data.get("lon"),
                "model_prob": model_prob,
                "edge": edge,
                "weather_summary": weather_summary,
                "llm_model": r["llm_model"],
            })

        return {"markets": markets, "total": len(markets)}
    except Exception as e:
        return {"markets": [], "total": 0, "error": str(e)}


def _weather_summary(wd: dict, parsed: dict) -> str:
    """Build a human-readable weather summary from raw weather data."""
    try:
        metric = parsed.get("metric", "")
        threshold = parsed.get("threshold")
        operator = parsed.get("operator", ">")
        prob = wd.get("probability")
        if prob is not None:
            pct = round(prob * 100)
            return f"{pct}% probability"
        val = wd.get("value") or wd.get("temperature") or wd.get("precip")
        if val is not None and threshold is not None:
            unit = "°F" if "temp" in metric else "in"
            return f"{val:.1f}{unit} vs {operator}{threshold}{unit}"
    except Exception:
        pass
    return "—"


# ── API: Trades ───────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def api_trades():
    try:
        with _db() as conn:
            rows = conn.execute("""
                SELECT t.id, t.market_id, t.direction, t.final_size AS size,
                       t.simulated_fill_price AS price, t.status,
                       t.created_at, t.rationale, m.question
                FROM paper_trades t
                LEFT JOIN markets m ON m.id = t.market_id
                ORDER BY t.created_at DESC LIMIT 30
            """).fetchall()

        return {"trades": [dict(r) for r in rows]}
    except Exception as e:
        return {"trades": [], "error": str(e)}


# ── API: Portfolio ────────────────────────────────────────────────────────────

@app.get("/api/portfolio")
async def api_portfolio():
    try:
        with _db() as conn:
            snap = conn.execute("""
                SELECT * FROM portfolio_snapshots ORDER BY created_at DESC LIMIT 1
            """).fetchone()

            positions = conn.execute("""
                SELECT p.market_id, p.direction, p.size, p.entry_price,
                       m.question, m.yes_price,
                       (SELECT rationale FROM paper_trades
                        WHERE market_id = p.market_id
                        ORDER BY created_at DESC LIMIT 1) AS rationale
                FROM paper_positions p
                LEFT JOIN markets m ON m.id = p.market_id
                WHERE p.size > 0
            """).fetchall()

        snapshot = dict(snap) if snap else {}
        return {
            "snapshot": snapshot,
            "positions": [dict(p) for p in positions],
        }
    except Exception as e:
        return {"snapshot": {}, "positions": [], "error": str(e)}


# ── API: Weights ──────────────────────────────────────────────────────────────

@app.get("/api/weights")
async def api_weights():
    try:
        with _db() as conn:
            rows = conn.execute("""
                SELECT source, weight, brier_score, sample_count
                FROM calibration_weights ORDER BY weight DESC
            """).fetchall()
        return {"weights": [dict(r) for r in rows]}
    except Exception as e:
        return {"weights": [], "error": str(e)}


# ── API: Logs ─────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_logs():
    try:
        if not _BOT_LOG.exists():
            return {"lines": ["[log file not found]"]}
        lines = _BOT_LOG.read_text(errors="replace").splitlines()
        return {"lines": lines[-100:]}
    except Exception as e:
        return {"lines": [f"Error reading log: {e}"]}


# ── API: Control ──────────────────────────────────────────────────────────────

@app.post("/api/control")
async def api_control(request: Request):
    try:
        body = await request.json()
        with _db() as conn:
            for key, val in body.items():
                conn.execute("""
                    INSERT INTO system_config (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """, (key, str(val)))
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
