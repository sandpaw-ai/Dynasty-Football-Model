"""Format-aware composite weight overrides.

The composite scorer in ``scoring.py`` weights each source by its
``default_weight × track_record_multiplier``. For most sources that's
fine — but in dynasty SF leagues, the value of QBs is fundamentally
different from 1QB leagues:

  * SF: must start a QB in the flex; ~24 startable QBs (12 teams × 2);
    the replacement-level QB is a startable-flex-week piece.
  * 1QB: only 12 startable QBs; ~30+ QBs are bench/streamers, replacement
    drops off less sharply because the next-best QB is closer to the
    cliff.

The similarity engine + positional VORP fix the *projection* side of
this (re-scored historical comps + replacement baseline per position).
This module adds a second knob: per-(format, position) MULTIPLIERS on
the source weights themselves, so that the *current-skill* signals
(``nfl_impact``) and the *career-arc* signal
(``similarity_career_arc``) both lift QBs harder in SF than in 1QB.

Phil's directive (2026-05-21):

  "Mahomes and Josh Allen for example are extremely valuable in a
   superflex league where you have to start 1 QB, and more often you
   are starting a QB in the superflex spot. Keep that in mind when
   developing the rankings."

Defaults documented inline below. All values intentionally live in a
single dict so they're discoverable, override-friendly, and pinnable
in tests.
"""
from __future__ import annotations

from typing import Optional


# Per-(league_format, position, source_slug) weight multiplier.
# When a key is missing we fall back to 1.0 (no change).
#
# Baselines (from v0.14.0):
#   similarity_career_arc.default_weight = 1.8
#   nfl_impact.default_weight            = 0.8
#
# In sf_ppr we want QBs to feel the SF premium through BOTH the
# similarity engine (longevity * SF replacement gap) and the
# current-skill signal (where the QB tier-cliff is steep). So:
#   sf_ppr × QB × similarity_career_arc  → 2.4 effective (1.8 × 1.333)
#   sf_ppr × QB × nfl_impact             → 1.0 effective (0.8 × 1.25)
#
# In 1qb_ppr we DE-emphasize QB career-arc weight vs SF, but we DON'T
# zero them out — top QBs are still scarce assets, just less so:
#   1qb_ppr × QB × similarity_career_arc → 1.4 effective (1.8 × 0.778)
#   1qb_ppr × QB × nfl_impact            → 0.8 effective (1.0)
#
# Non-QB positions are unaffected across formats (the VORP layer handles
# RB / WR / TE scarcity); we leave their multipliers at 1.0.

_COMPOSITE_WEIGHT_MULTIPLIERS: dict[tuple[str, str, str], float] = {
    # sf_ppr — in SF, QB current-skill (nfl_impact) is the dominant signal
    # because the similarity engine's KNN can't comp veteran starters
    # (age-28 Allen) to younger Brady/Manning career arcs. We lean on
    # current skill + market consensus to land top QBs at the top.
    #   similarity_career_arc:  1.8 × 1.333 = 2.4   (career-arc signal)
    #   nfl_impact:             0.8 × 3.125 = 2.5   (current-skill premium)
    #   fantasycalc:            0.6 × 2.0   = 1.2   (SF dynasty market — most accurate)
    ("sf_ppr", "QB", "similarity_career_arc"): 1.333,  # 1.8 → 2.4
    ("sf_ppr", "QB", "nfl_impact"):            2.5,    # 0.8 → 2.0  (recent perf only)
    ("sf_ppr", "QB", "fantasycalc"):           3.0,    # 0.6 → 1.8  (best SF market signal)
    ("sf_ppr", "QB", "dynastyprocess"):        4.0,    # 0.3 → 1.2  (aggregator, QB-tilted)
    ("sf_ppr", "QB", "brainy_ballers"):        2.0,    # 0.5 → 1.0
    ("sf_ppr", "QB", "nfl_draft_capital"):     2.0,    # rookies — lifts Maye/Williams

    # 1qb_ppr — pull QB weight back down so we don't over-rate QBs in 1QB.
    # We keep current-skill as a strong signal but de-emphasize all the
    # market-consensus QB-tilted sources because in 1QB those sources
    # over-rate the position vs the actual cap space they consume.
    ("1qb_ppr", "QB", "similarity_career_arc"): 0.778, # 1.8 → 1.4
    ("1qb_ppr", "QB", "nfl_impact"):            1.0,   # unchanged
    ("1qb_ppr", "QB", "fantasycalc"):           0.75,  # de-emphasize
    ("1qb_ppr", "QB", "dynastyprocess"):        0.75,
    ("1qb_ppr", "QB", "brainy_ballers"):        0.75,

    # sf_te_premium — same as sf_ppr for QB; TE gets a similar lift to
    # reflect the TE-premium reception bonus.
    ("sf_te_premium", "QB", "similarity_career_arc"): 1.333,
    ("sf_te_premium", "QB", "nfl_impact"):            3.125,
    ("sf_te_premium", "QB", "fantasycalc"):           2.0,
    ("sf_te_premium", "TE", "similarity_career_arc"): 1.111,  # 1.8 → 2.0
    ("sf_te_premium", "TE", "nfl_impact"):            1.125,  # 0.8 → 0.9
}


def composite_weight_multiplier(
    league_format: str,
    position: Optional[str],
    source_slug: str,
) -> float:
    """Look up the per-(format, position, source) multiplier on the
    source's default_weight. Returns 1.0 when no override exists.

    This is multiplicative on top of any ``track_record_multiplier``
    derived from backtesting — the two signals stack:

        effective_weight =
            default_weight
          * track_record_multiplier(source, position)
          * composite_weight_multiplier(format, position, source)
    """
    if not position:
        return 1.0
    key = (league_format, position.upper(), source_slug)
    return _COMPOSITE_WEIGHT_MULTIPLIERS.get(key, 1.0)


def explain_overrides() -> list[tuple[str, str, str, float]]:
    """Return all configured (format, position, source, multiplier)
    overrides for documentation / debug output.
    """
    return [
        (fmt, pos, slug, mult)
        for (fmt, pos, slug), mult in _COMPOSITE_WEIGHT_MULTIPLIERS.items()
    ]
