"""
dashboard/app.py — Streamlit UI for the Polymarket Weather Bot.

Panels:
  1. Header bar       — mode badge, kill switch toggle, bot status
  2. Active markets   — table of parsed markets with yes_price, volume
  3. Ensemble chart   — bar chart of model weights from calibration_weights
  4. LLM analysis     — latest trade commentary from llm_cache
  5. Trade log        — recent trades (paper or live)
  6. Bot status       — portfolio snapshot, open positions

Controls (write to system_config table, never mutate in-memory state):
  - Kill switch toggle     (key: bot_halted)
  - Mode toggle            (key: trading_mode)
  - Edge threshold slider  (key: min_edge_threshold)
  - Position size input    (key: max_position_usdc)
  - Per-market lock        (market_overrides table)

Data source: SQLite DB only (read-only from dashboard perspective).
Auto-refreshes every 30 seconds via st.rerun().

Run with:
    streamlit run dashboard/app.py
"""
import time

import streamlit as st

from config import settings

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Polymarket Weather Bot",
    page_icon="⛅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# DB helpers (read-only)
# ---------------------------------------------------------------------------


def _get_connection():
    from db.init import get_connection
    return get_connection()


def _read_system_config() -> dict:
    """Read all rows from system_config into a dict."""
    try:
        with _get_connection() as conn:
            rows = conn.execute("SELECT key, value FROM system_config").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _write_system_config(key: str, value: str) -> None:
    """Write a single system_config key (dashboard control)."""
    try:
        with _get_connection() as conn:
            conn.execute(
                "INSERT INTO system_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to write config: {exc}")


