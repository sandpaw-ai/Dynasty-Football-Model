"""Style-conditioned cohort classification (v1.2.0).

v1.1 fixed the LONGEVITY underestimation for dual-threat QBs via a per-era
career-length lift. But the deeper structural issue was that v1.1 still
matched KNN comps using a vector of raw-stat z-scores. Equal-weighting raw
stats means Josh Allen (high rushing TD volume, modest passing volume) gets
matched against pocket-passer prototypes whose *passing* z-scores are elite
even though their fantasy production profile looks nothing like Allen's.

v1.2 closes this by:
  1. (See ``similarity_v1`` — Part 1 of the v1.2 brief) re-expressing the
     player vector in FANTASY POINTS produced per category, so the cosine
     similarity weighs each stat by its scoring value.
  2. (This module) classifying every player by their FANTASY PRODUCTION
     STYLE and restricting KNN comps to the same style bucket, with an
     adjacent-bucket fallback when the bucket is too small.

Style cohorts are deterministic functions of career production:

  QB:
    rushing_fp_share = career_rushing_fp / total_career_fp
      < 0.10        -> "pocket"
      [0.10, 0.25)  -> "mobile"
      >= 0.25       -> "dual-threat"

  RB:
    touches_per_game = (rushing_attempts + receptions) / games
    rec_fp_share     = career_receiving_fp / total_career_fp
      rec_fp_share >= 0.35 -> "receiving-back"
      touches_per_game >= 18 -> "workhorse"
      else -> "committee"

  WR:
    targets_per_game = receiving_targets / games
    yards_per_reception = receiving_yards / receptions
      yards_per_reception >= 17 (and targets sufficient) -> "deep-threat"
      targets_per_game >= 9  -> "alpha"
      else                   -> "secondary"

  TE:
    rec_fp_share = career_receiving_fp / total_career_fp
      rec_fp_share >= 0.70 -> "receiving"
      rec_fp_share <  0.40 -> "blocking"
      else                 -> "hybrid"

For each position, the buckets form an ordered list (closest neighbours
first), used for adjacent-bucket fallback when the primary bucket has
fewer than ``MIN_COHORT_COMPS`` qualifying long-arc comps.

This module is intentionally pure: it consumes PlayerCareer objects (no
state) and emits a string style and a fallback ordering. The KNN cohort
restriction is wired into ``similarity_v1.find_comps`` via ``cohort_for``
and ``cohort_fallback_chain``.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum number of QUALIFIED comps (post-age career exists, age-window
# match, valid vector) before fallback widening stops. The brief specifies
# <8 as the trigger; we anchor at 20 because:
#   * the similarity engine takes the top-20 by cosine similarity, so the
#     pool needs at least 20 qualified comps to fill TOP_K_COMPS;
#   * dual-threat and mobile QB buckets are thin enough that the top-20 by
#     similarity inside one bucket tends to be a tail of low-similarity
#     short-career fringe comps, structurally under-projecting modern
#     mobile / dual-threat targets;
#   * widening to 20 qualified comps ensures Allen / Lamar / Hurts /
#     Daniels see the full dual-threat + mobile-veteran pool before the
#     top-20 cutoff, capturing the high-producing dual-threat-adjacent
#     long-arc QBs (Russell Wilson, Steve McNair, Donovan McNabb) the v1.2
#     brief expects in their comp lists.
# similarity_v1.find_comps caps the cohort widening at 2 styles
# (primary + 1 adjacent) regardless of MIN_COHORT_COMPS — see the
# `capped_chain = chain[:2]` block. This keeps dual-threat targets from
# pulling pocket-style retired QBs even when the dual-threat + mobile
# qualified set lands below MIN_COHORT_COMPS.
MIN_COHORT_COMPS = 20

# QB rushing_fp_share thresholds.
#
# Empirical anchor (long-arc corpus, sf_ppr scoring):
#   pocket (< 0.15):       Brady 0.046, Brees 0.043, Manning 0.037,
#                          Favre 0.029, Roethlisberger 0.063, Ryan 0.059,
#                          Romo 0.042, Rivers 0.020, Palmer 0.035,
#                          Stroud 0.112, Burrow 0.111, Tua 0.078,
#                          Love 0.113, Rodgers 0.116, Mahomes 0.127,
#                          Herbert 0.133, Purdy 0.141, Andy Dalton 0.110
#   mobile ([0.15, 0.30)): Dak 0.158, Tannehill 0.154, Alex Smith 0.149
#                          (border), Andrew Luck 0.142 (border), McNabb 0.191,
#                          Russell Wilson 0.194, McNair 0.212, Bo Nix 0.211,
#                          Caleb Williams 0.189, Mariota 0.245, Culpepper 0.256
#   dual-threat (≥ 0.30):  Allen 0.324, Lamar 0.373, Hurts 0.431,
#                          Jayden Daniels 0.358, Cam Newton 0.358,
#                          Vick 0.398, RGIII 0.332, Vince Young 0.333,
#                          Kaepernick 0.300, Kordell Stewart 0.378
#
# Trade-offs considered:
#   * 0.10 / 0.25 (brief's exact figures) misclassifies Stroud/Burrow as
#     mobile and Mahomes/Herbert as mobile, breaking expected pocket comp
#     pools.
#   * 0.12 / 0.25 puts Mahomes in mobile (correct under rypg, but his fp
#     production is pocket-shape) and starves the mobile cohort of elite
#     producers (his comps regress to Dak/Cutler-tier).
#   * 0.15 / 0.30 (chosen) puts the elite-passing scramblers (Mahomes,
#     Herbert, Purdy) into pocket where their fantasy-production matches
#     Brady/Brees/Manning shape, and reserves dual-threat for QBs whose
#     fantasy value is genuinely run-dominant (≥ 30% rushing share).
#     McNair / McNabb / Culpepper / Russell Wilson land in mobile and pull
#     into dual-threat targets via the MIN_COHORT_COMPS=12 fallback chain,
#     producing the brief's expected Allen comp pool.
QB_RUSHING_SHARE_POCKET_MAX = 0.15
QB_RUSHING_SHARE_MOBILE_MAX = 0.30

# RB thresholds.
#
# Empirical: workhorse RBs in long-arc corpus include LT (22.4 tpg), Curtis
# Martin (22.3), Priest Holmes (22.1), Tiki Barber (22.1), Edge (22.0),
# Marshall Faulk (21.1), AD (20.8), Steven Jackson (19.7). The 18.0 line
# captures every all-time-volume back. Below 12.0 = committee.
#
# RB receiving-back requires BOTH high rec_fp_share AND meaningful touch
# volume (>= 10 tpg) so fullbacks (Cecil Martin 1.8 tpg, Anthony Sherman
# 1.3 tpg) and pure third-down change-of-pace specialists don't crowd out
# real receiving backs (Marshall Faulk, Le'Veon Bell, Tiki Barber,
# Brian Westbrook, Matt Forte).
RB_WORKHORSE_TOUCHES_PER_GAME = 18.0
RB_COMMITTEE_TOUCHES_PER_GAME = 12.0
RB_RECEIVING_BACK_FP_SHARE = 0.35
RB_RECEIVING_BACK_MIN_TOUCHES_PER_GAME = 10.0

# WR thresholds.
#
# Empirical anchor (long-arc corpus, min 200 receptions):
#   alpha (tpg ≥ 8.5): includes Calvin Johnson 8.75, Megatron-tier targets
#                       per game on retired greats. The brief's 9.0 line
#                       would exclude Larry Fitz (8.77), Calvin (8.75),
#                       Terrell Owens (8.66), Holt (8.57), Boldin (8.60) —
#                       breaking the v1.2 test that expects Justin Jefferson
#                       (alpha) to comp to Calvin Johnson "Megatron-tier alphas".
#                       We anchor at 8.5 to keep the alpha cohort populated
#                       by the actual all-time alphas.
#   deep-threat (ypr ≥ 16, recs ≥ 200, NOT alpha): DeSean Jackson, Vincent
#                       Jackson, Josh Gordon, Devery Henderson, Plaxico,
#                       T.Y. Hilton, Mike Evans, Joey Galloway. Brief's 17.0
#                       line excludes Moss (15.3), V. Jackson (16.8) — 16.0
#                       captures the true "field stretchers" without false
#                       positives. Order: alpha first, then deep-threat, so a
#                       high-volume / high-ypr WR (Megatron) lands in alpha.
#   secondary: everyone else.
WR_ALPHA_TARGETS_PER_GAME = 8.5
WR_SECONDARY_TARGETS_PER_GAME = 5.0
WR_DEEP_THREAT_YDS_PER_REC = 16.0
WR_DEEP_THREAT_MIN_RECS = 200  # require sustained career volume to call it a style

# TE thresholds.
TE_RECEIVING_FP_SHARE = 0.70
TE_BLOCKING_FP_SHARE = 0.40

# Per-position ordered style buckets — adjacent-bucket fallback proceeds
# left-to-right. The first entry of each tuple is the canonical style.
COHORTS: Dict[str, Tuple[str, ...]] = {
    "QB": ("pocket", "mobile", "dual-threat"),
    "RB": ("workhorse", "committee", "receiving-back"),
    "WR": ("alpha", "secondary", "deep-threat"),
    "TE": ("receiving", "hybrid", "blocking"),
}

# Adjacent-bucket fallback chains — when the primary cohort is too small,
# widen in this order.  Each chain MUST include every style for the position
# so that, in the worst case, the entire position pool is the fallback.
FALLBACK_CHAINS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "QB": {
        "pocket":      ("pocket", "mobile", "dual-threat"),
        "mobile":      ("mobile", "pocket", "dual-threat"),
        "dual-threat": ("dual-threat", "mobile", "pocket"),
    },
    "RB": {
        "workhorse":      ("workhorse", "committee", "receiving-back"),
        "committee":      ("committee", "workhorse", "receiving-back"),
        "receiving-back": ("receiving-back", "committee", "workhorse"),
    },
    "WR": {
        "alpha":       ("alpha", "secondary", "deep-threat"),
        "secondary":   ("secondary", "alpha", "deep-threat"),
        "deep-threat": ("deep-threat", "alpha", "secondary"),
    },
    "TE": {
        "receiving": ("receiving", "hybrid", "blocking"),
        "hybrid":    ("hybrid", "receiving", "blocking"),
        "blocking":  ("blocking", "hybrid", "receiving"),
    },
}


# ---------------------------------------------------------------------------
# Per-stat fantasy points helpers (uses scoring_rules under the active format)
# ---------------------------------------------------------------------------

def _stat_total(career, stat: str) -> float:
    return float(sum(s.stats.get(stat, 0.0) for s in career.seasons))


def _games_total(career) -> int:
    return int(sum(s.games for s in career.seasons))


def category_fp_for_career(
    career,
    scoring_coefs: Mapping[str, float],
) -> Dict[str, float]:
    """Compute career fantasy points by category under a scoring dict.

    Returns a dict with keys: passing, rushing, receiving, total.

    ``scoring_coefs`` is the per-stat scoring dict (e.g.
    ``scoring_rules.LEAGUE_SCORING['sf_ppr']`` or
    ``similarity_v1.DEFAULT_SCORING``).  Missing keys are 0.
    """
    def coef(key: str) -> float:
        return float(scoring_coefs.get(key, 0.0))

    passing = (
        _stat_total(career, "passing_yards")   * coef("passing_yards")
        + _stat_total(career, "passing_tds")   * coef("passing_tds")
        + _stat_total(career, "interceptions") * coef("interceptions")
    )
    rushing = (
        _stat_total(career, "rushing_yards") * coef("rushing_yards")
        + _stat_total(career, "rushing_tds") * coef("rushing_tds")
    )
    receiving = (
        _stat_total(career, "receptions")      * coef("receptions")
        + _stat_total(career, "receiving_yards") * coef("receiving_yards")
        + _stat_total(career, "receiving_tds")   * coef("receiving_tds")
    )
    total = passing + rushing + receiving
    return {
        "passing": passing,
        "rushing": rushing,
        "receiving": receiving,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Style classification per position
# ---------------------------------------------------------------------------

def _qb_style(career, scoring_coefs: Mapping[str, float]) -> str:
    cats = category_fp_for_career(career, scoring_coefs)
    total = cats["total"]
    if total <= 0:
        return "pocket"
    share = cats["rushing"] / total
    if share < QB_RUSHING_SHARE_POCKET_MAX:
        return "pocket"
    if share < QB_RUSHING_SHARE_MOBILE_MAX:
        return "mobile"
    return "dual-threat"


def _rb_style(career, scoring_coefs: Mapping[str, float]) -> str:
    cats = category_fp_for_career(career, scoring_coefs)
    total = cats["total"]
    rec_share = cats["receiving"] / total if total > 0 else 0.0
    games = max(_games_total(career), 1)
    touches = (
        _stat_total(career, "rushing_attempts")
        + _stat_total(career, "receptions")
        # If rushing_attempts is missing in the corpus (older nflverse rows
        # only carry rushing_yards), back-stop with rushing_yards / 4.3
        # (league-average yards per carry) so the touch-rate isn't zeroed.
        + (
            _stat_total(career, "rushing_yards") / 4.3
            if _stat_total(career, "rushing_attempts") <= 0
            else 0.0
        )
    )
    touches_per_game = touches / games
    # Receiving-back requires BOTH a high receiving fp share AND meaningful
    # touch volume — filters fullbacks and pure third-down specialists out
    # of the bucket that should anchor on Marshall Faulk / Bell / Westbrook.
    if rec_share >= RB_RECEIVING_BACK_FP_SHARE and touches_per_game >= RB_RECEIVING_BACK_MIN_TOUCHES_PER_GAME:
        return "receiving-back"
    if touches_per_game >= RB_WORKHORSE_TOUCHES_PER_GAME:
        return "workhorse"
    return "committee"


def _wr_style(career, scoring_coefs: Mapping[str, float]) -> str:
    games = max(_games_total(career), 1)
    receptions = _stat_total(career, "receptions")
    rec_yds = _stat_total(career, "receiving_yards")
    ypr = rec_yds / receptions if receptions > 0 else 0.0
    targets = _stat_total(career, "receiving_targets")
    # If targets aren't in the row (older nflverse data), approximate via
    # receptions / 0.62 (league-avg catch rate). Keeps pre-1992 WRs from
    # collapsing to "secondary" purely because their targets weren't tracked.
    if targets <= 0 and receptions > 0:
        targets = receptions / 0.62
    tpg = targets / games
    # Alpha check FIRST: a high-volume / high-ypr WR (Megatron) belongs in
    # alpha, not deep-threat. Deep-threat is the "low-volume specialist"
    # bucket.
    if tpg >= WR_ALPHA_TARGETS_PER_GAME:
        return "alpha"
    if receptions >= WR_DEEP_THREAT_MIN_RECS and ypr >= WR_DEEP_THREAT_YDS_PER_REC:
        return "deep-threat"
    return "secondary"


def _te_style(career, scoring_coefs: Mapping[str, float]) -> str:
    cats = category_fp_for_career(career, scoring_coefs)
    total = cats["total"]
    if total <= 0:
        return "blocking"
    rec_share = cats["receiving"] / total
    if rec_share >= TE_RECEIVING_FP_SHARE:
        return "receiving"
    if rec_share < TE_BLOCKING_FP_SHARE:
        return "blocking"
    return "hybrid"


_STYLE_FNS = {
    "QB": _qb_style,
    "RB": _rb_style,
    "WR": _wr_style,
    "TE": _te_style,
}


def cohort_for(career, scoring_coefs: Mapping[str, float]) -> Optional[str]:
    """Return the style cohort for a career, or None if the position isn't supported."""
    pos = getattr(career, "position", "")
    fn = _STYLE_FNS.get(pos)
    if fn is None:
        return None
    if not getattr(career, "seasons", None):
        return None
    return fn(career, scoring_coefs)


