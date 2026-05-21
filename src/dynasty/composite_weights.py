"""composite_weights — removed in v1.0.

The v0.x composite weighted multiple sources with per-source multipliers and
per-position overrides. v1.0 runs a single engine, so no weighting is needed.

This stub preserves the public API surface for any caller still importing it.
"""
from __future__ import annotations
from typing import Optional


def composite_weight_multiplier(*args, **kwargs) -> float:
    """Always return 1.0 — composite weighting removed in v1.0."""
    return 1.0


def elite_proven_config() -> dict:
    """No-op stub — elite-proven calibration was a v0.18 hack."""
    return {"enabled": False}