def _get_active_markets() -> list[dict]:
    try:
        with _get_connection() as conn:
            rows = conn.execute(
                "SELECT id, question, yes_price, volume, end_date, parse_status "
                "FROM markets WHERE parse_status = 'success' "
                "ORDER BY volume DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _get_calibration_weights() -> list[dict]:
    try:
        with _get_connection() as conn:
            rows = conn.execute(
                "SELECT source, region, season, weight "
                "FROM calibration_weights ORDER BY weight DESC LIMIT 30"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _get_recent_trades(mode: str, limit: int = 20) -> list[dict]:
    table = "trades" if mode == "live" else "paper_trades"
    try:
        with _get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _get_portfolio_snapshot(mode: str) -> dict | None:
    try:
        with _get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE mode = ? "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (mode,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def _get_open_positions(mode: str) -> list[dict]:
    table = "positions" if mode == "live" else "paper_positions"
    try:
        with _get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE status = 'open'",  # noqa: S608
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _get_market_overrides() -> list[dict]:
    try:
        with _get_connection() as conn:
            rows = conn.execute("SELECT * FROM market_overrides").fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _set_market_lock(market_id: str, locked: bool) -> None:
    """
    Lock a market by setting action='skip' in market_overrides.
    Unlock by deleting the override row.
    """
    try:
        with _get_connection() as conn:
            if locked:
                conn.execute(
                    "INSERT INTO market_overrides (market_id, action) VALUES (?, 'skip') "
                    "ON CONFLICT(market_id) DO UPDATE SET action = 'skip'",
                    (market_id,),
                )
            else:
                conn.execute(
                    "DELETE FROM market_overrides WHERE market_id = ?",
                    (market_id,),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to lock market: {exc}")


# ---------------------------------------------------------------------------
# Sidebar — Controls
# ---------------------------------------------------------------------------


def _render_sidebar(cfg: dict) -> None:
    st.sidebar.title("⚙ Controls")

    # Kill switch
    is_halted = str(cfg.get("bot_halted", "0")).lower() in ("1", "true")
    new_halted = st.sidebar.toggle(
        "🛑 Kill Switch (halt trading)",
        value=is_halted,
        key="kill_switch",
    )
    if new_halted != is_halted:
        _write_system_config("bot_halted", "1" if new_halted else "0")
        st.rerun()

    st.sidebar.divider()

    # Mode toggle
    current_mode = cfg.get("trading_mode", settings.TRADING_MODE)
    mode_options = ["paper", "live"]
    mode_idx = mode_options.index(current_mode) if current_mode in mode_options else 0
    new_mode = st.sidebar.selectbox("Trading Mode", mode_options, index=mode_idx)
    if new_mode != current_mode:
        _write_system_config("trading_mode", new_mode)
        st.rerun()

    st.sidebar.divider()

    # Edge threshold slider
    current_edge = float(cfg.get("min_edge_threshold", settings.MIN_EDGE_THRESHOLD))
    new_edge = st.sidebar.slider(
        "Min Edge Threshold",
        min_value=0.01,
        max_value=0.50,
        value=current_edge,
        step=0.01,
        format="%.2f",
    )
    if abs(new_edge - current_edge) > 0.001:
        _write_system_config("min_edge_threshold", str(new_edge))

    # Max position size
    current_size = float(cfg.get("max_position_usdc", settings.MAX_POSITION_USDC))
    new_size = st.sidebar.number_input(
        "Max Position Size (USDC)",
        min_value=1.0,
        max_value=10000.0,
        value=current_size,
        step=1.0,
    )
    if abs(new_size - current_size) > 0.01:
        _write_system_config("max_position_usdc", str(new_size))


# ---------------------------------------------------------------------------
# Panel 1: Header bar
# ---------------------------------------------------------------------------


def _render_header(cfg: dict) -> None:
    mode = cfg.get("trading_mode", settings.TRADING_MODE)
    is_halted = str(cfg.get("bot_halted", "0")).lower() in ("1", "true")

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        st.title("⛅ Polymarket Weather Bot")
    with col2:
        badge_color = "red" if mode == "live" else "blue"
        st.markdown(
            f"<span style='background:{badge_color};color:white;padding:4px 10px;"
            f"border-radius:4px;font-weight:bold;'>{mode.upper()}</span>",
            unsafe_allow_html=True,
        )
    with col3:
        status = "🛑 HALTED" if is_halted else "✅ RUNNING"
        st.markdown(f"**{status}**")


# ---------------------------------------------------------------------------
# Panel 2: Active markets table
# ---------------------------------------------------------------------------


def _render_active_markets() -> None:
    st.subheader("📈 Active Markets")
    markets = _get_active_markets()
    if not markets:
        st.info("No active parsed markets.")
        return

    import pandas as pd
    df = pd.DataFrame(markets)
    # Truncate long questions
    if "question" in df.columns:
        df["question"] = df["question"].str[:80]
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Per-market lock controls
    with st.expander("🔒 Per-market locks"):
        overrides = {o["market_id"]: o["action"] == "skip" for o in _get_market_overrides()}
        for mkt in markets[:10]:  # Show top 10
            mid = mkt["id"]
            locked = bool(overrides.get(mid, 0))
            new_locked = st.checkbox(f"Lock {mid[:16]}…", value=locked, key=f"lock_{mid}")
            if new_locked != locked:
                _set_market_lock(mid, new_locked)


# ---------------------------------------------------------------------------
# Panel 3: Ensemble chart (model weights)
# ---------------------------------------------------------------------------


def _render_ensemble_chart() -> None:
    st.subheader("🌡 Ensemble Model Weights")
    weights = _get_calibration_weights()
    if not weights:
        st.info("No calibration weights yet — uniform weights in use.")
        return

    import pandas as pd
    df = pd.DataFrame(weights)
    df["label"] = df["source"] + "/" + df["region"].fillna("*") + "/" + df["season"].fillna("*")
    st.bar_chart(df.set_index("label")["weight"])


# ---------------------------------------------------------------------------
# Panel 4: LLM analysis (latest entries from llm_cache)
# ---------------------------------------------------------------------------


def _render_llm_analysis() -> None:
    st.subheader("🤖 LLM Analysis Cache")
    try:
        with _get_connection() as conn:
            rows = conn.execute(
                "SELECT question_hash, result, created_at FROM llm_cache "
                "ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []

    if not rows:
        st.info("No LLM cache entries yet.")
        return

    for row in rows:
        with st.expander(f"Hash: {row['question_hash'][:16]}… — {row['created_at']}"):
            st.code(row["result"], language="json")


# ---------------------------------------------------------------------------
# Panel 5: Trade log
# ---------------------------------------------------------------------------


def _render_trade_log(mode: str) -> None:
    st.subheader(f"📋 Trade Log ({mode})")
    trades = _get_recent_trades(mode)
    if not trades:
        st.info(f"No {mode} trades yet.")
        return

    import pandas as pd
    df = pd.DataFrame(trades)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Panel 6: Bot status (portfolio + open positions)
# ---------------------------------------------------------------------------


def _render_bot_status(mode: str) -> None:
    st.subheader("📊 Portfolio Status")
    snap = _get_portfolio_snapshot(mode)

    if snap:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Equity", f"${snap['total_equity']:.2f}")
        col2.metric("Unrealized P&L", f"${snap['unrealized_pnl']:.2f}")
        col3.metric("Daily P&L", f"${snap['daily_pnl']:.2f}")
        col4.metric("Open Positions", snap["open_positions"])
        st.caption(f"Snapshot at: {snap['snapshot_at']}")
    else:
        st.info("No portfolio snapshot yet.")

    # Open positions table
    positions = _get_open_positions(mode)
    if positions:
        st.subheader(f"Open Positions ({len(positions)})")
        import pandas as pd
        st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------


def _schedule_rerun(interval_seconds: int = 30) -> None:
    """Schedule a rerun after interval_seconds using st.empty + time.sleep."""
    placeholder = st.empty()
    for remaining in range(interval_seconds, 0, -1):
        placeholder.caption(f"Auto-refresh in {remaining}s…")
        time.sleep(1)
    placeholder.empty()
    st.rerun()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


def main() -> None:
    cfg = _read_system_config()
    mode = cfg.get("trading_mode", settings.TRADING_MODE)

    _render_sidebar(cfg)
    _render_header(cfg)
    st.divider()

    col_left, col_right = st.columns([2, 1])

    with col_left:
        _render_active_markets()
        st.divider()
        _render_trade_log(mode)

    with col_right:
        _render_bot_status(mode)
        st.divider()
        _render_ensemble_chart()
        st.divider()
        _render_llm_analysis()

    # Auto-refresh every 30 s
    _schedule_rerun(30)


if __name__ == "__main__":
    main()
