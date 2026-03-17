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
            "starting_capital": 2500.0,
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

    from datetime import datetime, timezone

    with _db() as conn:
        positions = conn.execute("""
            SELECT p.market_id, p.direction, p.size, p.entry_price,
                   p.unrealized_pnl, m.question, m.yes_price, m.end_date,
                   m.parsed,
                   (SELECT rationale FROM paper_trades
                    WHERE market_id = p.market_id
                    ORDER BY created_at DESC LIMIT 1) as rationale,
                   (SELECT adjusted_edge FROM signals
                    WHERE market_id = p.market_id
                    ORDER BY created_at DESC LIMIT 1) as edge_at_entry
            FROM paper_positions p
            JOIN markets m ON m.id = p.market_id
            WHERE p.size > 0 AND p.status = 'open'
            ORDER BY m.end_date ASC
        """).fetchall()

        snap = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_at DESC LIMIT 1"
        ).fetchone()

    if not positions:
        raise HTTPException(status_code=400, detail="No open positions")

    # ── Build enriched position rows ───────────────────────────
    import json as _json
    now_utc = datetime.now(timezone.utc)
    STARTING_CAPITAL = 2500.0

    equity     = float(snap["total_equity"]) if snap else STARTING_CAPITAL
    deployed   = sum(float(p["size"]) for p in positions)
    unrealized = sum(float(p["unrealized_pnl"] or 0) for p in positions)
    realized   = float(snap["realized_pnl"]) if snap else 0.0
    total_pnl  = unrealized + realized

    pos_rows = []
    for i, p in enumerate(positions, 1):
        pnl    = float(p["unrealized_pnl"] or 0)
        size   = float(p["size"])
        entry  = float(p["entry_price"])
        yes    = float(p["yes_price"])
        edge   = float(p["edge_at_entry"]) if p["edge_at_entry"] else None
        pnl_pct = (pnl / size * 100) if size else 0

        # Days to expiry
        try:
            end_dt  = datetime.strptime(p["end_date"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_left = max(0, (end_dt - now_utc).days)
        except Exception:
            days_left = "?"

        # Location from parsed JSON
        location = "Unknown"
        try:
            parsed = _json.loads(p["parsed"] or "{}")
            location = parsed.get("city") or "Unknown"
        except Exception:
            pass

        direction = p["direction"]
        win_loss  = "▲ WIN" if pnl > 0.05 else ("▼ LOSS" if pnl < -0.05 else "◆ FLAT")
        edge_str  = f"{edge*100:+.1f}%" if edge is not None else "n/a"
        rationale = (p["rationale"] or "none").strip()

        pos_rows.append(
            f"#{i:02d} | {win_loss} | {direction} | {location}\n"
            f"    Q: {p['question']}\n"
            f"    Size ${size:.0f} | Entry {entry:.3f} → Now {yes:.3f} | "
            f"P&L {pnl:+.2f} ({pnl_pct:+.1f}%) | Edge@entry {edge_str} | "
            f"Expires in {days_left}d\n"
            f"    Rationale: {rationale}"
        )

    positions_block = "\n\n".join(pos_rows)

    # ── Prompt ─────────────────────────────────────────────────
    prompt = f"""You are reviewing a live paper trading account on Polymarket weather prediction markets. Produce an **Intelligence Report** in clean markdown.

═══════════════════════════════════════
ACCOUNT SNAPSHOT  ({now_utc.strftime('%Y-%m-%d %H:%M UTC')})
═══════════════════════════════════════
Starting Capital : $2,500
Current Equity   : ${equity:,.2f}
Total P&L        : {total_pnl:+.2f} ({total_pnl/STARTING_CAPITAL*100:+.2f}%)
  Unrealized     : {unrealized:+.2f}
  Realized       : {realized:+.2f}
Deployed         : ${deployed:,.2f}  ({deployed/STARTING_CAPITAL*100:.0f}% of capital)
Available        : ${STARTING_CAPITAL - deployed:,.2f}
Open Positions   : {len(positions)}
═══════════════════════════════════════

POSITIONS (sorted by expiry, soonest first):
{positions_block}

═══════════════════════════════════════
REPORT INSTRUCTIONS
═══════════════════════════════════════
Write exactly these sections in order. Use the markdown headers shown.
Be specific — reference position numbers (#01, #02 …) when discussing individual trades.

## 📊 Portfolio Overview
2–3 sentences on overall health: equity trend, deployment level, P&L quality (is it driven by real edge or noise?).

## 🏆 Top Performers & 📉 Worst Positions
List the top 3 winning and bottom 3 losing positions by dollar P&L.
For each: position number, question (shortened), P&L, and one sentence on why it's moving that way.

## ⚠️ Contradicting Trades
Identify any pairs where we hold opposing views that logically conflict — e.g. YES on "Seattle > 5in rain" AND NO on "Seattle > 3in rain" in the same month (if YES on >5in is true, NO on >3in is wrong).
If none, write "None detected."

## 🔁 Duplicate / Overlapping Exposure
Identify groups of positions covering the same underlying event or location with adjacent thresholds — e.g. betting on temperature buckets 14°C, 15°C, 16°C, 17°C, 18°C in the same city/day.
Explain what that means for combined risk.
If none, write "None detected."

## 🎯 Risk & Concentration
- Which city / metric / event type is most over-represented?
- Any positions expiring within 2 days that need attention?
- Any position sized > $30 with a negative P&L trend?

## 💡 Key Takeaways
3–5 bullet points. Each starts with an action verb. Be direct and specific."""

    SYSTEM = (
        "You are a sharp quantitative trading analyst. "
        "Output clean markdown only — no preamble, no 'here is your report', just the sections. "
        "Be concise, specific, and ruthlessly actionable. Use numbers."
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
