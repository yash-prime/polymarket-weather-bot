"""
market/models.py — Canonical types for market data and trade signals.

All modules that produce or consume market/signal data must import
Market and Signal from here.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Market:
    """
    A Polymarket weather prediction market as fetched from the Gamma API.

    id             — Gamma/Polymarket market ID
    question       — Raw question string from Gamma API
    yes_price      — Current YES price (0.0–1.0)
    end_date       — Resolution date/time (UTC)
    volume         — Total USDC volume
    parsed         — LLM-parsed structured JSON (None = not yet parsed)
    parse_status   — "success" | "regex_fallback" | "failed" | "pending"
    resolution_risk — "LOW" | "MEDIUM" | "HIGH" | None
    """
    id: str
    question: str
    yes_price: float
    end_date: datetime
    volume: float
    parsed: dict | None
    parse_status: str
    resolution_risk: str | None = None


@dataclass
class Signal:
    """
    A trade signal produced by the Signal Engine.

    market_id      — Market this signal is for
    direction      — "YES" | "NO"
    raw_kelly_size — Kelly fraction of bankroll (before risk clamping, 0.0–1.0)
                     Risk Manager multiplies this by the bankroll and applies $cap.
    adjusted_edge  — Final edge after time-decay and liquidity penalty
    model_prob     — Probability from ModelResult
    market_price   — Current YES price from Market
    """
    market_id: str
    direction: str
    raw_kelly_size: float
    adjusted_edge: float
    model_prob: float
    market_price: float
