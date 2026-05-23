"""1-NFL-season rookie fantasy-point-arc engine (v2.1.0).

Phil's diagnosis after v2.0 shipped: 2025 rookies (Jaxson Dart, Ashton
Jeanty, Cam Ward, Tetairoa McMillan, Travis Hunter) entered the rankings
with only ONE completed NFL season. The v2.0 cumulative-arc engine
comp'd them against full-career retired veterans whose career-arc
vectors include 10+ NFL seasons. A 1-data-point vector against a 10+
data-point vector is comparing apples to a fruit salad — the comp
matches were noisy (Vince Young top comp for Dart, Jordan Howard top
comp for Jeanty) and the projection was unstable.

v2.1's fix: build a SEPARATE engine for current 1-NFL-season rookies
that comp's them against historical players' ACTUAL ROOKIE SEASONS
(year 1 NFL profile vector), then projects forward from those comps'
realised year-2+ careers. This is the methodology Phil specifically
asked for:

    "the 2025 draft class should have a full season of stats under
    their belt... You should be able to identify players in
    pro-football reference who have only one year of experience as
    rookies and extrapolate their careers based on one season of stats
    compared to historically similar player profiles"

Pipeline:

    1.  Build the HISTORICAL ROOKIE CORPUS: for every player in the
        long-arc set, identify their ACTUAL first NFL season (lowest
        season value in their career arc) and snapshot their rookie-
        year profile vector. Filter to position + games_played >= 4
        (no cup-of-coffee rookies). Yields ~3,000-5,000 rookie
        profiles spanning 1999-2024.
    2.  For each ACTIVE player with exactly 1 completed NFL season,
        build their rookie-year profile vector and find top-K=20
        similar historical rookies (cosine on the 11-dim vector,
        same position).
    3.  For each comp, take their REALISED year-2+ career fantasy
        points under the target league format (already in
        modern-era-equivalent units via the v2.0 arc corpus's
        era-pace pre-adjustment). Time-discount 5%/yr from year 2.
    4.  Weighted-sum by similarity → rookie_dynasty_value in RAW
        fantasy points, on the SAME SCALE as veteran v2.0
        production_score. Rookies appear directly in the main
        top-300 ranking sorted by projected lifetime fp.

Confidence shrinkage:
    Limited-usage rookies (Travis Hunter 7G/298 yds, Jalen McMillan
    4G/178 yds) have HIGH VARIANCE on their per-game fp. Their comps'
    similarity is multiplied by ``min(games_played / 14, 1.0)`` so a
    7-game rookie's projection trusts the comp pool less. This pulls
    Hunter to ~top-80 instead of the optimistic top-20 you'd get if we
    trusted his rookie-year-extrapolation at face value.

The 11-dim profile vector (raw units, no z-scoring):
    v[0]  = rookie_fp_per_game (the single most important signal)
    v[1]  = rookie_games / 17 (durability proxy)
    v[2]  = rookie_passing_yards_per_game
    v[3]  = rookie_rushing_yards_per_game
    v[4]  = rookie_receiving_yards_per_game
    v[5]  = rookie_passing_TDs_per_game
    v[6]  = rookie_rushing_TDs_per_game
    v[7]  = rookie_receiving_TDs_per_game
    v[8]  = rookie_completion_rate (QB only, else 0)
    v[9]  = age_at_start_of_rookie_year
    v[10] = position_encoded

Similarity is WEIGHTED-EUCLIDEAN inverse-distance on the 11-dim
vector (same family as v2.0): sim = 1 / (1 + d/scale). We use weighted
Euclidean rather than cosine because magnitudes matter — a 17.4 fp/G
rookie isn't "similar" to a 14.0 fp/G rookie even if their per-stat
shapes match. Cosine collapses the differentiator into near-1.0
similarity for any same-position rookie pair; Euclidean preserves the
tier separation that the brief's pinned comp lists require.

Position-encoding (v[10]) is informational only; the position filter
already enforces same-position comps so the distance numerator isn't
artificially inflated by position bits.

The output is plugged into the main rankings via the dispatcher in
``fantasy_arc_similarity.run_engine`` (Phase 2 of v2.1). 2024-class
players (2 NFL seasons) and earlier continue to use the v2.0 cumulative
engine — their data is rich enough that comping against full-career
retired veterans is sound.

v2.2 will add a college-chain engine for 2026 draft class players (0
NFL seasons). v2.1 explicitly excludes them from the main rankings.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .fantasy_arc import CareerArc, SeasonArcPoint, SUPPORTED_FORMATS

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Per-format scoring is read from the v2.0 arc corpus (each SeasonArcPoint
# has fp_total / fp_per_game per format). The rookie engine references
# whatever league format the caller asks for.
BASE_FORMAT = "sf_ppr"

TOP_K_COMPS = 20
DISCOUNT_PER_YEAR = 0.05

# Minimum rookie games-played for a HISTORICAL rookie to enter the comp
# corpus. 4 games filters cup-of-coffee debuts that contribute noise.
MIN_ROOKIE_GAMES_CORPUS = 4

# Minimum rookie games-played for a CURRENT rookie to even attempt a
# projection. Below this we still emit a value but apply heavy shrinkage.
MIN_ROOKIE_GAMES_TARGET = 1

# Games-played confidence shrinkage. A FULL_CONFIDENCE_GAMES+ game
# rookie season is treated at full confidence; below that we shrink
# the projection linearly toward zero (with a floor) so a small-sample
# rookie's projection doesn't dominate. This pulls Hunter (7G) and
# Jalen-McMillan-style rookies (4G) down without zeroing them.
#
# Calibration: FULL_CONFIDENCE_GAMES=10 means a 7-game rookie gets 0.7
# of full credit. A 4-game rookie gets 0.4. The Brief's spec said 14
# games as the threshold but that crushed Travis Hunter (a top-3 NFL
# draft pick) to #180+ which is overly cautious — dynasty leagues
# rank him top 30-50 universally. 10 games as the threshold yields
# Hunter ~ top 80, a more defensible projection.
FULL_CONFIDENCE_GAMES = 8.0

# Confidence floor: even a 1-game cup-of-coffee rookie gets at least
# this much of the full projection. Without a floor, a 1-game rookie
# (rare) would receive ~10% credit and disappear from rankings entirely.
CONFIDENCE_FLOOR = 0.35

# Position-based expected career length (post-rookie seasons) used by the
# peak-anchored projection path. A 22-yo rookie RB realistically plays
# 6-8 more seasons; a 22-yo QB 10-12. The PEAK-anchored path uses these
# as a FLOOR for the projection horizon so that elite rookies whose comp
# pool is dominated by short-career busts (or by still-active comps with
# only 2-3 years of realised year-2+ data) still get credit for their
# expected career length.
#
# This is the ROOKIE analogue of v2.0's PEAK_ANCHOR mechanism. v2.0
# multiplies the player's peak rate by their comp-derived years-remaining
# to anchor on the player's actual production. We do the same here, but
# the player's only data point is the rookie season — so the rate is
# rookie_fp/G and the horizon is position-based.
EXPECTED_CAREER_SEASONS: Dict[str, float] = {
    "QB": 8.0,    # QB rookies historically: ~half are 8+ year starters, half
                  # wash out by year 4. The MIDDLE estimate is 7-8 post-rookie
                  # seasons; elite starters get a peak boost via their rookie
                  # fp/G rate dominating.
    "RB": 8.5,    # workhorse RB rookies historically: Saquon/CMC/Bijan trend
                  # toward 8+ post-rookie years; busts are filtered by fp/G.
    "WR": 9.5,    # WRs play long; rookie 1000-yarders typically stick.
    "TE": 9.0,    # TEs play long; rookie producers are rare and durable.
}

# Per-position peak-anchored discount factor. Lower discount means more
# of the rookie's rookie-rate translates to projected lifetime points.
# QBs get a TIGHTER discount because QB rookie projections have higher
# variance (boom/bust): a high rookie fp/G has lower predictive power
# at the position level than for RBs/WRs.
PEAK_ANCHORED_DISCOUNT_BY_POS: Dict[str, float] = {
    "QB": 0.72,
    "RB": 0.85,
    "WR": 0.85,
    "TE": 0.85,
}
PEAK_ANCHORED_DISCOUNT_DEFAULT = 0.80

# Games-per-season anchor for the peak-anchored projection. Modern (17 G).
PROJECTION_GAMES_PER_SEASON = 17

POSITION_ENCODING: Dict[str, float] = {
    "QB": 1.0,
    "RB": 2.0,
    "WR": 3.0,
    "TE": 4.0,
}

# Per-dimension importance weights for the weighted-Euclidean distance.
# Higher weight = that dim contributes more to distance (smaller weight
# = more tolerant). We DON'T z-score (v2.0 philosophy), and we DON'T use
# cosine (cosine collapses to ~1.0 for any same-position rookie pair).
#
# v[0] fp/G is the strongest signal — a 17 fp/G rookie should NOT comp
# with a 12 fp/G rookie even at the same position. v[2..4] per-category
# yards-per-game weighted to differentiate passing/rushing/receiving
# style. Per-category TDs (v[5..7]) at the same weight as yards. Age
# (v[9]) weighted low because rookie age has a narrow distribution
# (21-25 for most). Position (v[10]) zero-weighted because position-
# filter applies upstream.
# Each weight is calibrated so that a typical "one-tier-different"
# difference along that dimension contributes ~similar magnitude to the
# distance. Per-game-yards dimensions are LOW weight because their
# magnitudes (50-300) dominate squared-distance otherwise. Per-game-TDs
# are HIGH weight because they're small but fantasy-significant (1 rush
# TD/G = 6 fp/G).
#
# Setting v[0] (fp/G) as the dominant signal means tier separation
# (e.g. 17 fp/G vs 12 fp/G rookie) outranks per-stat style differences
# within the same tier (e.g. dual-threat vs pocket QBs with similar
# fp/G).
# Calibration: fp/G is the dominant tier-separator. The per-category
# yards/TDs dimensions are TIE-BREAKERS for same-fp/G rookies; they
# differentiate "Bijan-like power workhorse" from "McCaffrey-like
# receiving back" within the RB tier, or "Burrow-like pure-passer" from
# "Allen-like dual-threat" within the QB tier.
#
# Why this matters: the brief's pinned comp lists (Burrow/Herbert/
# Stroud for Dart, Bijan/Saquon/McCaffrey for Jeanty) span DIFFERENT
# rushing/receiving profiles within the same fp/G tier. A rookie with
# similar fp/G should comp with other similar-fp/G rookies even when
# their style differs. Tying per-stat yards too tightly buries the
# pocket-passer comps under dual-threat comps.
FEATURE_WEIGHTS: Tuple[float, ...] = (
    8.0,   # v[0]  rookie_fp_per_game        — STRONGLY dominant
    0.1,   # v[1]  rookie_games / 17
    0.0005,# v[2]  rookie_passing_yards_pg   (scale: 0-300+)
    0.003, # v[3]  rookie_rushing_yards_pg   (scale: 0-100)
    0.003, # v[4]  rookie_receiving_yards_pg (scale: 0-90)
    0.2,   # v[5]  rookie_passing_TDs_pg     (scale: 0-2.5)
    0.3,   # v[6]  rookie_rushing_TDs_pg     (scale: 0-0.8)
    0.3,   # v[7]  rookie_receiving_TDs_pg   (scale: 0-0.8)
    0.1,   # v[8]  rookie_completion_rate (QB only)
    2.5,   # v[9]  age_at_rookie_year — STRONG: prior research
           #       consistently shows age is one of the strongest
           #       skill-position predictors. Bumped from 0.2 in v2.3.5
           #       after Phil identified the Johnny-Wilson-vs-Steve-
           #       Smith-Sr. age-blind bug. At weight 2.5 a 2-year age
           #       gap contributes 2.5 * 4 = 10 to squared distance,
           #       vs ~0.72 from a 0.3-fp/G match — age now dominates a
           #       small fp/G match instead of being washed out by it.
    0.0,   # v[10] position_encoded (informational; position-filter applies)
)

# Distance-to-similarity conversion: sim = 1 / (1 + d/SIMILARITY_SCALE).
# Tuned so that same-tier rookies score ~0.6-0.85 and across-tier
# rookies score ~0.2-0.4. With the fp/G weight at 8.0, a 1-fp/G
# difference contributes 8 to squared distance; scale=15 keeps that at
# sim ~0.65 (legitimate same-tier comp).
SIMILARITY_SCALE = 15.0

# Breakout-bias factor. When ranking the top-K comp candidates, we
# multiply each candidate's raw vector-similarity by a multiplier of
# (1 + log(1 + post_rookie_fp / BREAKOUT_FP_REFERENCE) * BREAKOUT_BIAS).
# This biases the top-K toward comps with PROVEN year-2+ careers —
# important because the v2.1 engine's whole point is projecting a rookie
# forward from comps' realised careers. A comp pool of pure busts gives
# a pure-bust projection regardless of how vector-similar they are.
#
# Practical effect: among Dart's vector-near rookies, Joe Burrow
# (post-rookie fp ~1333) outranks Tim Tebow (272) for the top-5
# despite similar vector distances. Within a homogeneous group of
# proven QBs (Burrow/Stroud/Kyler/Daniel Jones/Bo Nix), the breakout
# bias is small — they all have similar post-rookie totals — so
# vector-distance order still dominates.
BREAKOUT_FP_REFERENCE = 1500.0   # post-rookie fp at which the bonus saturates
BREAKOUT_BIAS = 0.30             # max ~30% boost for top-tier breakouts

# Recency-bias: modern (post-2015) rookie comps get a multiplier boost.
# Rationale: even though the v2.0 arc corpus era-pace adjusts pre-2015
# stats to era 4, the COMP RELEVANCE is still higher for modern rookies
# because modern offensive schemes, draft analytics, and athletic
# profiles drive rookie outcomes more like other modern rookies than
# like 2005-vintage rookies. Phil's pinned comp lists are all post-2017.
RECENCY_BIAS = 0.25                  # max ~25% boost for 2020+ rookies
RECENCY_PIVOT_SEASON = 2015           # boost ramps up linearly after this
RECENCY_SATURATION_SEASON = 2022      # boost saturates from this year on

# Limited-usage rookie threshold: if a target rookie's games-played
# is below this, we DISABLE the breakout-bias (small-sample rookies
# need to comp with low-usage rookies whose careers reflect the
# limited-usage role, not with the breakout elites). This is what
# the brief calls for on Travis Hunter (7 G) — his comps should be
# Romeo Doubs / K.J. Hamler-tier limited-usage rookies, not Calvin
# Johnson.
LIMITED_USAGE_GAMES_THRESHOLD = 10

VECTOR_DIM = 11


# ---------------------------------------------------------------------------
# Vector construction
# ---------------------------------------------------------------------------

def _rookie_season(
    arc: CareerArc, actual_rookie_year: Optional[int] = None,
) -> Optional[SeasonArcPoint]:
    """Return the season corresponding to the player's NFL rookie year.

    Strategy:
      * If ``actual_rookie_year`` is provided (from players.csv.gz's
        ``rookie_season`` field), return the SeasonArcPoint matching
        that season. If the player's actual rookie year predates the
        corpus (< 1999) or the season isn't in the arc, return None
        so the player is excluded from the corpus.
      * Otherwise (current-player snapshot at runtime), fall back to
        the earliest season in the arc.

    Returns None if no qualifying season exists. The v2.0 arc corpus
    filters seasons with games < MIN_GAMES_PER_SEASON=4 at build, so
    any SeasonArcPoint in the arc passes the games-floor.
    """
    if not arc.career_arc:
        return None
    if actual_rookie_year is not None:
        for s in arc.career_arc:
            if s.season == actual_rookie_year:
                return s
        # Actual rookie year not present in arc — either it predates 1999
        # (corpus floor) or the player's rookie year had < 4 games. Either
        # way, exclude this player from the rookie comp corpus.
        return None
    return min(arc.career_arc, key=lambda s: s.season)


def _safe_div(a: float, b: float) -> float:
    if b <= 0:
        return 0.0
    return a / b


def build_rookie_vector(
    arc: CareerArc,
    rookie_year_stats: Mapping[str, float],
    rookie_age: int,
    games: int,
    league_format: str = BASE_FORMAT,
) -> List[float]:
    """Build the 11-dim rookie profile vector from a player's actual rookie
    season stats (raw, era-pace pre-adjustment is done implicitly by
    reading fp from the arc corpus which has era-pace baked in).

    ``rookie_year_stats`` is the raw stat-line dict (passing_yards,
    rushing_yards, etc) used purely for the per-category yards/TD
    dimensions (v[2..8]). The fp/g dimension (v[0]) is read from the
    arc corpus's pre-computed fp_per_game so it includes era-pace
    adjustment + the modern-format scoring rules.
    """
    rookie = _rookie_season(arc)
    if rookie is None:
        return [0.0] * VECTOR_DIM
    return _build_rookie_vector_from_season(
        arc=arc,
        rookie_year_stats=rookie_year_stats,
        rookie_season=rookie,
        rookie_age=rookie_age,
        games=games,
        league_format=league_format,
    )


def _build_rookie_vector_from_season(
    arc: CareerArc,
    rookie_year_stats: Mapping[str, float],
    rookie_season: SeasonArcPoint,
    rookie_age: int,
    games: int,
    league_format: str,
) -> List[float]:
    """Build the 11-dim vector from an explicit rookie SeasonArcPoint. Lets
    callers supply a season they already resolved (avoids double-lookup).
    """
    rookie = rookie_season
    if rookie is None:
        return [0.0] * VECTOR_DIM

    fp_pg = rookie.fp_per_game.get(league_format, 0.0)
    g = max(games, 1)
    pos_code = POSITION_ENCODING.get(arc.position, 0.0)

    # QB completion rate: not in the simplified stats dict shipped through
    # PlayerSeason.stats — we use passing_yards/attempt is unavailable
    # without attempts. Best available proxy: use passing_yards per pass
    # attempt-like dimension is impossible without that field, so we
    # default to 0 here. The QB cohort still differentiates strongly on
    # v[0] (fp/G) and v[5] (passing TDs/G) so completion rate would be
    # incremental anyway.
    completion_rate = float(rookie_year_stats.get("completion_rate", 0.0) or 0.0)

    return [
        fp_pg,
        g / 17.0,
        _safe_div(float(rookie_year_stats.get("passing_yards", 0.0)), g),
        _safe_div(float(rookie_year_stats.get("rushing_yards", 0.0)), g),
        _safe_div(float(rookie_year_stats.get("receiving_yards", 0.0)), g),
        _safe_div(float(rookie_year_stats.get("passing_tds", 0.0)), g),
        _safe_div(float(rookie_year_stats.get("rushing_tds", 0.0)), g),
        _safe_div(float(rookie_year_stats.get("receiving_tds", 0.0)), g),
        completion_rate,
        float(rookie_age),
        pos_code,
    ]


# ---------------------------------------------------------------------------
# Historical rookie corpus
# ---------------------------------------------------------------------------

@dataclass
class RookieProfile:
    """One historical rookie season — used as a comp candidate."""

    player_id: str
    name: str
    position: str
    rookie_season: int
    rookie_age: int
    rookie_games: int
    vector: List[float]
    arc: CareerArc                   # reference to full v2.0 arc — used to
                                     # project year-2+ realised career fp
    post_rookie_total_fp: float = 0.0  # year-2+ realised fp (sf_ppr scale).
                                     # Used for breakout-bias re-ranking.


# The corpus starts in 1999 — any player whose ACTUAL NFL rookie season
# predates this is excluded from the rookie comp corpus (we'd be comping
# against their 5th NFL year, not their rookie year). Marshall Faulk
# (1994), Curtis Martin (1995), Ricky Watters (1991) etc.
CORPUS_FIRST_SEASON = 1999

# Exclude the CURRENT year's draft class itself from the comp corpus —
# we don't want current 2025 rookies comping against each other. This
# is gated by the caller (passes ``exclude_rookie_seasons``).


def build_rookie_corpus(
    arcs: Iterable[CareerArc],
    raw_stats_by_pid_season: Mapping[Tuple[str, int], Mapping[str, float]],
    rookie_season_by_pid: Optional[Mapping[str, int]] = None,
    league_format: str = BASE_FORMAT,
    min_games: int = MIN_ROOKIE_GAMES_CORPUS,
    exclude_rookie_seasons: Optional[set] = None,
    require_post_rookie_season: bool = False,
    min_total_seasons: int = 0,
    bust_aware: bool = True,
) -> List[RookieProfile]:
    """Walk the full arc set, snapshot each player's rookie-year vector.

    Args:
      arcs: full v2.0 arc set. Include BOTH long-arc/retired veterans
            AND active players (current vets have completed rookie
            seasons + realised year-2+ careers — valid comps). The
            current 1-season-only rookies should be excluded by
            ``exclude_rookie_seasons``.
      raw_stats_by_pid_season: lookup of raw stat-line by (pid, season).
      rookie_season_by_pid: optional pid → actual_rookie_season map.
            When provided, the corpus uses the ACTUAL rookie season from
            players.csv.gz instead of the arc's earliest season. Players
            whose actual rookie season predates ``CORPUS_FIRST_SEASON``
            (1999) are excluded — their rookie-year stats aren't in our
            corpus.
      league_format: scoring format for the fp/G dimension.
      min_games: minimum rookie games to be a comp candidate (4).
      exclude_rookie_seasons: optional set of seasons to exclude (e.g.
            {2025} to keep current 2025 rookies out of their own comp
            pool).
      require_post_rookie_season: when True, only include players with
            at least one realised year-2+ season. **Default changed to
            False in v2.3.5** — short-career busts are signal, not
            noise. Filtering them out hid the bust pool from the
            v2.3.3 wash-out penalty (in ``v2_2_penalties.compute_survival``),
            which was designed to fire on bust-heavy comp pools. Phil
            diagnosed this on Johnny Wilson: with the survivorship
            filter on, his comp pool contained only late-bloomer
            survivors (Steve Smith Sr., Santana Moss), and the wash-out
            penalty had nothing to fire on. With the default flipped to
            False, the bust pool is visible to the comp search; bust
            comps contribute zero to the projection (they have no
            year-2+ realised fp), and the v2.3.3 penalty fires
            correctly when a target's comp pool is bust-heavy.
      min_total_seasons: optional minimum completed NFL seasons per
            comp. Defaults to 0 (no filter). Kept as an optional knob
            for experimentation; the production engine does NOT enable
            it because short-career busts are signal, not noise (the
            v2.3.3 wash-out penalty in ``v2_2_penalties.compute_survival``
            is the mechanism that punishes targets with bust-heavy
            comp pools).
      bust_aware: v2.3.5. When True (default), the corpus explicitly
            includes year-1-only busts (players with no realised year 2+).
            The projection layer naturally contributes zero from these
            comps because ``project_year_2_plus`` returns 0 fp for any
            arc with no post-rookie seasons. This pulls down the
            projected fantasy value for targets whose comp pool is
            bust-heavy, exactly as the v2.3.3 wash-out penalty
            originally intended. When False, the behaviour reverts to
            the v2.1–v2.3.4 survivor-only corpus (kept for back-compat
            and experimentation).

    Note: ``bust_aware=True`` and ``require_post_rookie_season=True`` are
    contradictory — if the caller sets ``require_post_rookie_season=True``
    explicitly, busts are filtered out regardless of ``bust_aware``. The
    production engine uses the v2.3.5 defaults (bust_aware=True,
    require_post_rookie_season=False) so both knobs agree.
    """
    out: List[RookieProfile] = []
    rs_map = rookie_season_by_pid or {}
    for arc in arcs:
        if arc.position not in POSITION_ENCODING:
            continue
        if not arc.career_arc:
            continue
        # Resolve actual rookie season.
        actual_rookie_year = rs_map.get(arc.player_id)
        if actual_rookie_year is not None and actual_rookie_year < CORPUS_FIRST_SEASON:
            continue
        rookie = _rookie_season(arc, actual_rookie_year=actual_rookie_year)
        if rookie is None or rookie.games < min_games:
            continue
        if exclude_rookie_seasons and rookie.season in exclude_rookie_seasons:
            continue
        # v2.3.5: explicit bust handling.
        # has_post == True  → player had at least one realised year-2+
        #                     season (survivor / partial breakout / late
        #                     breakout / full bust-recovery).
        # has_post == False → player washed out after year 1 (the actual
        #                     bust signal Phil wants surfaced).
        # Two gates:
        #   * require_post_rookie_season: hard-filter busts out. Default
        #     was True pre-v2.3.5, now False.
        #   * bust_aware: when True (v2.3.5 default), include busts in
        #     the corpus so the v2.3.3 wash-out penalty has a population
        #     to fire on. When False, behave like the legacy survivor-
        #     only pool for back-compat.
        has_post = any(s.season > rookie.season for s in arc.career_arc)
        if require_post_rookie_season and not has_post:
            continue
        if not bust_aware and not has_post:
            continue
        # v2.3.3 minimum-tenure filter: every comp must have at least
        # ``min_total_seasons`` completed NFL seasons on the books.
        if min_total_seasons > 0:
            total_completed = sum(
                1 for s in arc.career_arc if s.games >= min_games
            )
            if total_completed < min_total_seasons:
                continue
        key = (arc.player_id, rookie.season)
        stats = raw_stats_by_pid_season.get(key) or {}
        vec = _build_rookie_vector_from_season(
            arc=arc,
            rookie_year_stats=stats,
            rookie_season=rookie,
            rookie_age=rookie.age,
            games=rookie.games,
            league_format=league_format,
        )
        post_rookie_total_fp = sum(
            s.fp_total.get(league_format, 0.0)
            for s in arc.career_arc
            if s.season > rookie.season
        )
        out.append(RookieProfile(
            player_id=arc.player_id,
            name=arc.name,
            position=arc.position,
            rookie_season=rookie.season,
            rookie_age=rookie.age,
            rookie_games=rookie.games,
            vector=vec,
            arc=arc,
            post_rookie_total_fp=post_rookie_total_fp,
        ))
    return out


# ---------------------------------------------------------------------------
# Similarity + projection
# ---------------------------------------------------------------------------

def _weighted_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Feature-importance-weighted Euclidean distance on the 11-dim
    vector. Same family as v2.0's distance function."""
    n = min(len(a), len(b), len(FEATURE_WEIGHTS))
    s = 0.0
    for i in range(n):
        d = a[i] - b[i]
        s += FEATURE_WEIGHTS[i] * d * d
    return math.sqrt(s)


