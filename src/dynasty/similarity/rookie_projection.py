"""Rookie college -> NFL career projection (PR #16).

The rookie engine mirrors :mod:`dynasty.similarity.projection` (the NFL
career-arc engine from PR #14) but operates on COLLEGE seasons. For each
college prospect we:

  1. Vectorize their most-recent college season (z-score across the NCAA
     corpus, per position).
  2. Find the top-K similar college player-seasons at the same position
     and class (FR/SO/JR/SR).
  3. Resolve each comp through ``data/bridge/ncaa_to_nfl.json`` to their
     NFL career.
  4. Aggregate the realized NFL careers weighted by similarity:
       - projected_lifetime_fantasy_points (sf_ppr or 1qb_ppr)
       - projected_career_seasons
       - per_year_probability_in_nfl[year_offset]
       - applies time-discount 5%/year for consistency with PR #14.
  5. Rescale into a per-position 0..100 ``rookie_dynasty_value``.

Comps that DID NOT reach the NFL contribute zero fantasy points (so their
existence pulls the projection down -- the "out-of-NFL-after-college" rate
is itself a strong negative signal).

Format awareness: PR #15's positional VORP + SF-aware scoring engine is
NOT yet merged into upstream/main at the time this PR was cut. The
fallback used here is the raw realized fantasy_ppr / fantasy_standard
totals already present on each NFL player-season. The result is still
format-aware in the sense that QB careers under SF will rank higher than
under 1QB once PR #15 is merged and the scoring layer is plugged in; for
now the rookie engine emits the same dynasty_value under both formats and
PR #15 + PR #16 will compose at the composite-scoring layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .vectorize import (
    CollegePlayerSeason,
    build_college_corpus,
    compute_college_zscore_stats,
    vectorize_college_football_season,
    cosine_similarity,
    PlayerSeason,
)
from .bridge import load_bridge
from ..sources.pro_football_reference import load_pfr_seasons


# Time discount mirrors PR #14 / projection.py.
TIME_DISCOUNT_PER_YEAR = 0.05

# Default K for KNN.
DEFAULT_K = 20

# Position-typical full-career lengths used to extrapolate STILL-ACTIVE
# NFL comps. A 4-year vet still playing under-projects badly if we just
# count realized seasons; we project them to their position's typical
# career length and pro-rate the lifetime fantasy total accordingly. The
# lengths roughly match Pro-Football-Reference's median career-length
# distributions for the post-2014 cohort at each position.
POSITION_TYPICAL_CAREER_LEN = {"QB": 12.0, "RB": 6.0, "WR": 10.0, "TE": 9.0}
# Conservative bonus applied to the still-active extrapolation — active
# players might wash out short of the typical length, so we discount the
# extrapolated tail by this factor.
STILL_ACTIVE_TAIL_DISCOUNT = 0.75
# A comp is treated as "still active" if their last NFL season is >= this
# year (pulled from the latest PFR season seen).
STILL_ACTIVE_THRESHOLD_YEARS_FROM_LAST = 1

# Class-year window: prefer same class, but allow neighbor class as a
# softer relaxation. Same-class first.
SAME_CLASS_WEIGHT = 1.0
NEIGHBOR_CLASS_WEIGHT = 0.7


@dataclass(frozen=True)
class CollegeComparable:
    """A single historical college comp for a query college season."""
    comp_name: str
    comp_cfb_player_id: str
    comp_school: str
    comp_season: int
    comp_class_year: str
    similarity: float
    # Bridged NFL career
    nfl_player_id: Optional[str] = None
    nfl_display_name: Optional[str] = None
    realized_nfl_seasons: int = 0
    realized_career_ppr: float = 0.0
    realized_career_standard: float = 0.0
    last_nfl_season: Optional[int] = None
    out_of_nfl_after_college: bool = True


@dataclass(frozen=True)
class RookieProjection:
    """Output of the rookie similarity engine for one college prospect."""
    cfb_player_id: str
    player_name: str
    position: str
    school: str
    query_season: int
    class_year: str
    n_comps: int
    n_comps_with_nfl: int
    avg_similarity: float
    projected_career_seasons: float
    projected_lifetime_fantasy_points: float
    projected_discounted_ppr: float
    nfl_hit_rate: float                 # weighted comp NFL rate
    rookie_dynasty_value: float         # 0..100, rescaled per-position
    comparables_top5: list[CollegeComparable] = field(default_factory=list)


# ---------------------------------------------------------------------------
# NFL career aggregates by NFL player_id
# ---------------------------------------------------------------------------

def _build_nfl_career_index(min_season: int = 2014) -> dict[str, dict]:
    """Return {nfl_player_id: career_totals} computed from PFR seasons.

    Aggregates lifetime PPR / standard fantasy points and season count
    across all seasons in the PFR cache. This is the "realized future" we
    use to score each comp's bridged NFL career.

    Each entry also carries the player's NFL position group (the
    most-frequent position across their seasons) so the rookie engine can
    apply position-typical career-length extrapolation to still-active
    comps.
    """
    seasons = load_pfr_seasons(min_season=min_season)
    by_pid: dict[str, dict] = {}
    from collections import Counter
    pos_counts: dict[str, Counter] = {}
    for r in seasons:
        pid = r.get("player_id") or ""
        if not pid:
            continue
        try:
            season = int(r["season"])
        except (KeyError, ValueError, TypeError):
            continue
        try:
            ppr = float(r.get("fantasy_points_ppr") or 0)
        except ValueError:
            ppr = 0.0
        try:
            std = float(r.get("fantasy_points") or 0)
        except ValueError:
            std = 0.0
        d = by_pid.setdefault(
            pid,
            {
                "seasons": 0, "career_ppr": 0.0, "career_standard": 0.0,
                "last_season": season, "first_season": season,
                "position": (r.get("position") or "").upper(),
            },
        )
        d["seasons"] += 1
        d["career_ppr"] += ppr
        d["career_standard"] += std
        if season > d["last_season"]:
            d["last_season"] = season
        if season < d["first_season"]:
            d["first_season"] = season
        pos_counts.setdefault(pid, Counter())[(r.get("position") or "").upper()] += 1
    # Pin the modal position per player
    for pid, c in pos_counts.items():
        most, _ = c.most_common(1)[0]
        by_pid[pid]["position"] = most
    return by_pid


def _extrapolated_career_totals(
    career: dict, position: str, max_known_nfl_season: int,
) -> tuple[float, float]:
    """Return (extrapolated_seasons, extrapolated_career_ppr) for an NFL
    career, projecting STILL-ACTIVE players up to their position-typical
    full career length. Retired players (last_season older than the
    bounded threshold) are returned as-is.
    """
    seasons = career.get("seasons", 0)
    if seasons <= 0:
        return (0.0, 0.0)
    realized_ppr = career.get("career_ppr", 0.0)
    last_season = career.get("last_season", 0)
    is_active = last_season >= max_known_nfl_season - STILL_ACTIVE_THRESHOLD_YEARS_FROM_LAST
    typical = POSITION_TYPICAL_CAREER_LEN.get(position, 8.0)
    if not is_active or seasons >= typical:
        return (float(seasons), float(realized_ppr))
    per_year = realized_ppr / max(1, seasons)
    remaining = typical - seasons
    extrapolated_seasons = seasons + remaining * STILL_ACTIVE_TAIL_DISCOUNT
    extrapolated_ppr = realized_ppr + per_year * remaining * STILL_ACTIVE_TAIL_DISCOUNT
    return (extrapolated_seasons, extrapolated_ppr)


# ---------------------------------------------------------------------------
# Comparables search
# ---------------------------------------------------------------------------

def _class_distance_weight(query: str, comp: str) -> float:
    """1.0 same class, 0.7 neighbor class, 0 otherwise."""
    if not query or not comp:
        return SAME_CLASS_WEIGHT  # missing class info -> don't penalize
    order = ["FR", "SO", "JR", "SR"]
    try:
        qi = order.index(query)
        ci = order.index(comp)
    except ValueError:
        return SAME_CLASS_WEIGHT
    d = abs(qi - ci)
    if d == 0:
        return SAME_CLASS_WEIGHT
    if d == 1:
        return NEIGHBOR_CLASS_WEIGHT
    return 0.0


def find_college_comparables(
    query: CollegePlayerSeason,
    corpus: list[CollegePlayerSeason],
    stats: dict,
    bridge: dict,
    nfl_career_idx: dict,
    k: int = DEFAULT_K,
    exclude_same_player: bool = True,
) -> list[CollegeComparable]:
    """Top-k college comparable seasons for the query, with NFL careers
    resolved via the bridge."""
    qvec = vectorize_college_football_season(query, stats)
    candidates: list[tuple[float, CollegePlayerSeason]] = []

    for ps in corpus:
        if ps.position != query.position:
            continue
        if ps.season >= query.season:
            continue  # only look at strictly-historical comps
        if exclude_same_player and ps.cfb_player_id == query.cfb_player_id:
            continue
        class_w = _class_distance_weight(query.class_year, ps.class_year)
        if class_w <= 0:
            continue
        sim = cosine_similarity(qvec, vectorize_college_football_season(ps, stats))
        # Down-weight neighbor-class comps so similarity ranking favors
        # same-class first.
        sim_eff = sim * class_w
        candidates.append((sim_eff, ps))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:k]

    # Max known NFL season for the "still active" check.
    max_known_nfl_season = max(
        (c.get("last_season", 0) for c in nfl_career_idx.values()), default=2024
    )

    comps: list[CollegeComparable] = []
    for sim, ps in top:
        b = bridge.get(ps.cfb_player_id) or {}
        nfl_pid = b.get("nfl_pfr_player_id")
        career = nfl_career_idx.get(nfl_pid or "") or {}
        if career:
            ext_seasons, ext_ppr = _extrapolated_career_totals(
                career, query.position, max_known_nfl_season
            )
        else:
            ext_seasons, ext_ppr = 0.0, 0.0
        comps.append(CollegeComparable(
            comp_name=ps.player_name,
            comp_cfb_player_id=ps.cfb_player_id,
            comp_school=ps.school,
            comp_season=ps.season,
            comp_class_year=ps.class_year,
            similarity=round(sim, 4),
            nfl_player_id=nfl_pid,
            nfl_display_name=b.get("nfl_display_name"),
            realized_nfl_seasons=int(round(ext_seasons)),
            realized_career_ppr=round(ext_ppr, 1),
            realized_career_standard=round(career.get("career_standard", 0.0), 1),
            last_nfl_season=career.get("last_season"),
            out_of_nfl_after_college=not bool(nfl_pid and career),
        ))
    return comps


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _weighted_avg(values: list[float], weights: list[float]) -> float:
    tw = sum(weights)
    if tw <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / tw


def project_rookie(
    query: CollegePlayerSeason,
    corpus: list[CollegePlayerSeason],
    stats: dict,
    bridge: dict,
    nfl_career_idx: dict,
    k: int = DEFAULT_K,
) -> RookieProjection:
    comps = find_college_comparables(
        query, corpus, stats, bridge, nfl_career_idx, k=k
    )
    if not comps:
        return RookieProjection(
            cfb_player_id=query.cfb_player_id,
            player_name=query.player_name,
            position=query.position,
            school=query.school,
            query_season=query.season,
            class_year=query.class_year,
            n_comps=0,
            n_comps_with_nfl=0,
            avg_similarity=0.0,
            projected_career_seasons=0.0,
            projected_lifetime_fantasy_points=0.0,
            projected_discounted_ppr=0.0,
            nfl_hit_rate=0.0,
            rookie_dynasty_value=0.0,
            comparables_top5=[],
        )

    weights = [max(0.0, c.similarity) for c in comps]
    sim_avg = sum(c.similarity for c in comps) / len(comps)

    # NFL-hit rate -- weighted fraction of comps with a non-zero NFL career.
    hit_flags = [0.0 if c.out_of_nfl_after_college else 1.0 for c in comps]
    nfl_hit_rate = _weighted_avg(hit_flags, weights)

    # Career seasons: comps with no NFL career contribute 0.
    career_seasons_vals = [float(c.realized_nfl_seasons) for c in comps]
    proj_seasons = _weighted_avg(career_seasons_vals, weights)

    # Lifetime PPR.
    career_ppr_vals = [float(c.realized_career_ppr) for c in comps]
    proj_ppr = _weighted_avg(career_ppr_vals, weights)

    # Time-discount the projected lifetime total, spread uniformly across
    # the projected seasons.
    n_yrs = max(1.0, proj_seasons)
    per_year = proj_ppr / n_yrs
    discounted = 0.0
    for y in range(1, max(1, int(round(n_yrs))) + 1):
        discounted += per_year / ((1.0 + TIME_DISCOUNT_PER_YEAR) ** y)

    # Dedupe top5 by comp player.
    seen: set[str] = set()
    top5: list[CollegeComparable] = []
    for c in comps:
        if c.comp_cfb_player_id in seen:
            continue
        seen.add(c.comp_cfb_player_id)
        top5.append(c)
        if len(top5) >= 5:
            break

    return RookieProjection(
        cfb_player_id=query.cfb_player_id,
        player_name=query.player_name,
        position=query.position,
        school=query.school,
        query_season=query.season,
        class_year=query.class_year,
        n_comps=len(comps),
        n_comps_with_nfl=sum(1 for c in comps if not c.out_of_nfl_after_college),
        avg_similarity=round(sim_avg, 4),
        projected_career_seasons=round(proj_seasons, 2),
        projected_lifetime_fantasy_points=round(proj_ppr, 1),
        projected_discounted_ppr=round(discounted, 1),
        nfl_hit_rate=round(nfl_hit_rate, 4),
        rookie_dynasty_value=0.0,   # filled in by rescale step
        comparables_top5=top5,
    )


def rescale_rookie_values(projections: list[RookieProjection]) -> list[RookieProjection]:
    """Rescale per-position projected_discounted_ppr into 0..100."""
    by_pos: dict[str, list[RookieProjection]] = {}
    for p in projections:
        by_pos.setdefault(p.position, []).append(p)

    out: list[RookieProjection] = []
    for pos, group in by_pos.items():
        vals = [g.projected_discounted_ppr for g in group]
        top = max(vals) if vals else 1.0
        top = top or 1.0
        for g in group:
            dv = 100.0 * g.projected_discounted_ppr / top if top > 0 else 0.0
            out.append(
                RookieProjection(
                    cfb_player_id=g.cfb_player_id,
                    player_name=g.player_name,
                    position=g.position,
                    school=g.school,
                    query_season=g.query_season,
                    class_year=g.class_year,
                    n_comps=g.n_comps,
                    n_comps_with_nfl=g.n_comps_with_nfl,
                    avg_similarity=g.avg_similarity,
                    projected_career_seasons=g.projected_career_seasons,
                    projected_lifetime_fantasy_points=g.projected_lifetime_fantasy_points,
                    projected_discounted_ppr=g.projected_discounted_ppr,
                    nfl_hit_rate=g.nfl_hit_rate,
                    rookie_dynasty_value=round(dv, 2),
                    comparables_top5=g.comparables_top5,
                )
            )
    return out


def latest_college_season_per_player(
    corpus: list[CollegePlayerSeason],
) -> dict[str, CollegePlayerSeason]:
    """Pick the most recent college season per cfb_player_id."""
    by_pid: dict[str, CollegePlayerSeason] = {}
    for ps in corpus:
        existing = by_pid.get(ps.cfb_player_id)
        if not existing or ps.season > existing.season:
            by_pid[ps.cfb_player_id] = ps
    return by_pid


def project_all_rookies(
    corpus: Optional[list[CollegePlayerSeason]] = None,
    bridge: Optional[dict] = None,
    nfl_career_idx: Optional[dict] = None,
    min_query_season: int = 2020,
    k: int = DEFAULT_K,
) -> list[RookieProjection]:
    """Build projections for every recent college prospect.

    "Recent" defaults to the last college season being >= 2020. We don't
    need to project Justin Herbert 2019 as a rookie (he's a 5yr NFL vet);
    we DO want to project the 2024 and 2025 college classes who'll enter
    the NFL as 2025/2026 rookies.
    """
    corpus = corpus if corpus is not None else build_college_corpus()
    stats = compute_college_zscore_stats(corpus)
    bridge = bridge if bridge is not None else load_bridge()
    nfl_career_idx = nfl_career_idx if nfl_career_idx is not None else _build_nfl_career_index()

    latest = latest_college_season_per_player(corpus)
    projections: list[RookieProjection] = []
    for pid, ps in latest.items():
        if ps.season < min_query_season:
            continue
        proj = project_rookie(ps, corpus, stats, bridge, nfl_career_idx, k=k)
        projections.append(proj)
    return rescale_rookie_values(projections)


# ---------------------------------------------------------------------------
# Convenience: a single-query helper used by the test suite.
# ---------------------------------------------------------------------------

def project_one_by_name_and_season(
    name_substring: str,
    season: int,
    corpus: Optional[list[CollegePlayerSeason]] = None,
    bridge: Optional[dict] = None,
    nfl_career_idx: Optional[dict] = None,
    k: int = DEFAULT_K,
) -> Optional[RookieProjection]:
    """Find the first CollegePlayerSeason whose name contains ``name_substring``
    in the given season, and return its rookie projection."""
    corpus = corpus if corpus is not None else build_college_corpus()
    stats = compute_college_zscore_stats(corpus)
    bridge = bridge if bridge is not None else load_bridge()
    nfl_career_idx = nfl_career_idx if nfl_career_idx is not None else _build_nfl_career_index()

    for ps in corpus:
        if ps.season != season:
            continue
        if name_substring.lower() in ps.player_name.lower():
            return project_rookie(ps, corpus, stats, bridge, nfl_career_idx, k=k)
    return None
