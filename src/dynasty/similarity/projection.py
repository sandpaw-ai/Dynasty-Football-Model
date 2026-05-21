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

from .comparables import Comparable, find_comparables
from .comparables import _player_seasons_by_pid
from .vectorize import (
    PlayerSeason,
    build_nfl_corpus,
    compute_zscore_stats,
)
from ..scoring_rules import score_season


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
) -> float:
    """Floor projection: re-score the player's recent 2-3 seasons under
    the active format, average per-season, then project N more years
    with a position-specific decay curve.

    Returns the projected remaining lifetime points (before time-discount).
    """
    arr = by_pid.get(pid, [])
    if not arr:
        return 0.0
    pos = arr[0].position
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
) -> CareerArcProjection:
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
) -> list[CareerArcProjection]:
    """Build projections for every player with a recent active season.

    "Active" = had a season >= min_query_season with >= min_games.
    """
    corpus = corpus or build_nfl_corpus()
    stats = compute_zscore_stats(corpus)
    by_pid = _player_seasons_by_pid(corpus)

    projections: list[CareerArcProjection] = []
    seen: set[str] = set()
    for ps in corpus:
        if ps.season < min_query_season:
            continue
        if ps.player_id in seen:
            continue
        latest = latest_season_for_player(ps.player_id, by_pid, min_games=min_games)
        if latest is None or latest.season < min_query_season:
            continue
        proj = project_player(latest, corpus, stats, by_pid, league_format=league_format)
        projections.append(proj)
        seen.add(ps.player_id)

    return apply_vorp_and_rescale(projections, league_format)