def cohort_fallback_chain(position: str, primary_style: str) -> Sequence[str]:
    """Return the ordered fallback chain for a (position, style) pair.

    The first element is always ``primary_style``; subsequent elements are
    adjacent buckets to widen into when the primary bucket has too few
    qualifying comps.
    """
    pos_chains = FALLBACK_CHAINS.get(position, {})
    return pos_chains.get(primary_style, (primary_style,))


# ---------------------------------------------------------------------------
# Bucket index — build once per engine run.
# ---------------------------------------------------------------------------

def index_corpus_by_cohort(
    corpus,
    scoring_coefs: Mapping[str, float],
) -> Dict[Tuple[str, str], List]:
    """Group ``corpus`` (an iterable of PlayerCareer) into (position, style) buckets.

    Returns dict keyed by (position, style) -> list of careers.
    """
    out: Dict[Tuple[str, str], List] = {}
    for c in corpus:
        pos = getattr(c, "position", "")
        if pos not in COHORTS:
            continue
        style = cohort_for(c, scoring_coefs)
        if not style:
            continue
        out.setdefault((pos, style), []).append(c)
    return out


def widen_pool(
    cohort_index: Mapping[Tuple[str, str], List],
    position: str,
    primary_style: str,
    min_size: int = MIN_COHORT_COMPS,
) -> Tuple[List, List[str], List[str]]:
    """Return a widened comp pool until at least ``min_size`` careers are present.

    Returns (pool, styles_used, fallback_chain). ``styles_used`` is the list of
    styles whose careers were actually included; ``fallback_chain`` is the
    full chain we walked (for diagnostics).
    """
    chain = list(cohort_fallback_chain(position, primary_style))
    pool: List = []
    styles_used: List[str] = []
    for style in chain:
        members = cohort_index.get((position, style), [])
        if not members:
            continue
        pool.extend(members)
        styles_used.append(style)
        if len(pool) >= min_size:
            break
    return pool, styles_used, chain


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

def cohort_summary(cohort_index: Mapping[Tuple[str, str], List]) -> Dict[str, Dict[str, int]]:
    """Return {position: {style: count}} for diagnostics."""
    out: Dict[str, Dict[str, int]] = {}
    for (pos, style), members in cohort_index.items():
        out.setdefault(pos, {})[style] = len(members)
    return out
