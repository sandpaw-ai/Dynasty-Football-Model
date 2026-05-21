"""Per-format fantasy scoring rules — re-score raw stat lines.

The composite scorer in ``scoring.py`` blends source rankings; this
module is the lower-level *fantasy scoring engine* that converts a raw
NFL stat line (passing yards / TDs / interceptions / rush stats /
receiving stats / fumbles) into a fantasy-point total under a specific
league_format's scoring rules.

The similarity engine consumes this to re-score historical comp
seasons through the active format's rules. A 2010 Peyton Manning
season earned its raw fantasy points under whatever scoring its
contemporaneous league used; when we project a modern SF league we
need to re-score that season under sf_ppr's rules so the projection
matches the league actually being played.

Coverage scope (v0.15.0):
  * sf_ppr            — 4pt pass TD, 1 PPR, dynasty-default SF.
  * 1qb_ppr           — same as sf_ppr (same per-stat-line scoring;
                        format only differs in roster / replacement
                        baseline, which is handled by VORP, not here).
  * sf_te_premium     — sf_ppr + 0.5 bonus per reception for TEs.
  * sf_ppr_redraft    — same per-stat-line as sf_ppr (used only for
                        ADP labelling on some market sources).

If the raw row is missing a stat field, it's treated as 0 — this is
safe for older seasons where some columns weren't tracked (e.g.
targets pre-1992).
"""
from __future__ import annotations

from typing import Mapping, Optional


def _f(v) -> float:
    try:
        if v in (None, "", "NA"):
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Per-format scoring coefficients.
# Each format maps a stat name to its points-per-unit value.
# Stat names match the nflverse / PFR weekly+season schema so a raw row
# can be re-scored field-by-field.
# ---------------------------------------------------------------------------
#
# Notes:
#   * Receptions reward (PPR=1.0, Half=0.5, Standard=0.0).
#   * Pass TD is 4 in dynasty-default SF. Some older PPR-redraft leagues
#     use 6 — we model sf_ppr_redraft at 4 to match the dynasty trend
#     sources that ride alongside the real dynasty pricing (FFC).
#   * Sacks taken / sack yards lost are NOT penalized in any standard
#     fantasy league we support. (Sack-yards-lost is a real stat in
#     the row but goes against the QB's rushing yards calc; we exclude
#     it from scoring.)
#   * 2pt conversions: 2 pts each.
#   * Fumbles LOST: -2 (we only penalize lost fumbles; raw fumbles
#     that the player recovers don't cost points).

_BASE_SF_PPR = {
    "passing_yards":     0.04,
    "passing_tds":       4.0,
    "interceptions":     -2.0,
    "passing_2pt_conversions": 2.0,
    "rushing_yards":     0.10,
    "rushing_tds":       6.0,
    "rushing_2pt_conversions": 2.0,
    "rushing_fumbles_lost": -2.0,
    "receptions":        1.0,    # PPR
    "receiving_yards":   0.10,
    "receiving_tds":     6.0,
    "receiving_2pt_conversions": 2.0,
    "receiving_fumbles_lost": -2.0,
    "sack_fumbles_lost": -2.0,
    "special_teams_tds": 6.0,
}

LEAGUE_SCORING: dict[str, dict[str, float]] = {
    "sf_ppr": dict(_BASE_SF_PPR),
    "1qb_ppr": dict(_BASE_SF_PPR),
    "sf_ppr_redraft": dict(_BASE_SF_PPR),
    # TE-premium just adds a per-reception bonus that's applied at
    # score-time for TEs only — see score_season() position handling.
    "sf_te_premium": dict(_BASE_SF_PPR),
}

# TE premium reception bonus (additive, on top of the 1.0 PPR).
TE_PREMIUM_BONUS_PER_REC = 0.5


def score_season(
    raw: Mapping[str, object],
    league_format: str,
    position: Optional[str] = None,
) -> float:
    """Re-score a single player-season's raw stat line under a format's rules.

    ``raw`` is a dict from the nflverse / PFR corpus (any of the columns
    documented at the top of this file). ``position`` is needed for
    TE-premium rules; pass it as ``raw.get('position')`` if not known.

    Returns the fantasy-point total (float) for that season under the
    given format. Missing fields are treated as 0.

    This is intentionally pure (no DB / no state) so it's trivially
    composable: the similarity engine calls it once per comp season,
    and tests assert specific equivalences directly.
    """
    coefs = LEAGUE_SCORING.get(league_format)
    if not coefs:
        # Unknown format — fall back to sf_ppr defaults rather than
        # crashing the pipeline.
        coefs = LEAGUE_SCORING["sf_ppr"]

    total = 0.0
    for stat, mult in coefs.items():
        total += _f(raw.get(stat)) * mult

    # TE-premium reception bonus.
    if league_format == "sf_te_premium":
        pos = (position or raw.get("position") or "").upper()
        if pos == "TE":
            total += _f(raw.get("receptions")) * TE_PREMIUM_BONUS_PER_REC

    return total


def score_seasons(
    rows: list[Mapping[str, object]],
    league_format: str,
    position: Optional[str] = None,
) -> float:
    """Sum scored points across a list of season rows."""
    return sum(score_season(r, league_format, position) for r in rows)


# ---------------------------------------------------------------------------
# Sanity helpers used by tests + UI methodology page.
# ---------------------------------------------------------------------------

def formats_with_same_per_stat_rules(a: str, b: str) -> bool:
    """Return True iff two formats share identical per-stat coefficients.

    Useful for the projection layer: if sf_ppr and 1qb_ppr have identical
    per-stat scoring (they do), re-scoring comp seasons under both
    produces the same per-season points. The format difference shows up
    only in VORP / replacement baselines.
    """
    return LEAGUE_SCORING.get(a) == LEAGUE_SCORING.get(b)
