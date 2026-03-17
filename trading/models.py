"""
trading/models.py — Canonical type for Risk Manager output.

ApprovedSignal is the contract between the Risk Manager and the Trader.
Import from here only — never redefine this dataclass elsewhere.
"""
from dataclasses import dataclass

from market.models import Signal


@dataclass
class ApprovedSignal:
    """
    A signal that has passed all Risk Manager guardrails.

    signal     — The original Signal from the Signal Engine
    final_size — Dollar amount in USDC to trade
                 = min(bankroll * signal.raw_kelly_size, MAX_POSITION_USDC)
    mode       — "live" | "paper"
    """
    signal: Signal
    final_size: float
    mode: str
