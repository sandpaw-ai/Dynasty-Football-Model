"""Format overlay — rescores the engine's comp projections under user-defined
fantasy scoring + roster settings.

v2.0 rewrite: the base engine produces ONE master ranking under sf_ppr. The
overlay layer re-projects under each league format by tapping into the
fantasy-point arc corpus directly (each player-season's fp_total under each
format is pre-computed at corpus build time — see
``fantasy_arc.build_career_arc``).

This module is **stateless** w.r.t. the engine: pass in the EngineResult, get
back a list of dicts with overlay-adjusted ranks under the requested format.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .career_length_era import (
    STYLE_POCKET,
    apply_lift,
    style_for_career,
)
from .fantasy_arc_similarity import (
    BASE_FORMAT,
    DISCOUNT_PER_YEAR,
    ELITE_PEAK_THRESHOLDS,
    MIN_GAMES_PER_SEASON,
    PEAK_ANCHOR_MIN_COMPS,
    PEAK_ANCHORED_DISCOUNT,
    PROJECTION_GAMES_PER_SEASON,
    SOFT_BAND,
    _projection_rate,
)
from .similarity_v1 import (
    DEFAULT_SCORING,
    EngineResult,
)

# Current era is era 4 (2020+). The lift is applied per-style at this era.
_CURRENT_ERA = 4


# Roster setting presets (starters per team; bench/depth not modeled).
PRESETS: Dict[str, Dict] = {
    "sf_ppr": {
        "label": "Superflex PPR",
        "scoring": dict(DEFAULT_SCORING),
        "roster": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SF": 1, "teams": 12},
    },
    "1qb_ppr": {
        "label": "1QB PPR",
        "scoring": dict(DEFAULT_SCORING),
        "roster": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SF": 0, "teams": 12},
    },
    "2qb_ppr": {
        "label": "2QB PPR",
        "scoring": dict(DEFAULT_SCORING),
        "roster": {"QB": 2, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SF": 0, "teams": 12},
    },
    "sf_te_premium": {
        "label": "Superflex TE-Premium PPR",
        "scoring": {**DEFAULT_SCORING},
        "roster": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SF": 1, "teams": 12},
        "te_reception_bonus": 0.5,
    },
}


@dataclass
class OverlayResult:
    league_format: str
    label: str
    rankings: List[Dict] = field(default_factory=list)
    replacement_baseline: Dict[str, float] = field(default_factory=dict)


def _starters_per_position(roster: Dict[str, int]) -> Dict[str, float]:
    teams = roster.get("teams", 12)
    qb = roster.get("QB", 1)
    rb = roster.get("RB", 2)
    wr = roster.get("WR", 3)
    te = roster.get("TE", 1)
    flex = roster.get("FLEX", 0)
    sf = roster.get("SF", 0)

    starters = {
        "QB": qb + sf * 0.85,
        "RB": rb + flex * 0.40 + sf * 0.05,
        "WR": wr + flex * 0.50 + sf * 0.05,
        "TE": te + flex * 0.10 + sf * 0.05,
    }
    return {pos: count * teams for pos, count in starters.items()}


def _project_comp_under_format(
    engine: EngineResult,
    comp_player_id: str,
    snapshot_age: int,
    league_format: str,
) -> float:
    """Look up the comp's arc and sum its post-snapshot_age fantasy points
    under the requested format. Time-discounted at DISCOUNT_PER_YEAR/yr."""
    arc = engine.arcs.get(comp_player_id) if engine.arcs else None
    if arc is None:
        return 0.0
    total = 0.0
    n = 0
    for s in arc.career_arc:
        if s.age <= snapshot_age:
            continue
        if s.games < MIN_GAMES_PER_SEASON:
            continue
        pts = s.fp_total.get(league_format, 0.0)
        pts *= (1.0 - DISCOUNT_PER_YEAR) ** n
        total += pts
        n += 1
    return total


def apply_overlay(
    engine: EngineResult,
    league_format: str = "sf_ppr",
    scoring_overrides: Optional[Dict[str, float]] = None,
    roster_overrides: Optional[Dict[str, int]] = None,
    te_reception_bonus: float = 0.0,
) -> OverlayResult:
    preset = PRESETS.get(league_format, PRESETS["sf_ppr"])
    roster = dict(preset["roster"])
    if roster_overrides:
        roster.update(roster_overrides)
    # ``scoring_overrides`` and ``te_reception_bonus`` are accepted for
    # back-compat but v2.0's overlay always reads the pre-computed
    # per-format fp from the arc corpus. Honour ``te_reception_bonus`` for
    # custom callers by mapping to sf_te_premium when set.
    effective_format = league_format
    if league_format == "sf_te_premium" or (te_reception_bonus and te_reception_bonus > 0):
        effective_format = "sf_te_premium"

    # 1) Re-project every active player's comp pool under this format.
    re_projected: List[Dict] = []
    careers_by_id = engine.careers
    for row in engine.rankings:
        pid = row["player_id"]
        ap = careers_by_id.get(pid)
        if ap is None or not ap.seasons:
            continue
        age_now = ap.seasons[-1].age
        comp_recs = engine.comps.get(pid, [])
        if not comp_recs:
            continue
        total_sim = sum(c["similarity"] for c in comp_recs) or 1.0
        comp_weighted_pts = 0.0
        comp_weighted_seasons = 0.0
        for c in comp_recs:
            snapshot_age = c.get("snapshot_age", age_now)
            pts = _project_comp_under_format(
                engine, c["player_id"], snapshot_age, effective_format,
            )
            n_seasons = max(c.get("post_age_seasons", 0), 0)
            w = c["similarity"] / total_sim
            comp_weighted_pts += pts * w
            comp_weighted_seasons += n_seasons * w

        # Peak-anchored projection under this format. Use the target's
        # projection_rate (blend of peak-3yr and recent-3yr) under THIS
        # format, derived from the pre-computed arc corpus.
        target_arc = engine.arcs.get(pid) if engine.arcs else None
        target_peak = 0.0
        projection_rate = 0.0
        if target_arc is not None:
            target_peak = target_arc.peak_3yr_fp_per_game.get(effective_format, 0.0)
            projection_rate = _projection_rate(target_arc, effective_format)
        if (
            comp_weighted_seasons > 0 and projection_rate > 0
            and len(comp_recs) >= PEAK_ANCHOR_MIN_COMPS
        ):
            avg_discount = (1.0 - DISCOUNT_PER_YEAR) ** (comp_weighted_seasons / 2.0)
            peak_anchored = (
                projection_rate * PROJECTION_GAMES_PER_SEASON
                * comp_weighted_seasons * avg_discount
            )
        else:
            peak_anchored = 0.0

        # Same threshold + soft-blend logic as the base engine, gated on
        # all-time peak so a one-time elite peak qualifies even after
        # decline (the projection_rate will be lower because of recent_3yr,
        # so the inflation is naturally tempered).
        threshold = ELITE_PEAK_THRESHOLDS.get(ap.position, 0.0)
        if target_peak >= threshold:
            weighted_points = max(comp_weighted_pts, peak_anchored)
        elif target_peak >= threshold - SOFT_BAND:
            t = (target_peak - (threshold - SOFT_BAND)) / SOFT_BAND
            weighted_points = (
                (1 - t) * comp_weighted_pts
                + t * max(comp_weighted_pts, peak_anchored)
            )
        else:
            weighted_points = comp_weighted_pts

        # v2.0 mild lift on fp (1.10 dual-threat, 1.05 mobile, 1.00 pocket).
        lift = row.get("career_length_lift_fp")
        if lift is None:
            lift = row.get("career_length_lift", 1.0) or 1.0
        weighted_points = apply_lift(weighted_points, float(lift))

        re_projected.append({
            "player_id": pid,
            "name": row["name"],
            "position": row["position"],
            "age": row["age"],
            "production_score_overlay": round(weighted_points, 1),
            "production_score_default": row["production_score"],
            "qb_style": row.get("qb_style"),
            "qb_career_rypg": row.get("qb_career_rypg"),
            "career_length_lift": round(lift, 3),
            # v2.0 projection diagnostics
            "comp_weighted_fp": round(comp_weighted_pts, 1),
            "peak_anchored_fp": round(peak_anchored, 1),
            "projection_path": (
                "peak_anchored" if peak_anchored > comp_weighted_pts
                else "comp_weighted"
            ),
            # v2.0 player-arc metrics carried through for the UI
            "peak_3yr_fp_per_game": row.get("peak_3yr_fp_per_game"),
            "peak_season_fp_per_game": row.get("peak_season_fp_per_game"),
            "career_avg_fp_per_game": row.get("career_avg_fp_per_game"),
            "career_total_fp_to_date": row.get("career_total_fp_to_date"),
        })

    # 2) Compute replacement baselines.
    by_pos: Dict[str, List[Dict]] = defaultdict(list)
    for r in re_projected:
        by_pos[r["position"]].append(r)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda r: r["production_score_overlay"], reverse=True)

    starters = _starters_per_position(roster)
    baselines: Dict[str, float] = {}
    for pos, players in by_pos.items():
        n = int(round(starters.get(pos, 12))) + 6
        if not players:
            baselines[pos] = 0.0
            continue
        idx = min(n, len(players) - 1)
        baselines[pos] = players[idx]["production_score_overlay"]

    # 3) Compute league value = production - baseline.
    for r in re_projected:
        baseline = baselines.get(r["position"], 0.0)
        r["league_value"] = round(r["production_score_overlay"] - baseline, 1)

    re_projected.sort(key=lambda r: r["league_value"], reverse=True)
    for i, r in enumerate(re_projected):
        r["overall_rank"] = i + 1

    default_rank = {row["player_id"]: row["overall_rank"] for row in engine.rankings}
    for r in re_projected:
        old = default_rank.get(r["player_id"])
        if old is None:
            r["vs_default_delta"] = 0
        else:
            r["vs_default_delta"] = old - r["overall_rank"]

    return OverlayResult(
        league_format=league_format,
        label=preset["label"],
        rankings=re_projected,
        replacement_baseline=baselines,
    )


def all_format_overlays(engine: EngineResult) -> Dict[str, OverlayResult]:
    return {
        fmt: apply_overlay(engine, league_format=fmt)
        for fmt in PRESETS
    }