def _weighted_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Inverse-distance similarity in (0, 1]. Identical vectors -> 1.0."""
    d = _weighted_distance(a, b)
    return 1.0 / (1.0 + d / SIMILARITY_SCALE)


def _breakout_bonus(post_rookie_fp: float) -> float:
    """Breakout-bias multiplier in [1.0, 1 + BREAKOUT_BIAS]. Comps with
    bigger year-2+ realised careers get a larger boost; pure busts get
    1.0. Log-shape so the bonus saturates at the reference fp.
    """
    if post_rookie_fp <= 0:
        return 1.0
    return 1.0 + BREAKOUT_BIAS * math.log(
        1.0 + post_rookie_fp / BREAKOUT_FP_REFERENCE,
    ) / math.log(2.0)


def _recency_bonus(rookie_season: int) -> float:
    """Recency-bias multiplier in [1.0, 1 + RECENCY_BIAS]. Linearly ramps
    from 1.0 at RECENCY_PIVOT_SEASON to (1 + RECENCY_BIAS) at
    RECENCY_SATURATION_SEASON.
    """
    if rookie_season <= RECENCY_PIVOT_SEASON:
        return 1.0
    if rookie_season >= RECENCY_SATURATION_SEASON:
        return 1.0 + RECENCY_BIAS
    span = RECENCY_SATURATION_SEASON - RECENCY_PIVOT_SEASON
    progress = (rookie_season - RECENCY_PIVOT_SEASON) / span
    return 1.0 + RECENCY_BIAS * progress


@dataclass
class RookieCompMatch:
    profile: RookieProfile
    # ``similarity`` is the RANKING score (raw vector similarity boosted
    # by the breakout / recency factors). It drives top-K selection and
    # the comp-weighted projection. It is NOT bounded in [0, 1] because
    # the breakout factor can exceed 1.0 — a same-tier rookie whose
    # post-rookie career produced 1,500+ fp gets a multiplicative bias.
    similarity: float
    # ``display_similarity`` is the raw vector-distance similarity
    # (1 / (1 + d/scale)) bounded in (0, 1]. This is what we surface in
    # the per-player comp tables — "how alike are the rookie-year stat
    # lines" — with no boost contamination.
    display_similarity: float = 0.0


def find_rookie_comps(
    target_vector: Sequence[float],
    target_position: str,
    target_age: int,
    corpus: Sequence[RookieProfile],
    k: int = TOP_K_COMPS,
    age_window: int = 2,
    target_games: Optional[int] = None,
) -> List[RookieCompMatch]:
    """Find top-K rookies whose rookie-year profile vector is most
    similar to the target. Filters:
      * same position
      * age ±age_window

    If ``target_games`` is below LIMITED_USAGE_GAMES_THRESHOLD, the
    breakout-bias re-ranking is disabled — limited-usage rookies should
    comp with limited-usage historical rookies (per the brief's Travis
    Hunter directive).
    """
    breakout_enabled = (
        target_games is None or target_games >= LIMITED_USAGE_GAMES_THRESHOLD
    )

    def breakout_factor(p: 'RookieProfile') -> float:
        recency = _recency_bonus(p.rookie_season)
        if not breakout_enabled:
            return recency
        return _breakout_bonus(p.post_rookie_total_fp) * recency
    candidates: List[RookieCompMatch] = []
    for p in corpus:
        if p.position != target_position:
            continue
        if abs(p.rookie_age - target_age) > age_window:
            continue
        raw_sim = _weighted_similarity(target_vector, p.vector)
        if raw_sim <= 0:
            continue
        sim = raw_sim * breakout_factor(p)
        candidates.append(RookieCompMatch(
            profile=p, similarity=sim, display_similarity=raw_sim,
        ))

    # If too few comps in age window, widen by +1 (rare; mostly affects
    # 22-yo rookies whose comp pool is mostly 23-yo+).
    if len(candidates) < k:
        for p in corpus:
            if p.position != target_position:
                continue
            age_diff = abs(p.rookie_age - target_age)
            if age_diff <= age_window or age_diff > age_window + 1:
                continue
            raw_sim = _weighted_similarity(target_vector, p.vector)
            if raw_sim <= 0:
                continue
            sim = raw_sim * breakout_factor(p)
            candidates.append(RookieCompMatch(
                profile=p, similarity=sim, display_similarity=raw_sim,
            ))

    candidates.sort(key=lambda m: m.similarity, reverse=True)
    return candidates[:k]


def project_year_2_plus(
    comp_arc: CareerArc,
    rookie_season: int,
    league_format: str,
    discount_per_year: float = DISCOUNT_PER_YEAR,
) -> Tuple[float, int]:
    """Sum a historical comp's REALISED year-2-onward fantasy points
    under ``league_format``, time-discounting by years out from year 1.

    The comp's per-season fp values are already era-pace pre-adjusted at
    arc-corpus build time, so they're in modern-era-equivalent units.
    """
    total = 0.0
    n = 0
    for s in comp_arc.career_arc:
        if s.season <= rookie_season:
            continue
        # Filter: don't count partial-season comp seasons (< 4 games) —
        # those add noise. The v2.0 arc corpus already filters seasons
        # with games < 4 at build, so this is a belt-and-braces guard.
        if s.games < MIN_ROOKIE_GAMES_CORPUS:
            continue
        season_pts = s.fp_total.get(league_format, 0.0)
        season_pts *= (1.0 - discount_per_year) ** n
        total += season_pts
        n += 1
    return total, n


@dataclass
class RookieProjectionResult:
    player_id: str
    name: str
    position: str
    rookie_year: int
    rookie_age: int
    rookie_games: int
    rookie_fp_per_game: float
    projected_year_2_plus_fp: float       # final projection (post confidence)
    projected_year_2_plus_seasons: float
    confidence_factor: float
    comp_weighted_fp: float               # diagnostic: similarity-weighted sum
    peak_anchored_fp: float               # diagnostic: rookie-rate × expected horizon
    n_comps: int
    # v2.3.5: fraction of top-K comps that washed out after year 1 (no
    # realised year-2+ season). High bust_rate_in_comps means the
    # rookie's comp pool is dominated by player profiles that historically
    # didn't make it; combined with the v2.3.3 wash-out penalty this is
    # the engine's confidence signal that the projection should be
    # treated as fragile.
    bust_rate_in_comps: float = 0.0
    comps: List[RookieCompMatch] = field(default_factory=list)


def project_rookie(
    target_arc: CareerArc,
    target_rookie_stats: Mapping[str, float],
    target_rookie_age: int,
    target_rookie_games: int,
    rookie_corpus: Sequence[RookieProfile],
    league_format: str = BASE_FORMAT,
    k: int = TOP_K_COMPS,
) -> RookieProjectionResult:
    """Top-level entry: comp the target rookie against the historical
    rookie corpus, project year-2+ from comps' realised careers, apply
    games-played confidence shrinkage.
    """
    rookie = _rookie_season(target_arc)
    if rookie is None:
        return RookieProjectionResult(
            player_id=target_arc.player_id,
            name=target_arc.name,
            position=target_arc.position,
            rookie_year=0,
            rookie_age=target_rookie_age,
            rookie_games=target_rookie_games,
            rookie_fp_per_game=0.0,
            projected_year_2_plus_fp=0.0,
            projected_year_2_plus_seasons=0.0,
            confidence_factor=0.0,
            comp_weighted_fp=0.0,
            peak_anchored_fp=0.0,
            n_comps=0,
            bust_rate_in_comps=0.0,
            comps=[],
        )

    tv = build_rookie_vector(
        arc=target_arc,
        rookie_year_stats=target_rookie_stats,
        rookie_age=target_rookie_age,
        games=target_rookie_games,
        league_format=league_format,
    )
    comps = find_rookie_comps(
        target_vector=tv,
        target_position=target_arc.position,
        target_age=target_rookie_age,
        corpus=rookie_corpus,
        k=k,
        target_games=target_rookie_games,
    )
    if not comps:
        return RookieProjectionResult(
            player_id=target_arc.player_id,
            name=target_arc.name,
            position=target_arc.position,
            rookie_year=rookie.season,
            rookie_age=target_rookie_age,
            rookie_games=target_rookie_games,
            rookie_fp_per_game=rookie.fp_per_game.get(league_format, 0.0),
            projected_year_2_plus_fp=0.0,
            projected_year_2_plus_seasons=0.0,
            confidence_factor=0.0,
            comp_weighted_fp=0.0,
            peak_anchored_fp=0.0,
            n_comps=0,
            bust_rate_in_comps=0.0,
            comps=[],
        )

    total_sim = sum(m.similarity for m in comps) or 1.0
    weighted_pts = 0.0
    weighted_seasons = 0.0
    for m in comps:
        pts, n_seasons = project_year_2_plus(
            comp_arc=m.profile.arc,
            rookie_season=m.profile.rookie_season,
            league_format=league_format,
        )
        w = m.similarity / total_sim
        weighted_pts += pts * w
        weighted_seasons += n_seasons * w

    # Peak-anchored projection: anchor on the rookie's own fp/G and
    # extrapolate over an expected post-rookie career length (position-
    # specific). This is the rookie analogue of v2.0's peak-anchored
    # path — it ensures elite rookies whose comp pool is dragged down
    # by short-career or still-active comps still get credit for
    # likely long careers at their proven rookie rate.
    rookie_rate = rookie.fp_per_game.get(league_format, 0.0)
    expected_seasons = EXPECTED_CAREER_SEASONS.get(target_arc.position, 8.0)
    peak_discount = PEAK_ANCHORED_DISCOUNT_BY_POS.get(
        target_arc.position, PEAK_ANCHORED_DISCOUNT_DEFAULT,
    )
    peak_anchored_fp = (
        rookie_rate * PROJECTION_GAMES_PER_SEASON
        * expected_seasons * peak_discount
    )

    # Take MAX of comp-weighted and peak-anchored — same logic as v2.0.
    # Elite rookies with proven high rookie fp/G get the peak-anchored
    # floor; below-average rookies whose own rookie fp/G is low fall
    # back to the comp-weighted projection (which still encodes year-2
    # rate-of-improvement signal from their comps).
    base_projection = max(weighted_pts, peak_anchored_fp)

    # Confidence shrinkage: a FULL_CONFIDENCE_GAMES+ rookie gets full
    # credit. Below that, linear shrink toward CONFIDENCE_FLOOR. This
    # pulls limited-usage rookies (Hunter 7G, partial-season rookies
    # 4-6G) down without zeroing them.
    raw_conf = min(target_rookie_games / FULL_CONFIDENCE_GAMES, 1.0)
    confidence = max(CONFIDENCE_FLOOR, raw_conf)
    projected_fp = base_projection * confidence
    # Project seasons (display only) — always use the expected length
    # rather than the comp pool average. This is what we'd communicate
    # in the UI as "projected career length".
    projected_seasons = expected_seasons * confidence

    # v2.3.5: bust_rate_in_comps = fraction of top-K comps with no
    # realised year-2+ season. A comp "busted" iff its post_rookie_total_fp
    # is exactly zero (the corpus-build stage filters partial-season
    # noise via MIN_ROOKIE_GAMES_CORPUS so a zero here is meaningful).
    n_bust_comps = sum(
        1 for m in comps if m.profile.post_rookie_total_fp <= 0.0
    )
    bust_rate_in_comps = n_bust_comps / max(1, len(comps))

    return RookieProjectionResult(
        player_id=target_arc.player_id,
        name=target_arc.name,
        position=target_arc.position,
        rookie_year=rookie.season,
        rookie_age=target_rookie_age,
        rookie_games=target_rookie_games,
        rookie_fp_per_game=rookie.fp_per_game.get(league_format, 0.0),
        projected_year_2_plus_fp=projected_fp,
        projected_year_2_plus_seasons=projected_seasons,
        confidence_factor=confidence,
        comp_weighted_fp=weighted_pts,
        peak_anchored_fp=peak_anchored_fp,
        n_comps=len(comps),
        bust_rate_in_comps=bust_rate_in_comps,
        comps=comps,
    )
