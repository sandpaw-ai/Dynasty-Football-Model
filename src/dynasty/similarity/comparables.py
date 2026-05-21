"""K-Nearest-Neighbor comparable search.

PR #14: Originally — given a query PlayerSeason, find historical comp
seasons that share the same position and a similar age, ranked by
cosine similarity of z-score vectors.

PR #17 (v0.17.0): The single-season-snapshot comparison was too
forgiving — a fluke 12-GP starter stretch (Jarrett Boykin 2013) could
match an elite season (Puka Nacua 2023+) on per-game shape even though
their CAREER-TO-DATE production lived in different universes. The fix
is a two-stage pipeline:

  1. **Cohort filter.** Pre-index the historical corpus by
     ``(position, age, career_season_number)``. A 3-NFL-season-deep
     24yo can only comp to other 3-NFL-season-deep 24-year-olds at the
     same position. Boykin (2 NFL seasons in by his age-24 year, only
     starter for a partial 2013) gets filtered structurally before
     any KNN scoring happens.
  2. **Production-percentile tier match.** Within the cohort, compute
     the query player's percentile by career-to-date fantasy points
     (re-scored under the active format). Restrict KNN to comps within
     a percentile band of the query (tighter band for elite, wider for
     low-tier). A top-5% age-24-WR-with-3-NFL-seasons (Nacua) can only
     comp to WRs in p80-p100 of that exact cohort.

The KNN score itself is now a blend of two cosine similarities:

  * **Cumulative** (from ``vectorize_cumulative``): career-through-age
    vector. Encodes the full arc, time-decay-weighted toward the most
    recent season.
  * **Snapshot** (from the legacy ``vectorize``): single-season shape.
    Still useful as a "what is this player doing right now" signal.

Blend curve by career_season_number:
  * 1 season:  100% snapshot (rookie \u2014 no arc yet; falls back to v0.14)
  * 2 seasons: 50% / 50% blend
  * 3+ seasons: 70% cumulative / 30% snapshot

This file remains dependency-light (pure Python) so the engine stays
importable on minimal installs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .vectorize import (
    CareerArcVector,
    PlayerSeason,
    build_career_arc_corpus,
    compute_cumulative_zscore_stats,
    cosine_similarity,
    vectorize,
    vectorize_cumulative,
)
from ..scoring_rules import score_season


# ---------------------------------------------------------------------------
# Cohort-filter knobs (PR #17)
# ---------------------------------------------------------------------------
#
# Percentile-tier band — how far above/below the query's career
# production percentile a comp may be. Elite tier (>=p90) gets the
# tightest band; low-tier (<p40) gets the widest so it still surfaces
# enough comps.
ELITE_TIER_PERCENTILE_BAND = 15.0      # \u00b115 percentile points
MID_TIER_PERCENTILE_BAND = 20.0
LOW_TIER_PERCENTILE_BAND = 25.0

# Cohort-widening fallback. If the strict (position, age=A, career
# season=N) cohort has fewer than this many valid comps, widen age to
# \u00b12 (and then \u00b13 if still insufficient). If we still can't reach this
# many comps, projection.py falls back to snapshot-only KNN.
MIN_COHORT_COMPS = 10

# Cumulative-vs-snapshot KNN blend by career_season_number. Index 0
# unused; index 1=rookie (no arc) \u2192 0.0 cum weight; index 2 \u2192 0.5;
# 3+ \u2192 0.7. Anything beyond index 6 stays at 0.7.
_CUM_BLEND_WEIGHTS = (0.0, 0.0, 0.5, 0.7)


def cumulative_blend_weight(career_season_number: int) -> float:
    """Return the cumulative-vector weight in the [0, 1] blend.

    1 NFL season   \u2192 0.0 (pure snapshot, rookie fallback)
    2 NFL seasons  \u2192 0.5 (even blend)
    3+ NFL seasons \u2192 0.7 (cumulative dominates)
    """
    if career_season_number <= 0:
        return 0.0
    if career_season_number < len(_CUM_BLEND_WEIGHTS):
        return _CUM_BLEND_WEIGHTS[career_season_number]
    return _CUM_BLEND_WEIGHTS[-1]


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


# ---------------------------------------------------------------------------
# Cohort index (PR #17)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortIndex:
    """Bucketed historical-corpus index keyed by
    ``(position, age_bucket, career_season_number)``.

    Used to filter the corpus to a structurally-comparable set BEFORE
    any KNN scoring runs.
    """
    # bucket key \u2192 list of arc indices into ``arcs``
    buckets: dict[tuple[str, int, int], list[int]]
    arcs: list[CareerArcVector]
    cum_stats: dict[str, dict[str, tuple[float, float]]]
    # Cohort percentile table: bucket \u2192 sorted list of career_fantasy
    # values (ascending). Used for fast percentile lookup.
    bucket_fantasy: dict[tuple[str, int, int], list[float]]


def _age_bucket(age: Optional[float]) -> int:
    """Bucket ages to integer years for cohort lookup.

    ``round(age)`` would scatter age=23.5 across two buckets; we
    truncate to int so 23.x \u2192 23, matching how PFR conventionally
    discusses "age 23 season".
    """
    if age is None:
        return -1
    return int(age)


def build_cohort_index(
    corpus: list[PlayerSeason],
    league_format: str = "sf_ppr",
) -> CohortIndex:
    """Build the (position, age, career_season_number) cohort index +
    cumulative z-score stats from a PlayerSeason corpus.

    O(N) in corpus size; called once per format per projection run.
    """
    arcs = build_career_arc_corpus(corpus, league_format=league_format)
    cum_stats = compute_cumulative_zscore_stats(arcs)

    buckets: dict[tuple[str, int, int], list[int]] = {}
    bucket_fantasy: dict[tuple[str, int, int], list[float]] = {}
    for i, arc in enumerate(arcs):
        key = (arc.position, _age_bucket(arc.age), arc.career_season_number)
        buckets.setdefault(key, []).append(i)
        bucket_fantasy.setdefault(key, []).append(arc.raw_features.get("career_fantasy", 0.0))

    for vals in bucket_fantasy.values():
        vals.sort()

    return CohortIndex(
        buckets=buckets,
        arcs=arcs,
        cum_stats=cum_stats,
        bucket_fantasy=bucket_fantasy,
    )


def _percentile_in_sorted(value: float, sorted_vals: list[float]) -> float:
    """Return the percentile (0..100) of ``value`` within ``sorted_vals``
    (ascending). If all values equal the query, returns 50.0.
    """
    n = len(sorted_vals)
    if n == 0:
        return 50.0
    # Count of strictly-less + half of equal handles ties symmetrically.
    lo = 0
    eq = 0
    for v in sorted_vals:
        if v < value:
            lo += 1
        elif v == value:
            eq += 1
    return 100.0 * (lo + eq / 2.0) / n


def _percentile_band(query_pct: float) -> float:
    """Wider band for low-tier players, tighter for elite-tier."""
    if query_pct >= 90.0:
        return ELITE_TIER_PERCENTILE_BAND
    if query_pct >= 40.0:
        return MID_TIER_PERCENTILE_BAND
    return LOW_TIER_PERCENTILE_BAND


def _gather_cohort(
    index: CohortIndex,
    position: str,
    age: int,
    csn: int,
    age_window: int = 1,
    csn_window: int = 1,
) -> list[int]:
    """Collect arc indices within ``\u00b1age_window`` age and ``\u00b1csn_window``
    career-season-number of (position, age, csn).
    """
    out: list[int] = []
    for a in range(age - age_window, age + age_window + 1):
        for n in range(max(1, csn - csn_window), csn + csn_window + 1):
            out.extend(index.buckets.get((position, a, n), []))
    return out


# ---------------------------------------------------------------------------
# Two-vector KNN comparable search (PR #17)
# ---------------------------------------------------------------------------


def find_comparables_cohort(
    query: PlayerSeason,
    corpus: list[PlayerSeason],
    snapshot_stats: dict,
    cohort_index: CohortIndex,
    k: int = 20,
    age_window: float = 1.0,
    exclude_same_player: bool = True,
    by_pid: Optional[dict[str, list[PlayerSeason]]] = None,
    league_format: str = "sf_ppr",
) -> tuple[list[Comparable], dict]:
    """PR #17 KNN: cohort-filter + percentile-tier + two-vector blend.

    Returns (top-k Comparables, diagnostics dict). The diagnostics
    expose the cohort size before/after filtering, blend weight used,
    fallback flags, etc. \u2014 the projection layer logs these out for the
    report.
    """
    if by_pid is None:
        by_pid = _player_seasons_by_pid(corpus)

    qage = query.age
    diag: dict = {
        "player_id": query.player_id,
        "player_name": query.player_name,
        "position": query.position,
        "query_age": qage,
        "cohort_size_raw": 0,
        "cohort_size_after_percentile": 0,
        "used_blend_weight": None,
        "fallback_snapshot_only": False,
        "widened_age_window": 1,
        "query_percentile": None,
        "percentile_band": None,
    }

    # ---- Build the query's CUMULATIVE arc ---------------------------------
    q_seasons = [ps for ps in by_pid.get(query.player_id, []) if ps.season <= query.season and ps.age is not None]
    q_seasons.sort(key=lambda x: x.season)
    if not q_seasons:
        # Can't even build a snapshot vector \u2014 give up.
        return [], diag

    csn = len(q_seasons)
    # Query's cumulative arc (re-extract fresh \u2014 the corpus version may
    # not exist if the query is synthesized at a non-standard age).
    from .vectorize import _extract_cumulative_features  # local import to avoid public API
    q_arc_feats = _extract_cumulative_features(q_seasons, query.position, league_format)
    q_career_fantasy = q_arc_feats.get("career_fantasy", 0.0)
    cum_blend_w = cumulative_blend_weight(csn)
    diag["used_blend_weight"] = cum_blend_w
    diag["career_season_number"] = csn

    # ---- Cohort filter ---------------------------------------------------
    cohort_pool: list[int] = []
    widened = 1
    if qage is not None and cum_blend_w > 0.0:
        age_int = int(qage)
        for w in (1, 2, 3):
            cohort_pool = _gather_cohort(cohort_index, query.position, age_int, csn, age_window=w, csn_window=1)
            # Filter out the query player themself when excluding.
            pool_filtered = [
                idx for idx in cohort_pool
                if not (exclude_same_player and cohort_index.arcs[idx].player_id == query.player_id)
                and cohort_index.arcs[idx].latest_season.season < query.season
            ]
            if len(pool_filtered) >= MIN_COHORT_COMPS:
                cohort_pool = pool_filtered
                widened = w
                break
            cohort_pool = pool_filtered
        diag["cohort_size_raw"] = len(cohort_pool)
        diag["widened_age_window"] = widened

    # ---- Percentile-tier filter ------------------------------------------
    pool_after_pct = cohort_pool
    if cohort_pool and cum_blend_w > 0.0:
        # Compute query's percentile in the raw cohort.
        cohort_fantasy = sorted(
            cohort_index.arcs[idx].raw_features.get("career_fantasy", 0.0)
            for idx in cohort_pool
        )
        q_pct = _percentile_in_sorted(q_career_fantasy, cohort_fantasy)
        band = _percentile_band(q_pct)
        lo = q_pct - band
        hi = q_pct + band
        diag["query_percentile"] = round(q_pct, 1)
        diag["percentile_band"] = band

        # Compute each cohort member's percentile in the SAME cohort
        # ordering, then keep those within [lo, hi].
        keep: list[int] = []
        for idx in cohort_pool:
            v = cohort_index.arcs[idx].raw_features.get("career_fantasy", 0.0)
            pct = _percentile_in_sorted(v, cohort_fantasy)
            if lo <= pct <= hi:
                keep.append(idx)
        pool_after_pct = keep
        diag["cohort_size_after_percentile"] = len(pool_after_pct)
    else:
        diag["cohort_size_after_percentile"] = 0

    # ---- Snapshot vectors (always available) -----------------------------
    qvec_snap = vectorize(query, snapshot_stats)

    # ---- Two-vector blend KNN over the cohort ----------------------------
    candidates: list[tuple[float, PlayerSeason, float, float]] = []

    if pool_after_pct and cum_blend_w > 0.0:
        # Cumulative-blended path
        # Build the query's cumulative z-score vector. We re-score it
        # against the cohort's stats so it's comparable to the arcs in
        # the cohort.
        # Note: we use the same cum_stats (whole-corpus) as everyone else.
        from .vectorize import CareerArcVector as _Arc
        q_arc = _Arc(
            player_id=query.player_id,
            player_name=query.player_name,
            position=query.position,
            age=float(qage) if qage is not None else 0.0,
            career_season_number=csn,
            league_format=league_format,
            raw_features=q_arc_feats,
            latest_season=q_seasons[-1],
        )
        q_cum_vec = vectorize_cumulative(q_arc, cohort_index.cum_stats)

        for idx in pool_after_pct:
            arc = cohort_index.arcs[idx]
            ps = arc.latest_season
            if ps.position != query.position:
                continue
            if ps.season >= query.season:
                continue
            if exclude_same_player and ps.player_id == query.player_id:
                continue
            # Cumulative cosine
            c_vec = vectorize_cumulative(arc, cohort_index.cum_stats)
            cum_sim = cosine_similarity(q_cum_vec, c_vec)
            # Snapshot cosine
            snap_sim = cosine_similarity(qvec_snap, vectorize(ps, snapshot_stats))
            # Blended
            sim = cum_blend_w * cum_sim + (1.0 - cum_blend_w) * snap_sim
            candidates.append((sim, ps, cum_sim, snap_sim))
    else:
        # Snapshot-only path (rookie or insufficient cohort)
        diag["fallback_snapshot_only"] = True
        for ps in corpus:
            if ps.position != query.position:
                continue
            if ps.season >= query.season:
                continue
            if exclude_same_player and ps.player_id == query.player_id:
                continue
            if qage is not None and ps.age is not None and abs(ps.age - qage) > age_window:
                continue
            sim = cosine_similarity(qvec_snap, vectorize(ps, snapshot_stats))
            candidates.append((sim, ps, 0.0, sim))

    # If cohort filtering left too few candidates AND we haven't gone
    # snapshot-only yet, fall back to widened or snapshot-only KNN.
    if len(candidates) < MIN_COHORT_COMPS and not diag["fallback_snapshot_only"] and cum_blend_w > 0.0:
        diag["fallback_snapshot_only"] = True
        candidates = []
        for ps in corpus:
            if ps.position != query.position:
                continue
            if ps.season >= query.season:
                continue
            if exclude_same_player and ps.player_id == query.player_id:
                continue
            if qage is not None and ps.age is not None and abs(ps.age - qage) > age_window:
                continue
            sim = cosine_similarity(qvec_snap, vectorize(ps, snapshot_stats))
            candidates.append((sim, ps, 0.0, sim))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:k]

    comps: list[Comparable] = []
    for sim, ps, _cum, _snap in top:
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
    return comps, diag


def find_comparables(
    query: PlayerSeason,
    corpus: list[PlayerSeason],
    stats: dict,
    k: int = 20,
    age_window: float = 1.0,
    exclude_same_player: bool = True,
    by_pid: Optional[dict[str, list[PlayerSeason]]] = None,
) -> list[Comparable]:
    """Legacy single-vector KNN \u2014 kept for back-compat with the v0.14 API
    and the existing similarity tests.

    PR #17 introduces :func:`find_comparables_cohort` which adds the
    cohort filter, percentile-tier matching, and the two-vector blend.
    Callers in the projection pipeline should use the cohort variant.
    The legacy function still applies position + age-window filtering
    and ranks by snapshot cosine similarity.
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
