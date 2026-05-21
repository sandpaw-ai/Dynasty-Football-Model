"""scoring.py — composite scoring removed in v1.0.

v0.x computed a weighted composite of multiple source rankings here. v1.0
runs a single engine (``dynasty.engine.similarity_v1``) and does not need
this module.

The ``compute_composite_scores`` function is kept as a no-op so callers
still importing it (e.g., the CLI) don't crash; it returns 0 rows scored.
"""
from __future__ import annotations
from typing import Optional


def compute_composite_scores(league_format: str = "sf_ppr",
                             session=None,
                             **kwargs) -> int:
    """v1.0 stub. The launcher now calls ``engine.similarity_v1.run_engine``."""
    return 0
