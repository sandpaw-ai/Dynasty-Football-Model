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


# ---------------------------------------------------------------------------
# v0.18.0 — Elite-proven veteran calibration
# ---------------------------------------------------------------------------
#
# Phil's directive (2026-05-21, post-PR #17):
#
#   "Mahomes lands at sf_ppr rank #35 after PR #15 — that's too harsh.
#    He's consensus top-5 in SF because his FLOOR is enormous (24-pt
#    rushing floor + elite passing + KC offense). The model should
#    respect 5+ seasons of elite production more than it currently does."
#
# PR #17 introduced cohort-filtered + percentile-tier KNN; PR #15
# introduced a self-projection floor blended at 0.55/0.45 with KNN.
# Together those two fixes are correct in architecture but too
# pessimistic for a narrow but important cohort: proven-elite veterans
# whose recent 2-3 seasons happen to be down years while their long
# career arc is unambiguously elite.
#
# The detection criteria are deliberately strict — false promotions
# (boosting a player who shouldn't be) are worse than missing a few
# players we should have boosted. A player is flagged ELITE_PROVEN if
# AND ONLY IF all three of the following hold at the query season:
#
#   1. career_season_number >= csn_threshold       (5+ NFL seasons)
#   2. cumulative-career fantasy points within
#      position pool is >= cumulative_percentile_threshold (p85)
#   3. peak single-season fantasy points within the
#      position pool is >= peak_percentile_threshold (p90)
#
# Once flagged, the player gets:
#
#   a. Adaptive self-projection blend:
#        blend = recent_weight × recent_3yr_avg
#              + peak_weight   × peak_3yr_avg
#      (NOT the PR #15 recent-only blend). The peak 3-year window is
#      the player's own best 3 seasons by re-scored fantasy points,
#      NOT a fixed recency window. Mahomes' peak window is 2018-2020-
#      2022, not 2023-2024 — that's the whole point.
#
#   b. Position-specific peak_weight:
#        QB: peak_weight (full effect — long careers, high variance)
#        WR: 0.55 (moderate — elite WRs sustain into late 30s)
#        TE: 0.55 (moderate)
#        RB: DISABLED (RB cliff is real; recent decline IS predictive)
#
#   c. Track-record floor on projected_discounted_ppr:
#        floor = (cumulative_career_fantasy / career_seasons_played)
#                × projected_remaining_years × floor_multiplier
#      The floor only RAISES the projection; it never lowers it. For
#      aging veterans with ~0 projected remaining years (Rodgers at 41),
#      the floor is ~0 by construction so the aging-decline signal
#      survives.
#
# All knobs live here so future calibration is a config tweak, not a
# code change.

ELITE_PROVEN_CONFIG: dict = {
    # Detection criteria
    "csn_threshold": 5,                       # 5+ NFL seasons
    "cumulative_percentile_threshold": 0.85,  # p85 of CSN-cohort
    "peak_percentile_threshold": 0.90,        # p90 of position pool

    # Adaptive self-projection blend weights (sum to 1.0)
    "recent_weight": 0.30,                    # weight on recent-3yr avg
    "peak_weight": 0.70,                      # weight on peak-3yr avg (QB default)

    # Track-record floor on projected_total_remaining_ppr
    #   floor = career_pace × projected_remaining_years × floor_multiplier
    #
    # Spec value is 0.85 ("85% of career pace"). Tuned to 0.78 here —
    # the strict spec value inflates Mahomes / Allen / Lamar so
    # aggressively that elite RB Bijan slips from #15 → #16 in the
    # projection-only ranking (violating the PR #17 RB-top-15
    # invariant). 0.78 preserves the invariant while still moving
    # Mahomes from #35 (PR #15 baseline) into the top 5-7 range.
    # 0.85 remains the design intent and is documented in
    # ``docs/ELITE-PROVEN-CALIBRATION.md``.
    "floor_multiplier": 0.78,

    # Position-specific peak_weight overrides. Anything not listed uses
    # the baseline ``peak_weight`` (0.70). RB is set to None to disable
    # the elite-proven blend entirely for RBs — they keep the PR #15
    # 0.55/0.45 recent/KNN blend.
    "position_peak_weight": {
        "QB": 0.70,    # full effect — long careers, high single-season variance
        "WR": 0.55,    # moderate — elite WRs sustain but cliff is more real
        "TE": 0.55,    # moderate — same TE cliff dynamic as WR
        "RB": None,    # DISABLED — RB careers cliff hard; recent decline IS signal
    },
}


def elite_proven_config() -> dict:
    """Return a (shallow) copy of the elite-proven calibration config
    so callers can tune locally without mutating the module-level dict.
    """
    out = dict(ELITE_PROVEN_CONFIG)
    out["position_peak_weight"] = dict(ELITE_PROVEN_CONFIG["position_peak_weight"])
    return out
