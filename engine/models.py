"""
engine/models.py — Canonical output type for the Weather Engine.

All modules that produce or consume weather probability data must import
ModelResult from here — never define their own equivalent.
"""
from dataclasses import dataclass, field


@dataclass
class ModelResult:
    """
    Probability output from the ensemble weather model.

    probability   — P(event occurs), range [0.0, 1.0]
    confidence    — Model agreement score, derived as:
                    max(0.0, min(1.0, 1.0 - (ensemble_std_dev / 0.5)))
                    0.0 = maximum disagreement (std_dev ≥ 0.5)
                    1.0 = full agreement (std_dev = 0.0)
    ci_low/high   — 90% confidence interval bounds on probability
    members_count — Number of ensemble members used in computation
    sources       — Data sources contributing to this result
    degraded_sources — Sources that failed and were excluded
    """
    probability: float
    confidence: float
    ci_low: float
    ci_high: float
    members_count: int
    sources: list[str]
    degraded_sources: list[str] = field(default_factory=list)
