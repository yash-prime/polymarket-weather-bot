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

from fastapi import FastAPI, HTTPException, Request
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

            scan_row = conn.execute(
                "SELECT value FROM system_config WHERE key = 'last_scan_at'"
            ).fetchone()
            last_scan_at = scan_row["value"] if scan_row else None

        return {
            "halted": halted,
            "mode": mode,
            "min_edge_threshold": edge,
            "max_position_usdc": size,
            "last_scan_at": last_scan_at,
            "scan_interval_seconds": 300,
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
                    lc.model AS llm_model,
                    CASE WHEN pp.size > 0 THEN pp.direction ELSE NULL END AS open_direction
                FROM markets m
                LEFT JOIN llm_cache lc ON lc.question = m.question
                LEFT JOIN paper_positions pp ON pp.market_id = m.id AND pp.size > 0
                ORDER BY pp.size DESC NULLS LAST, m.volume DESC
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
                "open_direction": r["open_direction"],
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
                SELECT * FROM portfolio_snapshots ORDER BY snapshot_at DESC LIMIT 1
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

        pos_list = []
        for p in positions:
            entry = p["entry_price"] or 0
            yes_price = p["yes_price"] or entry
            size = p["size"] or 0
            direction = p["direction"]
            if direction == "YES":
                unrealized = size * (yes_price / entry - 1) if entry > 0 else 0.0
            else:
                no_entry = 1.0 - entry
                no_current = 1.0 - yes_price
                unrealized = size * (no_current / no_entry - 1) if no_entry > 0 else 0.0
            row = dict(p)
            row["unrealized_pnl"] = unrealized
            row["current_price"] = yes_price
            pos_list.append(row)

        deployed = sum(float(p["size"]) for p in positions if (p["size"] or 0) > 0)

        return {
            "snapshot": snapshot,
            "positions": pos_list,
            "deployed": round(deployed, 2),
            "starting_capital": 2000.0,
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


# ── API: Intelligence Reports ─────────────────────────────────────────────────

@app.get("/api/intelligence-reports")
def api_get_reports():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, generated_at, content FROM intelligence_reports ORDER BY generated_at DESC"
        ).fetchall()
    return {"reports": [dict(r) for r in rows]}


@app.post("/api/intelligence-report")
def api_generate_report():
    from llm.openrouter_client import generate as or_generate, is_configured
    from llm.ollama_client import generate as ollama_generate

    with _db() as conn:
        positions = conn.execute("""
            SELECT p.market_id, p.direction, p.size, p.entry_price,
                   p.current_price, p.unrealized_pnl,
                   m.question, m.yes_price, m.end_date, m.volume,
                   (SELECT rationale FROM paper_trades
                    WHERE market_id = p.market_id
                    ORDER BY created_at DESC LIMIT 1) as rationale
            FROM paper_positions p
            JOIN markets m ON m.id = p.market_id
            WHERE p.size > 0 AND p.status = 'open'
            ORDER BY p.unrealized_pnl DESC
        """).fetchall()

        snap = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_at DESC LIMIT 1"
        ).fetchone()

    if not positions:
        raise HTTPException(status_code=400, detail="No open positions")

    # Build position list text
    pos_lines = []
    for i, p in enumerate(positions, 1):
        pnl = p["unrealized_pnl"] or 0
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        rationale = p["rationale"] or "no rationale stored"
        pos_lines.append(
            f"{i}. [{p['direction']}] {p['question']}\n"
            f"   Size: ${p['size']:.2f} | Entry: {p['entry_price']:.3f} | "
            f"Current YES: {p['yes_price']:.3f} | P&L: {pnl_str}\n"
            f"   Ends: {p['end_date']} | Rationale: {rationale}"
        )

    equity = snap["total_equity"] if snap else 2000.0
    deployed = sum(p["size"] for p in positions)
    unrealized = sum(p["unrealized_pnl"] or 0 for p in positions)

    prompt = f"""You are a trading analyst reviewing a paper trading account on Polymarket weather prediction markets.

PORTFOLIO SUMMARY:
- Starting Capital: $2,000
- Current Equity: ${equity:.2f}
- Total Deployed: ${deployed:.2f}
- Unrealized P&L: ${unrealized:+.2f}
- Open Positions: {len(positions)}

OPEN POSITIONS:
{chr(10).join(pos_lines)}

Please provide a comprehensive Intelligence Report covering:

1. **Portfolio Overview** — Overall health, performance, and capital deployment
2. **Position Analysis** — Which positions are winning/losing and why based on the rationale
3. **Contradicting Trades** — Any positions that directly contradict each other (e.g., betting YES on Seattle rain AND NO on Seattle rain in overlapping windows, or positions that logically conflict)
4. **Duplicate/Overlapping Trades** — Any positions covering the same event or very similar thresholds at the same location
5. **Risk Assessment** — Concentration risks, correlated positions, any positions with outsized size vs edge
6. **Key Insights & Recommendations** — What adjustments, if any, should be considered

Write in clear professional prose. Be specific about which trade numbers conflict or overlap. Be concise but complete."""

    SYSTEM = (
        "You are a quantitative trading analyst. Provide structured, insightful analysis "
        "of prediction market positions. Be direct, specific, and actionable."
    )

    try:
        if is_configured():
            content = or_generate(prompt, system=SYSTEM)
        else:
            content = ollama_generate(prompt, system=SYSTEM)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}")

    with _db() as conn:
        conn.execute(
            "INSERT INTO intelligence_reports (content) VALUES (?)",
            (content,)
        )
        conn.commit()
        report = conn.execute(
            "SELECT id, generated_at, content FROM intelligence_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return {"id": report["id"], "generated_at": report["generated_at"], "content": report["content"]}


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
