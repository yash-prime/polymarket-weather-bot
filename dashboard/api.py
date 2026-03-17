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
                       t.created_at, t.closed_at, t.rationale,
                       t.realized_pnl, t.close_reason, m.question
                FROM paper_trades t
                LEFT JOIN markets m ON m.id = t.market_id
                ORDER BY COALESCE(t.closed_at, t.created_at) DESC LIMIT 100
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
    prompt = f"""PORTFOLIO SNAPSHOT — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}

Starting Capital : $2,500.00
Current Equity   : ${equity:,.2f}
Total P&L        : {total_pnl:+.2f} ({total_pnl/STARTING_CAPITAL*100:+.2f}% of starting capital)
  Unrealized     : {unrealized:+.2f}
  Realized       : {realized:+.2f}
Deployed         : ${deployed:,.2f}  ({deployed/STARTING_CAPITAL*100:.0f}% of capital at risk)
Available        : ${STARTING_CAPITAL - deployed:,.2f}
Open Positions   : {len(positions)}

OPEN POSITIONS (sorted by expiry, soonest first):
{positions_block}

---
INSTRUCTIONS: Write a detailed Intelligence Report in markdown. Cover every section below fully — do not skip any, do not truncate. Use specific position numbers, dollar amounts, and percentages throughout. Explain your reasoning in plain English that a non-quant can understand.

## 📊 Portfolio Overview
Write 4–5 sentences. Explain: current equity vs starting capital, what percentage is deployed, overall P&L trajectory, and whether the portfolio is in a healthy state. Mention how many positions are winning vs losing.

## 🏆 Top Performers
List ALL positions with positive P&L, ranked best to worst. For each write one line: position number, shortened question, dollar P&L, and one sentence explaining why this position is profitable (is the market moving in our favor? is the edge playing out?).

## 📉 Worst Positions
List ALL positions with negative P&L, ranked worst to best. For each: position number, shortened question, dollar loss, and one sentence on what went wrong or what risk remains. Be honest — if a position looks bad, say so.

## ⚠️ Contradicting / Conflicting Trades
Look carefully for any bets that logically conflict with each other. Examples:
- YES on "Seattle rain > 5 inches" AND YES on "Seattle rain < 3 inches" in the same month (both can't win)
- YES on "temp > 15°C" AND YES on "temp < 14°C" same city same day
If you find conflicts, name the positions, explain the contradiction, and say what should happen.
If none, write "None found — conflict resolution appears to be working."

## 🔁 Overlapping / Redundant Exposure
Look for clusters of positions on the same underlying event with adjacent bins — e.g. multiple temperature buckets for the same city and date, or multiple precipitation ranges for the same city and month. Explain the combined exposure and what it means. If this is fine, say why.

## 🎯 Concentration & Risk Analysis
- Which city, metric, or event type has the most capital at risk? Give exact figures.
- Which positions expire soonest — are they positioned correctly for the remaining time?
- Are there any positions where the model probability is far from the market price but P&L is still negative? Explain the discrepancy.
- Any positions with model probability at 0% or 100% — are these believable?

## 🧠 Model Quality Check
Look at the edge figures and model probabilities. Call out anything suspicious:
- Model showing 0% or 100% probability (extreme — usually a data or parsing issue)
- Very high edge (>50%) that seems too good to be true
- Positions where our thesis seems clearly wrong given the current P&L

## 💡 Recommended Actions
Give 5–7 specific, actionable bullet points. Each must start with an action verb and reference specific positions by number. Examples: "Close #03 immediately because...", "Monitor #07 closely — expires in 2 days and is underwater", "Investigate why #12 shows 100% model probability"."""

    SYSTEM = (
        "You are a senior quantitative trading analyst reviewing a prediction market portfolio. "
        "Your job is to write a thorough, honest Intelligence Report that clearly explains what is happening, "
        "what is working, what is broken, and what to do about it. "
        "Write in plain English — specific, direct, and complete. "
        "Never truncate sections. Never say 'see above'. Never use filler phrases. "
        "Output clean markdown only — no preamble, no 'here is your report', start directly with the first section header. "
        "Use exact dollar amounts and percentages from the data provided."
    )

    try:
        if is_configured():
            content = or_generate(prompt, system=SYSTEM, max_tokens=4096, timeout=120)
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
