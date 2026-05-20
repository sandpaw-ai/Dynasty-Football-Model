"""Weighting policy for composite scoring.

v0.10 redesign (Phil request 2026-05-20): weights are deterministic per-
source, driven only by backtested correlation with realized NFL fantasy
production. They do **not** vary per-player.

    effective_weight = default_weight * track_record_multiplier

The one allowed source of per-player variation is the position-specific
track record: if a backtest produced a SourceTrackRecord row for
``(source, position="WR")``, that row's correlation is used for WR
players; non-WR players fall back to the overall (``position=None``) row.

Removed in v0.10:
  * ``position_modifier(slug, pos)`` — hand-coded per-(source, position)
    overrides. Now any position tilt has to come from backtest data.
  * ``years_pro_modifier(slug, years_pro)`` — linear decay for
    rookie-signal sources + inverse curve for market sources. Removed
    because it caused the same source to display different weight values
    for different players in the breakdown JSON.

What remains:
  * ``ROOKIE_SIGNAL_SOURCES`` — the set of source slugs whose data is
    fundamentally pre-NFL. Used by ``scoring.py`` to filter out
    retired/no-longer-rostered players whose only rankings come from
    these sources (the "no consensus" pattern).
  * ``corr_to_multiplier()`` — the |Spearman ρ| → multiplier ladder.
  * ``select_track_record_multiplier()`` — picks the position-specific
    track record row if present, falls back to overall.

Reference: ``docs/CHANGELOG-model.md`` § v0.10.0.
"""
from __future__ import annotations
from typing import Optional


# ---------------------------------------------------------------------------
# Rookie-signal sources — used by scoring.py to filter out retired players
# whose ONLY rankings come from pre-NFL data. ("No consensus" pattern.)
# ---------------------------------------------------------------------------

ROOKIE_SIGNAL_SOURCES = {
    "nfl_draft_capital",
    "ras",
    "cfbd_breakouts",
}


# ---------------------------------------------------------------------------
# Position-specific track-record selector.
# ---------------------------------------------------------------------------

def select_track_record_multiplier(
    track_records_by_pos: dict[Optional[str], float],
    position: Optional[str],
) -> float:
    """Pick the position-specific multiplier when available, else fallback.

    ``track_records_by_pos`` is the per-source mapping
    ``{position_or_None: multiplier}``. Lookup order:

      1. exact position match
      2. position-None (overall) entry
      3. neutral (1.0)
    """
    if position:
        m = track_records_by_pos.get(position.upper())
        if m is not None:
            return m
    overall = track_records_by_pos.get(None)
    if overall is not None:
        return overall
    return 1.0


def corr_to_multiplier(corr: Optional[float]) -> float:
    """Convert |spearman_corr| into a multiplier.

    Tuned per research §4 (slightly tighter than the v0.2 cutoffs):

      |ρ| ≥ 0.35  → 1.6
      |ρ| ≥ 0.25  → 1.3
      |ρ| ≥ 0.15  → 1.0
      |ρ| <  0.15 → 0.5
      None        → 1.0 (neutral)
    """
    if corr is None:
        return 1.0
    a = abs(corr)
    if a >= 0.35: return 1.6
    if a >= 0.25: return 1.3
    if a >= 0.15: return 1.0
    return 0.5
