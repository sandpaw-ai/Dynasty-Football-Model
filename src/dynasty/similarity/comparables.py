"""K-Nearest-Neighbor comparable search.

Given a query PlayerSeason, find historical comp seasons that share the
same position and a similar age, ranked by cosine similarity of z-score
vectors.

This is the heart of the career-arc projection: for each comp we know
the rest of their NFL career, so the weighted aggregate becomes a
realized-outcome projection for the query player.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .vectorize import (
    PlayerSeason,
    cosine_similarity,
    vectorize,
)


@dataclass(frozen=True)
class Comparable:
    """A single historical comp for a query season."""
    comp_name: str
    comp_player_id: str
    comp_team_or_school: str
    comp_season: int
    comp_age: Optional[float]
    similarity: float
    # Career outcomes after the comp season (computed by the projection step)
    remaining_seasons: int = 0
    remaining_ppr: float = 0.0
    remaining_standard: float = 0.0
    years_played_after: int = 0


def _player_seasons_by_pid(corpus: list[PlayerSeason]) -> dict[str, list[PlayerSeason]]:
    out: dict[str, list[PlayerSeason]] = {}
    for ps in corpus:
        out.setdefault(ps.player_id, []).append(ps)
    for arr in out.values():
        arr.sort(key=lambda x: x.season)
    return out


def career_remaining_after(
    pid: str,
    season: int,
    by_pid: dict[str, list[PlayerSeason]],
) -> tuple[int, float, float, int]:
    """Compute the comp's realized future career *after* a given season.

    Returns (n_future_seasons, future_ppr_total, future_standard_total,
    years_played_after).
    """
    arr = by_pid.get(pid, [])
    future = [ps for ps in arr if ps.season > season]
    if not future:
        return (0, 0.0, 0.0, 0)
    return (
        len(future),
        sum(ps.fantasy_ppr for ps in future),
        sum(ps.fantasy_standard for ps in future),
        future[-1].season - season,
    )


def find_comparables(
    query: PlayerSeason,
    corpus: list[PlayerSeason],
    stats: dict,
    k: int = 20,
    age_window: float = 1.0,
    exclude_same_player: bool = True,
    by_pid: Optional[dict[str, list[PlayerSeason]]] = None,
) -> list[Comparable]:
    """Find top-k comparable historical seasons for the query.

    Filters:
      * same position
      * abs(age - query_age) <= age_window  (when both ages known)
      * season < query.season (look only at historical comps)
      * not the query player itself (when exclude_same_player)
    """
    if by_pid is None:
        by_pid = _player_seasons_by_pid(corpus)

    qvec = vectorize(query, stats)
    qage = query.age
    candidates: list[tuple[float, PlayerSeason]] = []

    for ps in corpus:
        if ps.position != query.position:
            continue
        if ps.season >= query.season:
            continue
        if exclude_same_player and ps.player_id == query.player_id:
            continue
        if qage is not None and ps.age is not None and abs(ps.age - qage) > age_window:
            continue
        sim = cosine_similarity(qvec, vectorize(ps, stats))
        candidates.append((sim, ps))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:k]

    comps: list[Comparable] = []
    for sim, ps in top:
        n, fppr, fstd, yrs = career_remaining_after(ps.player_id, ps.season, by_pid)
        comps.append(Comparable(
            comp_name=ps.player_name,
            comp_player_id=ps.player_id,
            comp_team_or_school=ps.team,
            comp_season=ps.season,
            comp_age=ps.age,
            similarity=round(sim, 4),
            remaining_seasons=n,
            remaining_ppr=round(fppr, 1),
            remaining_standard=round(fstd, 1),
            years_played_after=yrs,
        ))
    return comps
