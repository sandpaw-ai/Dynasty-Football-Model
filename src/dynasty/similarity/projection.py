"""Career arc projection — turn comparables into a dynasty value.

For each NFL player we:

  1. Pick a "query season" — their most recent productive season.
  2. Find top-K comparable historical player-seasons at the same age
     and position (see comparables.py).
  3. Weight each comp by similarity. Compute:
       - projected_remaining_years (weighted median of comp careers)
       - projected_total_remaining_fantasy_ppr (weighted sum,
         time-discounted at 5%/year)
       - p_still_playing[year_offset]
  4. Rescale the projected PPR total into a dynasty_value in [0, 100].

The dynasty_value is what the new composite consumes. It encodes the
"younger players have more projectable production" principle directly:
a 22yo with comps who averaged 6 more productive seasons will score
materially higher than a 32yo with comps who averaged 2 more, even if
their current production is the same.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Optional

from .comparables import Comparable, find_comparables
from .comparables import _player_seasons_by_pid
from .vectorize import (
    PlayerSeason,
    build_nfl_corpus,
    compute_zscore_stats,
)


# Time discount per year (present-value framing — dynasty owners value
# production sooner, mortality risk for older players, etc.)
TIME_DISCOUNT_PER_YEAR = 0.05


@dataclass(frozen=True)
class CareerArcProjection:
    """Output of the projection step for one player."""
    player_id: str
    player_name: str
    position: str
    query_season: int
    query_age: Optional[float]
    n_comps: int
    avg_similarity: float
    projected_remaining_years: float
    projected_total_remaining_ppr: float
    projected_discounted_ppr: float
    dynasty_value: float        # rescaled 0..100
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


def project_player(
    query: PlayerSeason,
    corpus: list[PlayerSeason],
    stats: dict,
    by_pid: dict[str, list[PlayerSeason]],
    k: int = 20,
    age_window: float = 1.0,
) -> CareerArcProjection:
    comps = find_comparables(
        query=query,
        corpus=corpus,
        stats=stats,
        k=k,
        age_window=age_window,
        by_pid=by_pid,
    )
    if not comps:
        return CareerArcProjection(
            player_id=query.player_id,
            player_name=query.player_name,
            position=query.position,
            query_season=query.season,
            query_age=query.age,
            n_comps=0,
            avg_similarity=0.0,
            projected_remaining_years=0.0,
            projected_total_remaining_ppr=0.0,
            projected_discounted_ppr=0.0,
            dynasty_value=0.0,
            comparables=[],
        )

    # Use cosine similarity clamped to [0, 1] as the weight to avoid
    # giving anti-correlated comps negative weights.
    weights = [max(0.0, c.similarity) for c in comps]
    sim_avg = sum(c.similarity for c in comps) / len(comps)

    remaining_years_vals = [float(c.years_played_after) for c in comps]
    remaining_ppr_vals = [float(c.remaining_ppr) for c in comps]

    proj_years = _weighted_median(remaining_years_vals, weights)
    proj_ppr_total = _weighted_avg(remaining_ppr_vals, weights)

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
        n_comps=len(comps),
        avg_similarity=round(sim_avg, 4),
        projected_remaining_years=round(proj_years, 2),
        projected_total_remaining_ppr=round(proj_ppr_total, 1),
        projected_discounted_ppr=round(discounted, 1),
        dynasty_value=0.0,   # filled in by `rescale_dynasty_values`
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


def rescale_dynasty_values(
    projections: list[CareerArcProjection],
) -> list[CareerArcProjection]:
    """Rescale ``projected_discounted_ppr`` into a 0..100 dynasty value.

    Per-position rescale so that, e.g., a top RB's value ≈ a top WR's
    even though their raw PPR distributions differ.
    """
    by_pos: dict[str, list[CareerArcProjection]] = {}
    for p in projections:
        by_pos.setdefault(p.position, []).append(p)

    new_list: list[CareerArcProjection] = []
    for pos, group in by_pos.items():
        vals = [g.projected_discounted_ppr for g in group]
        if not vals:
            continue
        # Use the position max as the "top of the scale". The discounted
        # PPR distribution has a long right tail (a few elite ageless
        # WRs / young breakout QBs), so a percentile-based scale clipped
        # everyone interesting to 100. Using max keeps the ordering
        # informative without ceilings.
        top = max(vals) or 1.0
        for g in group:
            dv = 100.0 * g.projected_discounted_ppr / top if top > 0 else 0.0
            new_list.append(
                CareerArcProjection(
                    player_id=g.player_id,
                    player_name=g.player_name,
                    position=g.position,
                    query_season=g.query_season,
                    query_age=g.query_age,
                    n_comps=g.n_comps,
                    avg_similarity=g.avg_similarity,
                    projected_remaining_years=g.projected_remaining_years,
                    projected_total_remaining_ppr=g.projected_total_remaining_ppr,
                    projected_discounted_ppr=g.projected_discounted_ppr,
                    dynasty_value=round(dv, 2),
                    comparables=g.comparables,
                )
            )
    return new_list


def project_all_active_players(
    corpus: Optional[list[PlayerSeason]] = None,
    min_query_season: int = 2023,
    min_games: int = 8,
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
        proj = project_player(latest, corpus, stats, by_pid)
        projections.append(proj)
        seen.add(ps.player_id)

    return rescale_dynasty_values(projections)
