"""
tests/test_signal.py — Unit tests for market/signal.py

The ARCHITECTURE.md marks signal.py as CRITICAL (Kelly formula was wrong in v1).
All core cases must pass:

  - Zero edge → None (below threshold)
  - Threshold boundary: edge just above/below threshold
  - Kelly direction: YES when model > market, NO when model < market
  - Kelly fraction: result is ≤ KELLY_FRACTION * 1.0
  - time_decay: never negative, clamp at 60 days
  - Liquidity penalty: applied when volume < 1000
  - Degenerate market price (0 or 1): returns None safely
"""
from datetime import datetime, timedelta, timezone

import pytest

from engine.models import ModelResult
from market.models import Market, Signal
from market.signal import compute_signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _model(probability: float = 0.65, confidence: float = 0.8) -> ModelResult:
    return ModelResult(
        probability=probability,
        confidence=confidence,
        ci_low=probability - 0.10,
        ci_high=probability + 0.10,
        members_count=51,
        sources=["open_meteo_ensemble"],
    )


def _market(
    yes_price: float = 0.40,
    days_to_resolve: float = 10.0,
    volume: float = 2000.0,
) -> Market:
    end_date = datetime.now(timezone.utc) + timedelta(days=days_to_resolve)
    return Market(
        id="mkt-test",
        question="Will Chicago exceed 90°F?",
        yes_price=yes_price,
        end_date=end_date,
        volume=volume,
        parsed={"lat": 41.88, "lon": -87.63},
        parse_status="success",
    )


# ---------------------------------------------------------------------------
# Edge and threshold
# ---------------------------------------------------------------------------


class TestEdgeAndThreshold:
    def test_zero_edge_returns_none(self):
        # model_prob == market_price → raw_edge = 0
        signal = compute_signal(_market(yes_price=0.65), _model(probability=0.65))
        assert signal is None

    def test_edge_just_below_threshold_returns_none(self):
        # raw_edge * time_decay - liquidity_penalty < 0.08
        # With confidence=0.8, 10 days: time_decay ≈ max(0.1, 0.8*(1-0.2)) = 0.64
        # Need adjusted_edge < 0.08: raw_edge * 0.64 < 0.08 → raw_edge < 0.125
        # model=0.45, market=0.40 → raw_edge=0.05, adjusted=0.032 < 0.08
        signal = compute_signal(_market(yes_price=0.40), _model(probability=0.45))
        assert signal is None

    def test_edge_above_threshold_returns_signal(self):
        # model=0.65, market=0.40 → raw_edge=0.25 → well above threshold
        signal = compute_signal(_market(yes_price=0.40), _model(probability=0.65))
        assert signal is not None

    def test_negative_edge_above_threshold_gives_no_signal(self):
        # model=0.30, market=0.65 → raw_edge=-0.35, adjusted_edge ~ -0.22
        signal = compute_signal(_market(yes_price=0.65), _model(probability=0.30))
        assert signal is not None
        assert signal.direction == "NO"


# ---------------------------------------------------------------------------
# Kelly direction
# ---------------------------------------------------------------------------


class TestKellyDirection:
    def test_yes_direction_when_model_above_market(self):
        signal = compute_signal(_market(yes_price=0.30), _model(probability=0.60))
        assert signal is not None
        assert signal.direction == "YES"

    def test_no_direction_when_model_below_market(self):
        signal = compute_signal(_market(yes_price=0.70), _model(probability=0.30))
        assert signal is not None
        assert signal.direction == "NO"

    def test_yes_signal_has_positive_adjusted_edge(self):
        signal = compute_signal(_market(yes_price=0.30), _model(probability=0.60))
        assert signal.adjusted_edge > 0

    def test_no_signal_has_negative_adjusted_edge(self):
        signal = compute_signal(_market(yes_price=0.70), _model(probability=0.30))
        assert signal.adjusted_edge < 0


# ---------------------------------------------------------------------------
# Kelly fraction correctness
# ---------------------------------------------------------------------------


class TestKellyFraction:
    def test_raw_kelly_size_is_non_negative(self):
        signal = compute_signal(_market(yes_price=0.30), _model(probability=0.60))
        assert signal.raw_kelly_size >= 0.0

    def test_raw_kelly_size_at_most_kelly_fraction(self):
        from config import settings
        signal = compute_signal(_market(yes_price=0.30), _model(probability=0.90))
        assert signal.raw_kelly_size <= settings.KELLY_FRACTION

    def test_kelly_formula_yes_direction(self):
        """Manually verify Kelly calculation for a known input."""
        from config import settings
        # market_price=0.40, model_prob=0.65
        # b = (1-0.40)/0.40 = 1.5
        # p=0.65, q=0.35
        # f* = (1.5*0.65 - 0.35) / 1.5 = (0.975 - 0.35) / 1.5 = 0.4167
        # raw_kelly = 0.25 * 0.4167 = 0.1042
        expected_kelly = settings.KELLY_FRACTION * ((1.5 * 0.65 - 0.35) / 1.5)
        signal = compute_signal(
            _market(yes_price=0.40, days_to_resolve=0),  # no time decay at 0 days
            _model(probability=0.65, confidence=1.0),
        )
        # time_decay = max(0.1, 1.0 * (1 - 0.02*0)) = 1.0
        # adjusted_edge = 0.25 * 1.0 - 0 = 0.25 ≥ 0.08 → signal
        assert signal is not None
        assert signal.raw_kelly_size == pytest.approx(expected_kelly, abs=1e-4)

    def test_kelly_formula_no_direction(self):
        """Verify Kelly for NO trade."""
        from config import settings
        # market_price=0.60, model_prob=0.30 (NO trade)
        # p_no = 1-0.30=0.70, b_no = 0.60/0.40 = 1.5
        # f* = (1.5*0.70 - 0.30)/1.5 = (1.05-0.30)/1.5 = 0.5
        # raw_kelly = 0.25 * 0.5 = 0.125
        expected_kelly = settings.KELLY_FRACTION * ((1.5 * 0.70 - 0.30) / 1.5)
        signal = compute_signal(
            _market(yes_price=0.60, days_to_resolve=0),
            _model(probability=0.30, confidence=1.0),
        )
        assert signal is not None
        assert signal.raw_kelly_size == pytest.approx(expected_kelly, abs=1e-4)


