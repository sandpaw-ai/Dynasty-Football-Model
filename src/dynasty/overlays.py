"""overlays — removed in v1.0.

The v0.x RAS/SRS correlation overlay system folded into v1.0's format
overlay engine (``dynasty.engine.format_overlay``). This module stays as
a stub for compatibility.
"""
from __future__ import annotations
from pathlib import Path

CORRELATION_TABLE_PATH = Path("data/overlays/correlation_table.json")


def compute_correlations(*args, **kwargs) -> dict:
    return {}
