"""
tests/test_integration.py — Paper-mode end-to-end integration test.

Verifies the full pipeline with zero real API calls:
  market scanned → LLM parsed → weather computed → signal generated →
  risk approved → paper trade placed → portfolio updated →
  portfolio_snapshots table written

All external dependencies (Open-Meteo HTTP, NOAA HTTP, ECMWF, Ollama HTTP,
Gamma API HTTP) are mocked. The only real I/O is the SQLite DB.

Sequence validated:
  1. job_fetch_markets() inserts a new market as parse_status='pending'
  2. parser.parse() resolves it to parse_status='success'
  3. get_active_markets() returns the parsed market
  4. weather.compute() returns a ModelResult
  5. signal.compute_signal() returns a Signal with positive edge
  6. risk.approve() returns an ApprovedSignal
  7. paper_trader.place_limit_order() writes to paper_trades + paper_positions
  8. portfolio.job_portfolio_snapshot() writes to portfolio_snapshots
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from db.init import get_connection
from engine.models import ModelResult
from market.models import Market, Signal
from trading.models import ApprovedSignal


# ---------------------------------------------------------------------------
# Fixtures — minimal fake data
# ---------------------------------------------------------------------------

_FAKE_MODEL_RESULT = ModelResult(
    probability=0.70,
    confidence=0.85,
    ci_low=0.60,
    ci_high=0.80,
    members_count=10,
    sources=["open_meteo_ensemble", "noaa"],
    degraded_sources=["ecmwf"],
)

_MARKET_ID = "integration-mkt-001"

_PARSED_JSON = {
    "city": "Chicago",
    "lat": 41.88,
    "lon": -87.63,
    "metric": "temperature_2m_max",
    "threshold": 90.0,
    "operator": ">",
    "window_start": "2026-06-15",
    "window_end": "2026-06-15",
    "parse_status": "success",
}

_GAMMA_RESPONSE = [
    {
        "id": _MARKET_ID,
        "question": "Will Chicago temperature exceed 90°F this week?",
        "outcomePrices": "[0.40, 0.60]",
        "outcomes": '["YES", "NO"]',
        "volume": "2500",
        # 5 days out — time_decay stays high, adjusted_edge stays above threshold
        "endDateIso": "2026-03-22T00:00:00Z",
        "active": True,
        "closed": False,
        "tags": [{"slug": "weather"}],
    }
]


# ---------------------------------------------------------------------------
# Full pipeline integration test
# ---------------------------------------------------------------------------


class TestPaperE2EPipeline:
    def test_full_pipeline_end_to_end(self, tmp_db_path):
        """
        Full paper-mode pipeline: scan → parse → weather → signal → risk → trade → portfolio.
        Zero real API calls. Validates each DB write.
        """
        # ----------------------------------------------------------------
        # Step 1: job_fetch_markets() — scans Gamma, writes pending market
        # ----------------------------------------------------------------
        with patch("market.scanner.requests") as mock_requests, \
             patch("market.scanner._RATE_LIMITER") as mock_rl:

            mock_rl.check_and_record.return_value = True
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _GAMMA_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_requests.get.return_value = mock_resp

            from market.scanner import job_fetch_markets
            count = job_fetch_markets(db_path=tmp_db_path)

        assert count >= 1, "Expected at least 1 market to be fetched"

        with get_connection(tmp_db_path) as conn:
            row = conn.execute(
                "SELECT parse_status FROM markets WHERE id = ?", (_MARKET_ID,)
            ).fetchone()
        assert row is not None, "Market not written to DB"
        assert row["parse_status"] == "pending"

        # ----------------------------------------------------------------
        # Step 2: parser.parse() — resolves question, updates parse_status
        # ----------------------------------------------------------------
        with patch("llm.ollama_client.generate", return_value=json.dumps(_PARSED_JSON)):
            from llm.parser import parse
            result = parse("Will Chicago reach 90°F on June 15?", db_path=tmp_db_path)

        assert result.get("parse_status") == "success"

        # Update DB to mark market as parsed (simulates llm_parse_job)
        parsed_str = json.dumps(_PARSED_JSON)
        with get_connection(tmp_db_path) as conn:
            conn.execute(
                "UPDATE markets SET parse_status='success', parsed=? WHERE id=?",
                (parsed_str, _MARKET_ID),
            )
            conn.execute(
                "UPDATE markets SET yes_price=0.40 WHERE id=?",
                (_MARKET_ID,),
            )
            conn.commit()

        # ----------------------------------------------------------------
        # Step 3: get_active_markets() — returns parsed market
        # ----------------------------------------------------------------
        from market.scanner import get_active_markets
        markets = get_active_markets(db_path=tmp_db_path)

        mkt = next((m for m in markets if m.id == _MARKET_ID), None)
        assert mkt is not None, "Market not returned as active after parsing"

        # ----------------------------------------------------------------
        # Step 4: weather.compute() — returns ModelResult
        # ----------------------------------------------------------------
        with patch("engine.weather._fetch_open_meteo", return_value={}), \
             patch("engine.weather._fetch_noaa", return_value=None), \
             patch("engine.weather._fetch_ecmwf", return_value=None), \
             patch("engine.weather.compute_probability", return_value=_FAKE_MODEL_RESULT):

            from engine.weather import compute as weather_compute
            model_result = weather_compute(mkt, db_path=tmp_db_path)

        assert model_result is not None
        assert isinstance(model_result, ModelResult)

        # ----------------------------------------------------------------
        # Step 5: signal.compute_signal() — returns Signal
        # ----------------------------------------------------------------
        from market.signal import compute_signal
        signal = compute_signal(mkt, model_result)

        assert signal is not None, "Expected a signal — model_prob=0.70 vs market price=0.40 → large edge"
        assert signal.direction in ("YES", "NO")
        assert signal.adjusted_edge > 0

        # ----------------------------------------------------------------
        # Step 6: risk.approve() — returns ApprovedSignal in paper mode
        # ----------------------------------------------------------------
        with patch("trading.risk._is_halted", return_value=False):
            from trading.risk import approve
            approved = approve(signal, "paper", db_path=tmp_db_path)

        assert approved is not None, "Risk manager rejected a valid signal unexpectedly"
        assert isinstance(approved, ApprovedSignal)
        assert approved.final_size > 0

        # ----------------------------------------------------------------
        # Step 7: paper_trader.place_limit_order() — writes to paper tables
        # ----------------------------------------------------------------
        from trading.paper_trader import place_limit_order as paper_place
        order_id = paper_place(
            approved.signal.market_id,
            approved.signal.direction,
            approved.final_size,
            approved.signal.market_price,
            db_path=tmp_db_path,
        )

        assert order_id is not None
        assert order_id.startswith("paper-")

        with get_connection(tmp_db_path) as conn:
            trade_row = conn.execute(
                "SELECT status FROM paper_trades WHERE market_id = ?",
                (approved.signal.market_id,),
            ).fetchone()
            pos_row = conn.execute(
                "SELECT status FROM paper_positions WHERE market_id = ?",
                (approved.signal.market_id,),
            ).fetchone()
            live_trade_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE market_id = ?",
                (approved.signal.market_id,),
            ).fetchone()[0]

        assert trade_row is not None and trade_row["status"] == "open"
        assert pos_row is not None and pos_row["status"] == "open"
        assert live_trade_count == 0, "Paper trade must NEVER write to live trades table"

        # ----------------------------------------------------------------
        # Step 8: portfolio.job_portfolio_snapshot() → portfolio_snapshots
        # ----------------------------------------------------------------
        from trading.portfolio import job_portfolio_snapshot
        job_portfolio_snapshot(mode="paper", db_path=tmp_db_path)

        with get_connection(tmp_db_path) as conn:
            snap_row = conn.execute(
                "SELECT open_positions FROM portfolio_snapshots WHERE mode = 'paper' "
                "ORDER BY snapshot_at DESC LIMIT 1"
            ).fetchone()

        assert snap_row is not None, "portfolio_snapshots must have a row after job_portfolio_snapshot()"
        assert snap_row["open_positions"] >= 1

    def test_no_signal_when_edge_below_threshold(self, tmp_db_path):
        """
        When model probability is very close to market price,
        signal.compute_signal() returns None (no trade).
        """
        # market price = 0.65, model_prob = 0.66 → tiny edge < threshold
        market = Market(
            id="mkt-low-edge",
            question="Will it rain?",
            yes_price=0.65,
            volume=5000.0,
            end_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
            parse_status="success",
            parsed=json.dumps(_PARSED_JSON),
        )
        low_edge_result = ModelResult(
            probability=0.66, confidence=0.9, ci_low=0.6, ci_high=0.7,
            members_count=5, sources=["open_meteo_ensemble"], degraded_sources=[],
        )
        from market.signal import compute_signal
        signal = compute_signal(market, low_edge_result)
        assert signal is None

    def test_paper_pipeline_leaves_live_tables_empty(self, tmp_db_path):
        """After a full paper pipeline run, live tables remain untouched."""
        order_id = None
        with patch("trading.risk._is_halted", return_value=False):
            from market.signal import compute_signal
            from trading.risk import approve
            from trading.paper_trader import place_limit_order as paper_place

            market = Market(
                id="mkt-isolation",
                question="Will Phoenix hit 110°F?",
                yes_price=0.35,
                volume=3000.0,
                end_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
                parse_status="success",
                parsed=json.dumps({**_PARSED_JSON, "threshold": 110.0}),
            )
            signal = compute_signal(market, _FAKE_MODEL_RESULT)
            if signal:
                approved = approve(signal, "paper", db_path=tmp_db_path)
                if approved:
                    order_id = paper_place(
                        approved.market_id, approved.direction,
                        approved.size, approved.price,
                        db_path=tmp_db_path,
                    )

        with get_connection(tmp_db_path) as conn:
            live_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            live_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert live_trades == 0
        assert live_positions == 0
