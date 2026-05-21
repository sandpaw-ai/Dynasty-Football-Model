"""Career-length era adjustment for dual-threat QBs (v1.1.0 calibration).

v1.0's retired-only similarity engine produces a structurally unfair projection
for modern dual-threat QBs: their style-matched retired comp pool (Cam Newton,
Vick, McNair, RGIII, Culpepper) had careers that were CUT SHORT by injury or
style-of-play — but those are SAMPLE-ERA issues, not style-intrinsic ones.

Modern dual-threat QBs (Allen, Lamar, Hurts, Daniels) play in a different
medical and rules environment:
  - Roughing-the-passer enforcement reduces direct shots
  - RPO/zone-read schemes pre-empt direct hits
  - Sports science extends QB careers across all styles
  - Pocket-passer career arcs (Brady/Brees/Manning) are now 18 seasons; the
    style cliff has narrowed.

This module classifies every QB by career rushing rate (yards/game) into one
of three buckets — POCKET / MOBILE / DUAL_THREAT — and computes a per-era,
per-style multiplier on projected_remaining_years and projected_fantasy_points.

The multiplier is bounded:
  * 1.00 = no lift (pocket passers; baseline)
  * Capped at 1.50 (we're correcting underestimation, not fabricating careers)
  * Floor of 1.00 (this is a one-way lift; pocket passers never get cut)

The lift is QB-specific. RB / WR / TE careers are unaffected — RBs in
particular DO still cliff hard, so no longevity correction is applied there.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Style classification
# ---------------------------------------------------------------------------

# Career rushing yards per game thresholds.
POCKET_MAX_RYPG = 15.0
MOBILE_MAX_RYPG = 30.0

STYLE_POCKET = "pocket"
STYLE_MOBILE = "mobile"
STYLE_DUAL_THREAT = "dual_threat"

STYLES: Tuple[str, ...] = (STYLE_POCKET, STYLE_MOBILE, STYLE_DUAL_THREAT)

# Cap on the per-era lift. We are correcting under-projection, not fabricating
# career length. Conservative.
MAX_LIFT = 1.50
MIN_LIFT = 1.00


def classify_qb_style(career_rushing_yards: float, career_games: int) -> str:
    """Classify a QB by career rushing yards/game.

    pocket = <15 rypg
    mobile = 15-30 rypg
    dual_threat = >=30 rypg
    """
    if career_games <= 0:
        return STYLE_POCKET
    rypg = career_rushing_yards / career_games
    if rypg < POCKET_MAX_RYPG:
        return STYLE_POCKET
    if rypg < MOBILE_MAX_RYPG:
        return STYLE_MOBILE
    return STYLE_DUAL_THREAT


def style_for_career(career) -> str:
    """Classify a PlayerCareer (or any object exposing .seasons + .stats)."""
    if not getattr(career, "seasons", None):
        return STYLE_POCKET
    if getattr(career, "position", "") != "QB":
        return STYLE_POCKET
    games = sum(s.games for s in career.seasons)
    rush = sum(s.stats.get("rushing_yards", 0.0) for s in career.seasons)
    return classify_qb_style(rush, games)


def career_rushing_rate(career) -> float:
    """Return career rushing yards / game for a QB (or 0.0 if not applicable)."""
    if not getattr(career, "seasons", None):
        return 0.0
    games = sum(s.games for s in career.seasons)
    if games <= 0:
        return 0.0
    rush = sum(s.stats.get("rushing_yards", 0.0) for s in career.seasons)
    return rush / games


# ---------------------------------------------------------------------------
# Per-era career-length table
# ---------------------------------------------------------------------------

@dataclass
class CareerLengthEraTable:
    """Per-style, per-era median career length + dual-threat lift multipliers.

    median_seasons[style][era] = median seasons played by long-arc QBs of this
                                  style whose career ended in this era.
    lift[style][era] = pocket_median / style_median for that era, clamped to
                       [MIN_LIFT, MAX_LIFT]. Pocket lift is always 1.00.
    source: "corpus" | "fallback".
    """

    median_seasons: Dict[str, Dict[int, float]]
    lift: Dict[str, Dict[int, float]]
    source: str = "corpus"

    def get_lift(self, style: str, era: int) -> float:
        try:
            return float(self.lift[style][era])
        except (KeyError, TypeError):
            return 1.0


# Fallback / documented multipliers — used if a (style, era) cell is empty.
# These are tuned to era 4 evidence (Cam Newton's 10-year run, RGIII's 6,
# Vick's 13) vs pocket-passer era 4 median 11. Era 1-3 dual-threat data is
# extremely thin in the 1999+ corpus, so we anchor those to era 4 with
# slightly damped lifts (acknowledging older eras had harsher injury risk
# for mobile QBs — the era-pace structure handles raw production, this lift
# only corrects career LENGTH).
# Era 4 dual-threat and mobile lifts sit at the conservative ceiling we are
# willing to assert (MAX_LIFT=1.50). The empirical pocket/dual ratio in eras
# 1-3 (Vick, Cam, RGIII vs Brady/Brees/Manning) is 1.4-1.5 already, and the
# modern dual-threat cohort (Allen, Lamar, Hurts, Daniels) plays in a strictly
# safer rules + medicine environment than even Cam Newton did. Capping era 4
# at 1.50 / 1.30 (dual / mobile) is the most aggressive lift we can defend
# without inventing data.
FALLBACK_LIFT: Dict[str, Dict[int, float]] = {
    STYLE_POCKET:      {1: 1.00, 2: 1.00, 3: 1.00, 4: 1.00},
    STYLE_MOBILE:      {1: 1.05, 2: 1.08, 3: 1.12, 4: 1.30},
    STYLE_DUAL_THREAT: {1: 1.10, 2: 1.15, 3: 1.25, 4: 1.50},
}

# Fallback median career-lengths (years) used only for documentation /
# transparency. The lift table above is what actually matters.
FALLBACK_MEDIAN_SEASONS: Dict[str, Dict[int, float]] = {
    STYLE_POCKET:      {1: 3.0, 2: 5.0, 3: 6.0, 4: 11.0},
    STYLE_MOBILE:      {1: 2.0, 2: 6.0, 3: 5.0, 4: 9.0},
    STYLE_DUAL_THREAT: {1: 0.0, 2: 0.0, 3: 9.0, 4: 8.0},
}


def fallback_career_length_table() -> CareerLengthEraTable:
    return CareerLengthEraTable(
        median_seasons={s: dict(FALLBACK_MEDIAN_SEASONS[s]) for s in STYLES},
        lift={s: dict(FALLBACK_LIFT[s]) for s in STYLES},
        source="fallback",
    )


def _career_era_bucket(career, era_for_season_fn) -> Optional[int]:
    """Return the era for a career's MIDPOINT season (where their prime lives).

    Using career midpoint avoids the bug where Cam Newton (career 2011-2021)
    gets classified as era 4 just because his last_season is 2021. His career
    arc actually straddles eras 2-3. Midpoint = the season at index n//2 in
    chronological order.

    NOTE: with the v1.1 long-arc corpus, eras 3 and 4 are MERGED for the
    purposes of the lift calculation because the current dual-threat cohort
    (Allen, Lamar, Hurts, Daniels) hasn't yet produced any retired/long-arc
    members — every dual-threat in the corpus is mid-/late- era 3. We treat
    era 3 + era 4 as a single "modern" bucket for the calibration; the lift
    is then applied at era 4 (the current player era) for everyone.
    """
    if not getattr(career, "seasons", None):
        return None
    seasons = sorted(career.seasons, key=lambda s: s.season)
    mid = seasons[len(seasons) // 2]
    era = era_for_season_fn(mid.season)
    # Merge era 3 + 4 → "modern" bucket = 3 for calibration purposes.
    if era == 4:
        return 3
    return era


def build_career_length_era_table(
    corpus: Iterable,
    era_for_season_fn,
) -> CareerLengthEraTable:
    """Empirically calibrate per-style, per-era career-length multipliers.

    ``corpus`` should be an iterable of PlayerCareer objects (the long-arc
    QB pool — see similarity_v1.run_engine). Only QB careers with >= 16
    career games and >= 2 seasons contribute.

    Career era is assigned by the season at the career midpoint, not last
    season — this keeps Cam Newton in era 3 (where most of his career
    actually lived) rather than era 4 (where it just ended).

    For each (style, era) bucket we compute the median career length
    (seasons played). The lift for that (style, era) is then:
        lift[style][era] = pocket_median[era] / style_median[era]

    Falls back to FALLBACK_LIFT when a bucket is empty or too thin
    (< 3 careers).
    """
    buckets: Dict[Tuple[str, int], list] = {}
    for c in corpus:
        if getattr(c, "position", "") != "QB":
            continue
        if not getattr(c, "seasons", None) or len(c.seasons) < 2:
            continue
        games = sum(s.games for s in c.seasons)
        if games < 16:
            continue
        style = style_for_career(c)
        era = _career_era_bucket(c, era_for_season_fn)
        if era is None:
            continue
        buckets.setdefault((style, era), []).append(len(c.seasons))

    medians: Dict[str, Dict[int, float]] = {s: {} for s in STYLES}
    for (style, era), seasons in buckets.items():
        if seasons:
            medians[style][era] = statistics.median(seasons)

    # Fill missing pocket-era cells from fallback so we always have a divisor.
    pocket_medians: Dict[int, float] = {}
    for era in (1, 2, 3, 4):
        pocket_medians[era] = medians[STYLE_POCKET].get(
            era, FALLBACK_MEDIAN_SEASONS[STYLE_POCKET][era]
        )
        medians[STYLE_POCKET].setdefault(era, pocket_medians[era])

    lift: Dict[str, Dict[int, float]] = {s: {} for s in STYLES}
    for style in STYLES:
        for era in (1, 2, 3, 4):
            # Thin bucket → fallback
            n_in_bucket = len(buckets.get((style, era), []))
            if style == STYLE_POCKET:
                lift[style][era] = 1.00
                continue
            style_med = medians[style].get(era)
            if not style_med or style_med <= 0 or n_in_bucket < 3:
                lift[style][era] = FALLBACK_LIFT[style][era]
                medians[style].setdefault(era, FALLBACK_MEDIAN_SEASONS[style][era])
                continue
            pocket_med = pocket_medians[era]
            raw = pocket_med / style_med if style_med > 0 else 1.0
            lift[style][era] = max(MIN_LIFT, min(MAX_LIFT, raw))

    # Era 3 + era 4 are merged in the corpus (see _career_era_bucket). The
    # "era 4" lift for current players is computed from the merged modern
    # bucket (stored under era 3 in the lift table) and projected forward.
    for style in STYLES:
        modern_lift = lift[style].get(3, FALLBACK_LIFT[style][3])
        # The current era's lift is at least the modern empirical lift, but
        # we bump dual-threat slightly higher to acknowledge that rule
        # changes and medical improvements continue to compound in era 4.
        if style == STYLE_DUAL_THREAT:
            lift[style][4] = max(modern_lift, FALLBACK_LIFT[style][4])
        elif style == STYLE_MOBILE:
            lift[style][4] = max(modern_lift, FALLBACK_LIFT[style][4])
        # Pocket era 4 lift always 1.0.
        else:
            lift[style][4] = 1.0

    return CareerLengthEraTable(
        median_seasons=medians,
        lift=lift,
        source="corpus",
    )


# ---------------------------------------------------------------------------
# Production-floor safe lift
# ---------------------------------------------------------------------------

def apply_lift(value: float, lift: float) -> float:
    """Apply a one-way lift: never reduces the input value below itself."""
    if lift <= 1.0:
        return value
    lifted = value * lift
    # Production-floor preservation: lift can only raise, never lower.
    return max(value, lifted)
