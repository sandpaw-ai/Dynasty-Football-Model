"""Position-specific and years-pro weighting hooks for composite scoring.

This module centralizes the *modifier* lookups the scorer applies on top of
``Source.default_weight``. Three knobs:

1. **Position modifier** — per-(source_slug, position) multiplier. Lets us
   say "RAS counts 1.5× at WR but only 0.3× at QB". Defaults to 1.0 when
   not specified.

2. **Years-pro decay** — for sources whose signal is fundamentally a *pre-NFL*
   or *rookie-only* read (draft capital, RAS, college breakouts), decay the
   weight as the player ages out of rookie status. For market sources, an
   *inverse* curve: they're trailing indicators for rookies but reliable
   for veterans.

3. **Position-specific track record** — when a backtested source-track-record
   row exists for a specific position, prefer it over the overall (position-
   None) row. Lets us say "FantasyPros ECR is great at WR but mediocre at
   TE" without having to hand-tune.

Keeping this in its own module means the scoring pipeline stays focused on
flow control and the weighting policy is editable in one place.

Reference: ``docs/RESEARCH-sources.md`` §4 ("Suggested weighting + track-
record multiplier"), and ``docs/CHANGELOG-model.md`` § v0.7.0.
"""
from __future__ import annotations
from typing import Optional


# ---------------------------------------------------------------------------
# 1. Position-specific source weights.
# ---------------------------------------------------------------------------

# Map of (source_slug, position) → multiplier. Missing entry → 1.0.
# Positions match the canonical NFL skill positions: QB, RB, WR, TE.
POSITION_MODIFIERS: dict[tuple[str, str], float] = {
    # Athleticism: critical for WR/TE/RB, near-irrelevant for QB.
    ("ras", "WR"): 1.5,
    ("ras", "TE"): 1.5,
    ("ras", "RB"): 1.2,
    ("ras", "QB"): 0.3,

    # College breakout / dominator: alpha-WR archetype is the highest-signal
    # use case. Solid at TE, decent at RB, weak at QB.
    ("cfbd_breakouts", "WR"): 1.5,
    ("cfbd_breakouts", "TE"): 1.3,
    ("cfbd_breakouts", "RB"): 1.0,
    ("cfbd_breakouts", "QB"): 0.4,

    # NFL Draft capital: matters at every position but especially QB (where
    # the team's draft-capital commitment correlates with the opportunity
    # they'll get) and RB (where opportunity = draft capital → touches).
    ("nfl_draft_capital", "QB"): 1.2,
    ("nfl_draft_capital", "RB"): 1.1,
    ("nfl_draft_capital", "WR"): 1.0,
    ("nfl_draft_capital", "TE"): 1.0,

    # Market sources: no position tilt by default (they already aggregate
    # signal across positions). Adjustments can be added via backtest data.
}


def position_modifier(slug: str, position: Optional[str]) -> float:
    """Lookup the position-specific multiplier for a source. Default 1.0."""
    if not position:
        return 1.0
    return POSITION_MODIFIERS.get((slug, position.upper()), 1.0)


# ---------------------------------------------------------------------------
# 2. Years-pro decay.
# ---------------------------------------------------------------------------

# Sources whose signal is *pre-NFL* and decays with experience.
ROOKIE_SIGNAL_SOURCES = {
    "nfl_draft_capital",
    "ras",
    "cfbd_breakouts",
}

# Sources that *lag* for rookies — market values are trailing indicators in
# the rookie cohort. Re-weights downward in rookie + year-2 seasons.
TRAILING_FOR_ROOKIES_SOURCES = {
    "fantasycalc",
    "ffc_adp",
    "dynastyprocess",  # ECR consensus
}


def years_pro_modifier(slug: str, years_pro: Optional[int]) -> float:
    """Multiplier reflecting how this source's signal ages.

    - Rookie-signal sources (draft capital, RAS, CFBD): 1.0 at year 0, decay
      0.2 per year, floor 0.3 at year 4+. Rationale: a 6th-year RB's RAS
      score is barely-relevant; their actual NFL production should dominate.
    - Trailing-for-rookies sources (FantasyCalc, FFC, ECR): 0.6 at year 0,
      0.8 at year 1, 1.0 at year 2+. Rationale: market values lag draft-class
      reality but catch up by Year 3.
    - Others: 1.0 (no decay).

    ``years_pro = None`` is treated as "established player" (return 1.0) so
    veterans with missing draft_year still score cleanly.
    """
    if years_pro is None:
        return 1.0

    yrs = max(0, int(years_pro))

    if slug in ROOKIE_SIGNAL_SOURCES:
        return max(0.3, 1.0 - 0.2 * yrs)

    if slug in TRAILING_FOR_ROOKIES_SOURCES:
        if yrs == 0:
            return 0.6
        if yrs == 1:
            return 0.8
        return 1.0

    return 1.0


# ---------------------------------------------------------------------------
# 3. Position-specific track-record selector.
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