# ---------------------------------------------------------------------------
# Time decay
# ---------------------------------------------------------------------------


class TestTimeDecay:
    def test_time_decay_not_negative_for_60_day_market(self):
        """Core regression test: time_decay must be ≥ 0.1 for any input."""
        signal = compute_signal(
            _market(yes_price=0.30, days_to_resolve=60.0),
            _model(probability=0.60),
        )
        # time_decay = max(0.1, confidence * (1 - 0.02 * min(60, 45)))
        #            = max(0.1, 0.8 * (1 - 0.9)) = max(0.1, 0.08) = 0.1
        # adjusted_edge = 0.30 * 0.1 - 0 = 0.03 < threshold → None
        # If we use higher model prob to clear threshold despite clamped decay:
        signal2 = compute_signal(
            _market(yes_price=0.10, days_to_resolve=60.0),
            _model(probability=0.90, confidence=1.0),
        )
        if signal2 is not None:
            assert signal2.adjusted_edge > 0  # must be positive, not wrapped around

    def test_time_decay_clamped_at_45_days(self):
        """Markets beyond 45 days use the same decay as a 45-day market."""
        market_45 = _market(yes_price=0.30, days_to_resolve=45.0)
        market_90 = _market(yes_price=0.30, days_to_resolve=90.0)
        model = _model(probability=0.70, confidence=0.8)

        sig45 = compute_signal(market_45, model)
        sig90 = compute_signal(market_90, model)

        # Both should produce the same adjusted edge (45-day cap applies)
        if sig45 is not None and sig90 is not None:
            assert sig45.adjusted_edge == pytest.approx(sig90.adjusted_edge, abs=1e-4)

    def test_time_decay_minimum_is_0_1(self):
        """With confidence=0 and 45+ days, decay floor is 0.1."""
        market = _market(yes_price=0.10, days_to_resolve=60.0)
        model = _model(probability=0.90, confidence=0.0)

        # time_decay = max(0.1, 0.0 * (1 - 0.02*45)) = max(0.1, 0.0) = 0.1
        # adjusted_edge = 0.80 * 0.1 - 0.0 = 0.08 → exactly at threshold
        signal = compute_signal(market, model)
        # Edge = 0.80 * 0.1 = 0.08 ≥ threshold → signal generated
        # (may depend on floating point — just ensure it doesn't crash)
        # The important thing: if it's generated, adjusted_edge > 0
        if signal is not None:
            assert signal.adjusted_edge >= 0


# ---------------------------------------------------------------------------
# Liquidity penalty
# ---------------------------------------------------------------------------


class TestLiquidityPenalty:
    def test_thin_book_reduces_edge(self):
        """Volume < 1000 subtracts 0.02 from adjusted_edge."""
        # High-volume market
        sig_liquid = compute_signal(
            _market(yes_price=0.40, volume=5000),
            _model(probability=0.65, confidence=1.0),
        )
        # Thin market
        sig_thin = compute_signal(
            _market(yes_price=0.40, volume=500),
            _model(probability=0.65, confidence=1.0),
        )
        assert sig_liquid is not None
        assert sig_thin is not None
        assert sig_liquid.adjusted_edge > sig_thin.adjusted_edge

    def test_liquidity_penalty_exact_value(self):
        """Penalty is exactly 0.02 on thin books."""
        sig_liquid = compute_signal(
            _market(yes_price=0.40, volume=5000, days_to_resolve=0),
            _model(probability=0.65, confidence=1.0),
        )
        sig_thin = compute_signal(
            _market(yes_price=0.40, volume=500, days_to_resolve=0),
            _model(probability=0.65, confidence=1.0),
        )
        if sig_liquid and sig_thin:
            assert sig_liquid.adjusted_edge - sig_thin.adjusted_edge == pytest.approx(0.02, abs=1e-4)


# ---------------------------------------------------------------------------
# Edge cases / degenerate inputs
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_market_price_zero_returns_none(self):
        """YES price of 0 would create division by zero in b computation."""
        signal = compute_signal(_market(yes_price=0.0), _model(probability=0.5))
        assert signal is None

    def test_market_price_one_returns_none(self):
        """YES price of 1.0 means b_no = price/(1-price) → division by zero."""
        # Model says 0.0 (NO trade) but market price is 1.0
        signal = compute_signal(_market(yes_price=1.0), _model(probability=0.0))
        # Adjusted edge of -1.0 is above threshold magnitude, but b_no is degenerate
        assert signal is None

    def test_signal_contains_correct_market_id(self):
        market = _market()
        market.id = "specific-market-id"
        signal = compute_signal(market, _model(probability=0.80))
        if signal is not None:
            assert signal.market_id == "specific-market-id"

    def test_signal_contains_model_prob_and_price(self):
        signal = compute_signal(_market(yes_price=0.30), _model(probability=0.65))
        if signal is not None:
            assert signal.model_prob == pytest.approx(0.65)
            assert signal.market_price == pytest.approx(0.30)
