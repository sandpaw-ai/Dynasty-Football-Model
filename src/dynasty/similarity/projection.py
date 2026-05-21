"""Career arc projection — turn comparables into a dynasty value.

For each NFL player we:

  1. Pick a "query season" — their most recent productive season.
  2. Find top-K comparable historical player-seasons at the same age
     and position (see comparables.py).
  3. Weight each comp by similarity. Compute:
       - projected_remaining_years (weighted median of comp careers)
       - projected_total_remaining_fantasy_points (weighted sum,
         time-discounted at 5%/year). v0.15.0: the comp seasons'
         fantasy points are re-scored under the ACTIVE league_format's
         rules — see ``_rescore_career_remaining_after()``.
  4. Convert projected lifetime points → POSITIONAL VORP by subtracting
     the per-position replacement baseline derived from the active
     player pool. Apply a scarcity-cliff multiplier on top.
  5. Rescale the VORP-adjusted value into a dynasty_value in [0, 100]
     across ALL positions in the format (no longer per-position) so the
     positional gap is preserved in the cross-position ranking.

Format awareness (v0.15.0): every step above is parametrized by
``league_format``. Replacement baselines differ between sf_ppr (QB24,
RB36, WR48, TE12) and 1qb_ppr (QB12, others same), which is what
encodes the SF QB premium that the v0.14 model missed.

The dynasty_value is what the new composite consumes. It encodes:
  * current skill (via the vector)
  * longevity (via the comp careers' realized futures)
  * format-aware scoring (re-scored comp seasons)
  * positional scarcity (VORP + cliff multiplier)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Optional

from .comparables import (
    Comparable,
    CohortIndex,
    build_cohort_index,
    find_comparables,
    find_comparables_cohort,
)
from .comparables import _player_seasons_by_pid
from .vectorize import (
    PlayerSeason,
    build_nfl_corpus,
    compute_zscore_stats,
)
from ..scoring_rules import score_season
from ..composite_weights import elite_proven_config


# ---------------------------------------------------------------------------
# Self-projection floor (v0.15.0)
# ---------------------------------------------------------------------------
#
# The KNN engine systematically under-projects veteran starters whose
# same-age comps in the 1999-2024 corpus declined or retired (Allen at
# 28 KNN-matches to Plummer at 28, who was three years from retirement;
# Mahomes at 28 matches journeymen like Carr 2020). This isn't a bug
# in KNN — it's a corpus skew: elite-tier QBs are RARE, and same-age
# elite-tier comps are even rarer.
#
# To floor the projection at something realistic, we blend the KNN
# projection with a *self-projection*: take the player's recent
# 2-3-season average and project N more years assuming a position-
# specific decline curve (peaks-and-fades hand-tuned to the 1999-2024
# corpus aggregates).
#
# Blend weight controlled by ``SELF_PROJECTION_FLOOR_WEIGHT``: 0 = pure
# KNN (legacy v0.14 behavior), 1 = pure self-projection. The default
# 0.5 lets the KNN engine still distinguish among players of similar
# production but with very different comp quality (a young breakout
# RB with elite RB comps should still beat a journeyman with similar
# raw production).

SELF_PROJECTION_FLOOR_WEIGHT: dict[str, float] = {
    "QB": 0.55,   # KNN is most broken for QBs (see Allen/Mahomes)
    "RB": 0.35,   # RB careers are short — KNN's mortality signal matters more
    "WR": 0.40,   # WR longevity is real; bias slightly toward KNN
    "TE": 0.40,
}

# Realistic expected remaining seasons at each (position, age). Tuned
# off PFR's career-length distributions; trades precision for
# robustness.
# Returns expected remaining seasons given current age.
def _expected_remaining_years(position: str, age: float) -> float:
    pos = (position or "").upper()
    a = max(20.0, min(40.0, age or 27.0))
    if pos == "QB":
        # QBs decline gracefully; many start to drop off at 36+.
        return max(1.0, 36.0 - a + 1.0)
    if pos == "RB":
        # RB cliff arrives early; peak at 23-26 then sharp dropoff.
        return max(1.0, 30.0 - a)
    if pos == "WR":
        # WRs play long; cliff at 32-33.
        return max(1.0, 33.0 - a + 1.0)
    if pos == "TE":
        return max(1.0, 33.0 - a + 1.0)
    return max(1.0, 30.0 - a + 1.0)


# Position-specific decay-per-year for the player's own production
# over the projected remaining years.
_SELF_PROJECTION_DECAY = {
    "QB": 0.94,   # ~6% decay/year (modest)
    "RB": 0.85,   # 15% decay/year (steep)
    "WR": 0.92,
    "TE": 0.92,
}


def _self_projection(
    pid: str,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
    query_season: int,
    query_age: Optional[float],
    base_pts_override: Optional[float] = None,
) -> float:
    """Floor projection: re-score the player's recent 2-3 seasons under
    the active format, average per-season, then project N more years
    with a position-specific decay curve.

    ``base_pts_override`` lets the elite-proven path inject a blended
    base-points (recent_3yr × recent_weight + peak_3yr × peak_weight)
    while reusing the same decay + remaining-years math.

    Returns the projected remaining lifetime points (before time-discount).
    """
    arr = by_pid.get(pid, [])
    if not arr:
        return 0.0
    pos = arr[0].position
    if base_pts_override is not None:
        base_pts = base_pts_override
    else:
        # Use the player's last up-to-3 seasons (relative to query_season)
        recent = [ps for ps in arr if ps.season <= query_season][-3:]
        if not recent:
            return 0.0
        rescored = [score_season(ps.raw, league_format, position=pos) for ps in recent]
        base_pts = sum(rescored) / len(rescored)
    if base_pts <= 0:
        return 0.0
    yrs = _expected_remaining_years(pos, query_age if query_age is not None else 27.0)
    decay = _SELF_PROJECTION_DECAY.get(pos, 0.9)
    total = 0.0
    pts = base_pts
    for _ in range(int(round(yrs))):
        pts *= decay
        total += pts
    return total


# ---------------------------------------------------------------------------
# v0.18.0 — Elite-proven veteran calibration
# ---------------------------------------------------------------------------
#
# Detection + adaptive blend + track-record floor for proven-elite
# veterans whose recent 2-3 seasons happen to be down years while their
# long career arc is unambiguously elite. See
# ``docs/ELITE-PROVEN-CALIBRATION.md`` and ``composite_weights.py``
# ``ELITE_PROVEN_CONFIG`` for the policy.
#
# Decision flowchart (per player):
#
#       +---------------------------------------+
#       | csn = number of NFL seasons through Y |
#       +-------------------+-------------------+
#                           |
#               csn < 5  ---+---  csn >= 5
#                  |                 |
#                  v                 v
#         (young player)   +---------------------+
#         KNN-only path    | cumulative pct >=85 |
#         (legacy)         |   AND peak pct >=90 |
#                          +----+-----------+----+
#                               |           |
#                              No          Yes
#                               |           |
#                               v           v
#                       (non-elite vet)  ELITE_PROVEN flag set
#                       PR #15 blend     adaptive blend +
#                       (0.55/0.45)      track-record floor
#                                        position-weight applies
#
# QB — full elite-proven effect (peak_weight = 0.70)
# WR — moderate effect (peak_weight = 0.55)
# TE — moderate effect (peak_weight = 0.55)
# RB — elite-proven DISABLED (cliff is real; recent decline IS signal)


@dataclass(frozen=True)
class ElitePoolStats:
    """Per-position percentile thresholds used to detect elite_proven.

    Computed ONCE per projection run across the historical corpus.
    All thresholds are in raw re-scored fantasy points under the
    active league_format.

    Cumulative thresholds are CSN-cohort-normalized: a player at
    career_season_number=N is compared against all historical players
    at the same position who reached at least N seasons, measured by
    their cumulative fantasy points THROUGH-CSN-N (not their final
    career total). This makes "p85 cum at csn=5" mean "top 15% of
    5-year veterans at this point in their career," which is what the
    task spec actually wants for Mahomes-class detection.

    Peak thresholds are the simpler full-position-pool percentile
    because peak single-season fantasy doesn't shift with career stage.
    """
    # (position, csn) → threshold cumulative fantasy through csn seasons
    cumulative_threshold_by_pos_csn: dict[tuple[str, int], float]
    # position → threshold peak single-season fantasy
    peak_threshold_by_pos: dict[str, float]
    config: dict                                    # frozen copy of ELITE_PROVEN_CONFIG


def _percentile_value(sorted_vals: list[float], pct: float) -> float:
    """Return the value at ``pct`` (0..1) of ``sorted_vals`` (ascending).
    Uses nearest-rank.
    """
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = max(0, min(n - 1, int(round(pct * (n - 1)))))
    return sorted_vals[idx]


def _player_career_fantasy(
    pid: str,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
    through_season: Optional[int] = None,
) -> tuple[float, float, int]:
    """Return (career_total, peak_single_season, seasons_played) for a
    player's career, re-scored under ``league_format``.

    If ``through_season`` is given, only seasons with
    ``season <= through_season`` are counted.
    """
    arr = by_pid.get(pid, [])
    if not arr:
        return (0.0, 0.0, 0)
    pos = arr[0].position
    seasons = arr if through_season is None else [
        ps for ps in arr if ps.season <= through_season
    ]
    if not seasons:
        return (0.0, 0.0, 0)
    per_season = [score_season(ps.raw, league_format, position=pos) for ps in seasons]
    return (sum(per_season), max(per_season), len(seasons))


def build_elite_pool_stats(
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
    config: Optional[dict] = None,
) -> ElitePoolStats:
    """Compute per-(position, csn) percentile thresholds for elite-proven
    detection.

    Cumulative thresholds use a CSN-cohort-normalized distribution:
    for each (position, csn=N), the basis is every historical player
    at that position who reached >= N seasons, measured by their
    cumulative-through-csn-N fantasy points.

    Peak thresholds use the simpler all-position historical pool.

    Restricted to players with at least ``csn_threshold`` seasons —
    we're computing the "proven veteran" distribution, not the
    "all rookies" distribution.
    """
    cfg = config if config is not None else elite_proven_config()
    csn_threshold = int(cfg["csn_threshold"])
    cum_pct = cfg["cumulative_percentile_threshold"]
    peak_pct = cfg["peak_percentile_threshold"]

    # (position, csn) → list of cumulative fantasy-through-csn values
    cum_pool: dict[tuple[str, int], list[float]] = {}
    peak_by_pos: dict[str, list[float]] = {}
    for pid, arr in by_pid.items():
        if not arr:
            continue
        pos = arr[0].position
        per_season = [score_season(ps.raw, league_format, position=pos) for ps in arr]
        n_total = len(per_season)
        if n_total >= 1:
            peak_by_pos.setdefault(pos, []).append(max(per_season))
        # For each csn (>= csn_threshold) the player reached, record
        # the through-csn cumulative.
        running = 0.0
        for i, pts in enumerate(per_season):
            running += pts
            csn = i + 1
            if csn >= csn_threshold:
                cum_pool.setdefault((pos, csn), []).append(running)

    cum_thresh: dict[tuple[str, int], float] = {}
    for key, vals in cum_pool.items():
        cum_thresh[key] = _percentile_value(sorted(vals), cum_pct)

    peak_thresh: dict[str, float] = {}
    for pos, vals in peak_by_pos.items():
        peak_thresh[pos] = _percentile_value(sorted(vals), peak_pct)

    return ElitePoolStats(
        cumulative_threshold_by_pos_csn=cum_thresh,
        peak_threshold_by_pos=peak_thresh,
        config=cfg,
    )


def _detect_elite_proven(
    query: PlayerSeason,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
    elite_pool_stats: ElitePoolStats,
) -> tuple[bool, dict]:
    """Return (is_elite_proven, debug_dict).

    Strict AND: csn >= csn_threshold AND cumulative >= p85 of position
    pool AND peak_single_season >= p90 of position pool AND position
    is NOT disabled (e.g. RB).
    """
    cfg = elite_pool_stats.config
    pos = (query.position or "").upper()

    arr = by_pid.get(query.player_id, [])
    seasons_through = [ps for ps in arr if ps.season <= query.season]
    csn = len(seasons_through)

    cum_total, peak, _ = _player_career_fantasy(
        query.player_id, by_pid, league_format, through_season=query.season
    )
    # CSN-cohort-normalized cumulative threshold. For csn=N, the basis
    # is every historical player at the same position who reached N+
    # seasons; threshold = p85 of THEIR cumulative-through-csn-N. If
    # this exact (pos, csn) bucket is unobserved (rare — very high csn),
    # fall back to the highest available csn for this position.
    cum_thresh = elite_pool_stats.cumulative_threshold_by_pos_csn.get(
        (pos, csn), None
    )
    if cum_thresh is None:
        # find nearest lower csn bucket at this position
        candidates = [
            (k[1], v) for k, v in elite_pool_stats.cumulative_threshold_by_pos_csn.items()
            if k[0] == pos and k[1] <= csn
        ]
        cum_thresh = max(candidates, key=lambda x: x[0])[1] if candidates else float("inf")
    peak_thresh = elite_pool_stats.peak_threshold_by_pos.get(pos, float("inf"))

    # Position must not have its peak_weight disabled (None).
    pos_peak_w = cfg.get("position_peak_weight", {}).get(pos, cfg["peak_weight"])
    position_enabled = pos_peak_w is not None

    is_elite = (
        position_enabled
        and csn >= cfg["csn_threshold"]
        and cum_total >= cum_thresh
        and peak >= peak_thresh
    )
    debug = {
        "csn": csn,
        "cum_total": round(cum_total, 1),
        "cum_threshold": round(cum_thresh, 1) if cum_thresh != float("inf") else None,
        "peak": round(peak, 1),
        "peak_threshold": round(peak_thresh, 1) if peak_thresh != float("inf") else None,
        "position_enabled": position_enabled,
        "position_peak_weight": pos_peak_w,
        "is_elite_proven": is_elite,
    }
    return is_elite, debug


def _peak_3yr_avg(
    pid: str,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
    through_season: int,
) -> float:
    """Return the average fantasy points of the player's best 3 seasons
    (re-scored under ``league_format``) on or before ``through_season``.

    NOT a recency window — it's the player's OWN peak 3-season window.
    For Mahomes through 2024 this is 2018+2020+2022, not 2022-2023-2024.
    If fewer than 3 seasons exist, averages whatever is available.
    """
    arr = by_pid.get(pid, [])
    if not arr:
        return 0.0
    pos = arr[0].position
    seasons = [ps for ps in arr if ps.season <= through_season]
    if not seasons:
        return 0.0
    scored = sorted(
        (score_season(ps.raw, league_format, position=pos) for ps in seasons),
        reverse=True,
    )
    top = scored[:3]
    return sum(top) / len(top)


def _elite_proven_track_record_floor(
    pid: str,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
    through_season: int,
    projected_remaining_years: float,
    floor_multiplier: float,
) -> float:
    """Track-record floor on projected_total_remaining_ppr (before
    time-discount).

    floor = (career_total / seasons_played) × projected_remaining_years
            × floor_multiplier

    Reads as: "you've averaged X per year for Y years — assume at least
    floor_multiplier of that pace for your projected remaining years."

    For aging veterans with ~0 remaining years (Rodgers at 41), the
    floor collapses toward 0 by construction.
    """
    cum_total, _, n = _player_career_fantasy(
        pid, by_pid, league_format, through_season=through_season
    )
    if n <= 0:
        return 0.0
    career_pace = cum_total / n
    yrs = max(0.0, projected_remaining_years)
    return career_pace * yrs * floor_multiplier


# Time discount per year (present-value framing — dynasty owners value
# production sooner, mortality risk for older players, etc.)
TIME_DISCOUNT_PER_YEAR = 0.05

# ---------------------------------------------------------------------------
# Positional VORP (v0.15.0)
# ---------------------------------------------------------------------------
#
# A 12-team league must field N starters at each position per week.
# The "replacement-level" player at a position is the Nth-best player
# in the league — i.e. the worst guy who is still starting any given
# week. In SF, the SF roster slot is dominated by a QB, so the
# starter count for QB is 2 × 12 = 24. In 1QB it's 1 × 12 = 12.
#
# The replacement-baseline lifetime fantasy points are computed
# dynamically from the player pool: we take the top-N projected
# lifetime points per position, and the Nth value IS the baseline.
# Sub-baseline players have NEGATIVE VORP (or floored at 0).

# Starter counts assumed for the standard 12-team roster shape:
#   QB-SF: 2 (QB1 + QB2 in SF slot)
#   QB-1QB: 1
#   RB: 3 (RB1, RB2, flex-bias toward RB)
#   WR: 4 (WR1, WR2, WR3, flex-bias toward WR)
#   TE: 1
# Times 12 teams.

LEAGUE_TEAMS = 12
STARTERS_PER_TEAM: dict[str, dict[str, int]] = {
    "sf_ppr":          {"QB": 2, "RB": 3, "WR": 4, "TE": 1},
    "1qb_ppr":         {"QB": 1, "RB": 3, "WR": 4, "TE": 1},
    "sf_te_premium":   {"QB": 2, "RB": 3, "WR": 3, "TE": 2},
    "sf_ppr_redraft":  {"QB": 2, "RB": 3, "WR": 4, "TE": 1},
}


def replacement_index(league_format: str, position: str) -> int:
    """Return the 1-based rank that defines replacement level for the
    given (format, position).

    SF: QB24. 1QB: QB12. Others derive similarly.
    Defaults to sf_ppr's shape if the format is unknown.
    """
    shape = STARTERS_PER_TEAM.get(league_format, STARTERS_PER_TEAM["sf_ppr"])
    return LEAGUE_TEAMS * shape.get(position.upper(), 3)


# Scarcity-cliff multiplier — capture how steep the dropoff is from the
# top tier to replacement. Bounded so it can't dominate.
SCARCITY_CLIFF_FACTOR = 0.3       # tunable scaling on cliff steepness
SCARCITY_CLIFF_CAP = 1.5          # maximum multiplier
SCARCITY_CLIFF_FLOOR = 1.0        # never penalize below 1.0


@dataclass(frozen=True)
class CareerArcProjection:
    """Output of the projection step for one player."""
    player_id: str
    player_name: str
    position: str
    query_season: int
    query_age: Optional[float]
    league_format: str
    n_comps: int
    avg_similarity: float
    projected_remaining_years: float
    projected_total_remaining_ppr: float
    projected_discounted_ppr: float
    # v0.15.0 — positional VORP fields
    replacement_baseline: float
    vorp: float                  # projected_discounted_ppr - replacement
    scarcity_multiplier: float   # >= 1.0
    dynasty_value: float        # rescaled 0..100 across the full pool
    comparables: list[Comparable]


def _weighted_avg(values: list[float], weights: list[float]) -> float:
    tw = sum(weights)
    if tw <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / tw


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """Crude weighted median: pair up, sort, find the half-weight crossover."""
    if not values:
        return 0.0
    pairs = sorted(zip(values, weights))
    total = sum(weights)
    if total <= 0:
        return median(values)
    acc = 0.0
    half = total / 2.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


# ---------------------------------------------------------------------------
# Format-aware re-scoring of comp careers (v0.15.0)
# ---------------------------------------------------------------------------


def _rescored_remaining_after(
    pid: str,
    season: int,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str,
) -> tuple[int, float, int]:
    """Compute the comp's realized future career *after* a given season,
    with each future season RE-SCORED under the active league_format's rules.

    Returns ``(n_future_seasons, rescored_future_total, years_played_after)``.

    This is the v0.15.0 fix: the historical corpus stores
    ``fantasy_points_ppr`` as it was scored at the time, but a 2010
    Peyton Manning season under sf_ppr rules with 33 PassTDs scores
    differently than under, say, 6pt-TD legacy redraft. We always
    re-score from raw stats so the projection matches the format
    actually being played.
    """
    arr = by_pid.get(pid, [])
    future = [ps for ps in arr if ps.season > season]
    if not future:
        return (0, 0.0, 0)
    total = 0.0
    pos = arr[0].position if arr else None
    for ps in future:
        # ps.raw carries the original nflverse row with all stat fields.
        total += score_season(ps.raw, league_format, position=pos)
    return (
        len(future),
        total,
        future[-1].season - season,
    )


def project_player(
    query: PlayerSeason,
    corpus: list[PlayerSeason],
    stats: dict,
    by_pid: dict[str, list[PlayerSeason]],
    league_format: str = "sf_ppr",
    k: int = 20,
    age_window: float = 1.0,
    cohort_index: Optional[CohortIndex] = None,
    diagnostics: Optional[list] = None,
    elite_pool_stats: Optional[ElitePoolStats] = None,
) -> CareerArcProjection:
    # PR #17 path: cohort-filtered + percentile-tiered + two-vector
    # blended KNN. Falls back gracefully to legacy snapshot-only KNN
    # inside find_comparables_cohort when the cohort is too thin (rookies
    # or rare-stage-of-career players).
    if cohort_index is not None:
        comps_raw, diag = find_comparables_cohort(
            query=query,
            corpus=corpus,
            snapshot_stats=stats,
            cohort_index=cohort_index,
            k=k,
            age_window=age_window,
            by_pid=by_pid,
            league_format=league_format,
        )
        if diagnostics is not None:
            diagnostics.append(diag)
    else:
        # Legacy v0.14/v0.15 path — kept for callers that haven't been
        # updated to pre-build the cohort index.
        comps_raw = find_comparables(
            query=query,
            corpus=corpus,
            stats=stats,
            k=k,
            age_window=age_window,
            by_pid=by_pid,
        )
    if not comps_raw:
        return CareerArcProjection(
            player_id=query.player_id,
            player_name=query.player_name,
            position=query.position,
            query_season=query.season,
            query_age=query.age,
            league_format=league_format,
            n_comps=0,
            avg_similarity=0.0,
            projected_remaining_years=0.0,
            projected_total_remaining_ppr=0.0,
            projected_discounted_ppr=0.0,
            replacement_baseline=0.0,
            vorp=0.0,
            scarcity_multiplier=1.0,
            dynasty_value=0.0,
            comparables=[],
        )

    # v0.15.0: rebuild each comp's `remaining_ppr` under the active
    # format's scoring rules. We keep the rest of the Comparable
    # fields the same so the UI still renders comp names + years.
    comps: list[Comparable] = []
    for c in comps_raw:
        n, fpts_rescored, yrs = _rescored_remaining_after(
            c.comp_player_id, c.comp_season, by_pid, league_format
        )
        comps.append(Comparable(
            comp_name=c.comp_name,
            comp_player_id=c.comp_player_id,
            comp_team_or_school=c.comp_team_or_school,
            comp_season=c.comp_season,
            comp_age=c.comp_age,
            similarity=c.similarity,
            remaining_seasons=n,
            remaining_ppr=round(fpts_rescored, 1),
            remaining_standard=c.remaining_standard,
            years_played_after=yrs,
        ))

    # Use cosine similarity clamped to [0, 1] as the weight to avoid
    # giving anti-correlated comps negative weights.
    weights = [max(0.0, c.similarity) for c in comps]
    sim_avg = sum(c.similarity for c in comps) / len(comps)

    remaining_years_vals = [float(c.years_played_after) for c in comps]
    remaining_ppr_vals = [float(c.remaining_ppr) for c in comps]

    proj_years = _weighted_median(remaining_years_vals, weights)
    proj_ppr_total = _weighted_avg(remaining_ppr_vals, weights)

    # v0.15.0: blend KNN projection with the player's own self-projection.
    # Without this floor, veteran starters whose KNN comps retired early
    # (Allen at 28 → Plummer at 28) get crushed. The self-projection uses
    # the player's own recent 2-3 seasons re-scored under the active
    # format, multiplied by position-specific decay and expected
    # remaining years. The blend weight is tuned per-position.
    #
    # v0.18.0 (elite-proven veteran calibration):
    #   * Detect ELITE_PROVEN (csn>=5, cum>=p85, peak>=p90, position
    #     enabled). For QBs the bar lands Mahomes, Allen, Lamar, Burrow,
    #     Hurts; for WR the bar lands Hill, Adams, Kupp; for TE Kelce.
    #   * For ELITE_PROVEN: self-projection base_pts = recent_w × recent_3yr_avg
    #     + peak_w × peak_3yr_avg. Peak_w is position-tunable (QB=0.70,
    #     WR/TE=0.55, RB=disabled).
    #   * Also enforce a track-record floor: max(KNN-blend, career_pace
    #     × remaining_years × floor_multiplier).
    is_elite_proven = False
    elite_debug: dict = {}
    if elite_pool_stats is not None:
        is_elite_proven, elite_debug = _detect_elite_proven(
            query, by_pid, league_format, elite_pool_stats
        )

    if is_elite_proven:
        cfg = elite_pool_stats.config
        pos_peak_w = cfg.get("position_peak_weight", {}).get(
            query.position.upper(), cfg["peak_weight"]
        )
        if pos_peak_w is None:
            pos_peak_w = cfg["peak_weight"]
        peak_w = float(pos_peak_w)
        recent_w = 1.0 - peak_w  # complementary; ignore raw recent_weight config
        # Recent 3-year avg (re-scored)
        arr = by_pid.get(query.player_id, [])
        recent = [ps for ps in arr if ps.season <= query.season][-3:]
        rescored_recent = [
            score_season(ps.raw, league_format, position=query.position)
            for ps in recent
        ]
        recent_avg = (
            sum(rescored_recent) / len(rescored_recent) if rescored_recent else 0.0
        )
        peak_avg = _peak_3yr_avg(
            query.player_id, by_pid, league_format, query.season
        )
        blended_base = recent_w * recent_avg + peak_w * peak_avg
        # Cap at career-best season × 1.0 to prevent over-projection.
        _, career_peak_single, _ = _player_career_fantasy(
            query.player_id, by_pid, league_format, through_season=query.season
        )
        if career_peak_single > 0:
            blended_base = min(blended_base, career_peak_single)
        self_total = _self_projection(
            query.player_id, by_pid, league_format, query.season, query.age,
            base_pts_override=blended_base,
        )
        # ELITE_PROVEN keeps the SAME floor_weight as non-elite — the
        # change is to the BASE points used inside the self-projection
        # (peak-tilted, not recent-tilted). Raising floor_weight on top
        # would over-promote players who already had reasonable KNN
        # projections and push non-QB elites (Bijan/Gibbs) out of the
        # cross-position top 15.
        floor_weight = SELF_PROJECTION_FLOOR_WEIGHT.get(query.position, 0.4)
    else:
        self_total = _self_projection(
            query.player_id, by_pid, league_format, query.season, query.age
        )
        floor_weight = SELF_PROJECTION_FLOOR_WEIGHT.get(query.position, 0.4)

    proj_ppr_total = (
        (1.0 - floor_weight) * proj_ppr_total + floor_weight * self_total
    )

    # Also blend remaining-years estimate (KNN can mis-predict for
    # veterans whose comps quit early). We use the position-age curve
    # as a soft floor on years.
    expected_yrs = _expected_remaining_years(
        query.position, query.age if query.age is not None else 27.0
    )
    proj_years = (1.0 - floor_weight) * proj_years + floor_weight * expected_yrs

    # v0.18.0: ELITE_PROVEN track-record floor. Only RAISES the
    # projection — it never lowers it. Career pace × projected remaining
    # years × floor_multiplier (default 0.85). For aging veterans whose
    # projected_remaining_years has collapsed (Rodgers at 41), the floor
    # collapses with it — so the aging-decline signal survives.
    if is_elite_proven:
        cfg = elite_pool_stats.config
        floor = _elite_proven_track_record_floor(
            query.player_id,
            by_pid,
            league_format,
            query.season,
            proj_years,
            float(cfg["floor_multiplier"]),
        )
        if floor > proj_ppr_total:
            proj_ppr_total = floor
            elite_debug["floor_applied"] = round(floor, 1)

    if diagnostics is not None and elite_debug:
        # Re-tag the latest diagnostic entry (which was appended above
        # by find_comparables_cohort) with the elite-proven debug. If
        # we're on the legacy path, just append a standalone entry.
        if diagnostics and diagnostics[-1].get("player_id") == query.player_id:
            diagnostics[-1]["elite_proven"] = elite_debug
        else:
            diagnostics.append({
                "player_id": query.player_id,
                "player_name": query.player_name,
                "elite_proven": elite_debug,
            })

    # Time-discount the projected total. Approximate: spread the total
    # across the projected years uniformly and discount each year.
    n_yrs = max(1.0, proj_years)
    per_year = proj_ppr_total / n_yrs
    discounted = 0.0
    for y in range(1, max(1, int(round(n_yrs))) + 1):
        discounted += per_year / ((1.0 + TIME_DISCOUNT_PER_YEAR) ** y)

    # For the 5 surfaced to the UI, dedupe by comp player so we don't
    # show "Dez Bryant 2012, Dez Bryant 2013, Dez Bryant 2014" — pick
    # only the highest-similarity season per comp player.
    seen_pids: set[str] = set()
    top5: list[Comparable] = []
    for c in comps:
        if c.comp_player_id in seen_pids:
            continue
        seen_pids.add(c.comp_player_id)
        top5.append(c)
        if len(top5) >= 5:
            break

    return CareerArcProjection(
        player_id=query.player_id,
        player_name=query.player_name,
        position=query.position,
        query_season=query.season,
        query_age=query.age,
        league_format=league_format,
        n_comps=len(comps),
        avg_similarity=round(sim_avg, 4),
        projected_remaining_years=round(proj_years, 2),
        projected_total_remaining_ppr=round(proj_ppr_total, 1),
        projected_discounted_ppr=round(discounted, 1),
        # VORP fields filled in by ``apply_vorp_and_rescale``
        replacement_baseline=0.0,
        vorp=0.0,
        scarcity_multiplier=1.0,
        dynasty_value=0.0,
        comparables=top5,
    )


def latest_season_for_player(
    pid: str,
    by_pid: dict[str, list[PlayerSeason]],
    min_games: int = 8,
) -> Optional[PlayerSeason]:
    """Pick the most recent meaningfully-productive season for a player."""
    arr = by_pid.get(pid, [])
    if not arr:
        return None
    # Prefer the most recent season with >= min_games; else most recent
    qualified = [ps for ps in arr if ps.games >= min_games]
    if qualified:
        return qualified[-1]
    return arr[-1]


# ---------------------------------------------------------------------------
# Positional VORP + scarcity cliff (v0.15.0)
# ---------------------------------------------------------------------------


def _compute_position_baselines(
    projections: list[CareerArcProjection],
    league_format: str,
) -> dict[str, float]:
    """For each position, take the Nth-best projected_discounted_ppr where
    N = ``replacement_index(format, pos)``. That value IS the
    replacement-level baseline.

    Falls back to the worst player at the position when fewer than N
    players exist (rare for QB/RB/WR/TE in practice).
    """
    by_pos: dict[str, list[float]] = {}
    for p in projections:
        by_pos.setdefault(p.position, []).append(p.projected_discounted_ppr)

    baselines: dict[str, float] = {}
    for pos, vals in by_pos.items():
        if not vals:
            continue
        sorted_desc = sorted(vals, reverse=True)
        n = replacement_index(league_format, pos)
        idx = min(n, len(sorted_desc)) - 1
        baselines[pos] = max(0.0, sorted_desc[idx])
    return baselines


def _compute_scarcity_multipliers(
    projections: list[CareerArcProjection],
    league_format: str,
    baselines: dict[str, float],
) -> dict[str, float]:
    """Compute a per-position scarcity-cliff multiplier.

    cliff_steepness = (top_starters_avg - next_6_avg) / max(next_6_avg, 1)
    multiplier      = clamp(1 + factor * steepness, 1.0, CAP)

    A steep tier cliff (top-tier QBs >> the next 6 right behind) yields
    a larger multiplier, which is then applied to every player at that
    position when computing dynasty_value. This rewards positions where
    the gap between starter-tier and replacement-tier is large.
    """
    by_pos: dict[str, list[float]] = {}
    for p in projections:
        by_pos.setdefault(p.position, []).append(p.projected_discounted_ppr)

    mults: dict[str, float] = {}
    for pos, vals in by_pos.items():
        if not vals:
            mults[pos] = 1.0
            continue
        sorted_desc = sorted(vals, reverse=True)
        n_starters = replacement_index(league_format, pos)
        # top tier = the N starters
        top_slice = sorted_desc[:n_starters] or sorted_desc[:1]
        # cliff sample = the 6 players just past replacement
        cliff_slice = sorted_desc[n_starters : n_starters + 6]
        if not cliff_slice:
            mults[pos] = 1.0
            continue
        top_avg = sum(top_slice) / len(top_slice)
        cliff_avg = sum(cliff_slice) / len(cliff_slice)
        if cliff_avg <= 0:
            mults[pos] = SCARCITY_CLIFF_CAP
            continue
        steepness = (top_avg - cliff_avg) / cliff_avg
        m = SCARCITY_CLIFF_FLOOR + SCARCITY_CLIFF_FACTOR * steepness
        # Clamp into [floor, cap]
        m = max(SCARCITY_CLIFF_FLOOR, min(SCARCITY_CLIFF_CAP, m))
        mults[pos] = m
    return mults


def apply_vorp_and_rescale(
    projections: list[CareerArcProjection],
    league_format: str,
) -> list[CareerArcProjection]:
    """Fill in the VORP-related fields on the projection list and
    rescale ``dynasty_value`` ACROSS positions so positional gaps are
    preserved in the final ranking.

    Order of operations:
      1. compute per-position replacement baselines from the pool itself.
      2. compute per-position scarcity cliff multipliers.
      3. vorp = max(0, discounted_ppr - baseline) × scarcity_multiplier.
      4. rescale vorp → dynasty_value in [0, 100] across the WHOLE pool
         (not per-position). This is the key change vs v0.14: the
         top QB in SF will score higher than the median WR by design,
         because that's literally what the league rewards.

    VORP can be negative (sub-replacement) — we keep the sign so the rank
    order at the back of the pool is preserved. The 0..100 dynasty_value
    rescale then floors at 0 by shifting the whole distribution so the
    worst projection lands at 0 and the best at 100. This is materially
    better than floor-at-0 because veteran starters with a short
    projected career (Allen at 28) still have a meaningful relative
    score above replacement.
    """
    baselines = _compute_position_baselines(projections, league_format)
    mults = _compute_scarcity_multipliers(projections, league_format, baselines)

    # First pass: compute vorp_raw for everyone. We do NOT floor at 0
    # here — sub-replacement players should still rank above the
    # deepest sub-replacement players (a starting QB who projects below
    # QB24 in SF is still ahead of a backup with zero floor).
    enriched: list[CareerArcProjection] = []
    for p in projections:
        base = baselines.get(p.position, 0.0)
        mult = mults.get(p.position, 1.0)
        vorp_raw = (p.projected_discounted_ppr - base) * mult
        enriched.append(CareerArcProjection(
            player_id=p.player_id,
            player_name=p.player_name,
            position=p.position,
            query_season=p.query_season,
            query_age=p.query_age,
            league_format=p.league_format,
            n_comps=p.n_comps,
            avg_similarity=p.avg_similarity,
            projected_remaining_years=p.projected_remaining_years,
            projected_total_remaining_ppr=p.projected_total_remaining_ppr,
            projected_discounted_ppr=p.projected_discounted_ppr,
            replacement_baseline=round(base, 1),
            vorp=round(vorp_raw, 1),
            scarcity_multiplier=round(mult, 3),
            dynasty_value=0.0,
            comparables=p.comparables,
        ))

    # Cross-position rescale: shift so the minimum VORP lands at 0,
    # then divide by the shifted top.
    vorp_vals = [e.vorp for e in enriched]
    if not vorp_vals:
        return enriched
    lo = min(vorp_vals)
    hi = max(vorp_vals)
    span = hi - lo if (hi - lo) > 0 else 1.0
    final: list[CareerArcProjection] = []
    for e in enriched:
        dv = 100.0 * (e.vorp - lo) / span
        final.append(CareerArcProjection(
            player_id=e.player_id,
            player_name=e.player_name,
            position=e.position,
            query_season=e.query_season,
            query_age=e.query_age,
            league_format=e.league_format,
            n_comps=e.n_comps,
            avg_similarity=e.avg_similarity,
            projected_remaining_years=e.projected_remaining_years,
            projected_total_remaining_ppr=e.projected_total_remaining_ppr,
            projected_discounted_ppr=e.projected_discounted_ppr,
            replacement_baseline=e.replacement_baseline,
            vorp=e.vorp,
            scarcity_multiplier=e.scarcity_multiplier,
            dynasty_value=round(dv, 2),
            comparables=e.comparables,
        ))
    return final


# Legacy alias — keep the old name working for any external callers
# (and the existing report.py expects v0.14 names). New code should
# prefer ``apply_vorp_and_rescale``.
def rescale_dynasty_values(
    projections: list[CareerArcProjection],
    league_format: str = "sf_ppr",
) -> list[CareerArcProjection]:
    return apply_vorp_and_rescale(projections, league_format)


def project_all_active_players(
    corpus: Optional[list[PlayerSeason]] = None,
    min_query_season: int = 2023,
    min_games: int = 8,
    league_format: str = "sf_ppr",
    collect_diagnostics: bool = False,
) -> list[CareerArcProjection]:
    """Build projections for every player with a recent active season.

    "Active" = had a season >= min_query_season with >= min_games.

    PR #17: also pre-builds the cohort index (cumulative-career-arc
    vectors + (position, age, career_season_number) buckets) so the
    KNN can apply the cohort filter, percentile-tier band, and the
    two-vector blend.

    PR #18: also pre-builds ``elite_pool_stats`` (per-position p85/p90
    fantasy thresholds across the active pool) so the elite-proven
    veteran calibration can flag Mahomes-class players consistently.
    """
    corpus = corpus or build_nfl_corpus()
    stats = compute_zscore_stats(corpus)
    by_pid = _player_seasons_by_pid(corpus)
    cohort_index = build_cohort_index(corpus, league_format=league_format)

    diagnostics: Optional[list] = [] if collect_diagnostics else None

    # First pass: collect the active player ids so we can build the
    # elite-pool reference distribution against the SAME pool that's
    # being projected (not the historical corpus, which includes many
    # retired-early comps).
    active_pids: list[str] = []
    latest_by_pid: dict[str, PlayerSeason] = {}
    seen_first: set[str] = set()
    for ps in corpus:
        if ps.season < min_query_season:
            continue
        if ps.player_id in seen_first:
            continue
        latest = latest_season_for_player(ps.player_id, by_pid, min_games=min_games)
        if latest is None or latest.season < min_query_season:
            continue
        seen_first.add(ps.player_id)
        active_pids.append(ps.player_id)
        latest_by_pid[ps.player_id] = latest

    elite_pool_stats = build_elite_pool_stats(
        by_pid, league_format=league_format
    )

    projections: list[CareerArcProjection] = []
    seen: set[str] = set()
    for ps in corpus:
        if ps.season < min_query_season:
            continue
        if ps.player_id in seen:
            continue
        latest = latest_by_pid.get(ps.player_id)
        if latest is None:
            continue
        proj = project_player(
            latest,
            corpus,
            stats,
            by_pid,
            league_format=league_format,
            cohort_index=cohort_index,
            diagnostics=diagnostics,
            elite_pool_stats=elite_pool_stats,
        )
        projections.append(proj)
        seen.add(ps.player_id)

    enriched = apply_vorp_and_rescale(projections, league_format)
    if collect_diagnostics:
        # Stash on the module so callers can inspect after the fact
        # without changing the return signature.
        globals()["_LAST_PROJECTION_DIAGNOSTICS"] = diagnostics
    return enriched
