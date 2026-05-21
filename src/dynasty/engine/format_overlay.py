"""Format overlay — rescores the engine's comp projections under user-defined
fantasy scoring + roster settings.

The default engine output (``similarity_v1.run_engine``) ranks every active
player under the PPR-default scoring table. The format overlay takes those
same comps and re-runs the projection pass with a custom scoring dict, then
recomputes a VORP-style league-specific value using the supplied roster
settings.

This module is **stateless** w.r.t. the engine: pass in the EngineResult, get
back a list of dicts with overlay-adjusted ranks.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .similarity_v1 import (
    DEFAULT_SCORING,
    DISCOUNT_PER_YEAR,
    EngineResult,
    _project_comp_post_age,
)


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
        # 2QB starts two real QBs (no flex SF), slightly higher QB premium
        # than SF because you cannot fill QB2 with a RB/WR/TE.
        "roster": {"QB": 2, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SF": 0, "teams": 12},
    },
    "sf_te_premium": {
        "label": "Superflex TE-Premium PPR",
        "scoring": {**DEFAULT_SCORING},
        "roster": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SF": 1, "teams": 12},
    },
}
# TE-premium: +0.5 PPR for TEs is handled inside apply_overlay via a TE bonus
# (per_reception bonus). We don't model per-position scoring as separate dicts;
# instead the overlay applies the bonus on TE comp projections.
PRESETS["sf_te_premium"]["te_reception_bonus"] = 0.5


@dataclass
class OverlayResult:
    league_format: str
    label: str
    rankings: List[Dict] = field(default_factory=list)  # sorted by overlay value desc
    replacement_baseline: Dict[str, float] = field(default_factory=dict)


def _starters_per_position(roster: Dict[str, int]) -> Dict[str, float]:
    """Effective starters across the league per position.

    SF and FLEX slots are split heuristically:
      - SF: 0.85 QB / 0.05 RB / 0.05 WR / 0.05 TE
      - FLEX: 0.40 RB / 0.50 WR / 0.10 TE
    """
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


def apply_overlay(
    engine: EngineResult,
    league_format: str = "sf_ppr",
    scoring_overrides: Optional[Dict[str, float]] = None,
    roster_overrides: Optional[Dict[str, int]] = None,
    te_reception_bonus: float = 0.0,
) -> OverlayResult:
    preset = PRESETS.get(league_format, PRESETS["sf_ppr"])
    scoring = dict(preset["scoring"])
    if scoring_overrides:
        scoring.update(scoring_overrides)
    roster = dict(preset["roster"])
    if roster_overrides:
        roster.update(roster_overrides)
    te_bonus = te_reception_bonus or preset.get("te_reception_bonus", 0.0)

    # 1) Re-project every active player's top-comp pool under this format.
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
        weighted_points = 0.0
        for c in comp_recs:
            comp = careers_by_id.get(c["player_id"])
            if comp is None:
                continue
            local_scoring = dict(scoring)
            # TE premium: extra per-reception on TE comps only.
            if comp.position == "TE" and te_bonus:
                local_scoring["receptions"] = (
                    local_scoring.get("receptions", 0.0) + te_bonus
                )
            pts, _ = _project_comp_post_age(
                comp, age_floor=age_now,
                pace=engine.era_pace, scoring=local_scoring,
            )
            weighted_points += pts * (c["similarity"] / total_sim)
        re_projected.append({
            "player_id": pid,
            "name": row["name"],
            "position": row["position"],
            "age": row["age"],
            "production_score_overlay": round(weighted_points, 1),
            "production_score_default": row["production_score"],
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
        n = int(round(starters.get(pos, 12))) + 6  # replacement = last starter + waiver buffer
        if not players:
            baselines[pos] = 0.0
            continue
        idx = min(n, len(players) - 1)
        baselines[pos] = players[idx]["production_score_overlay"]

    # 3) Compute league value = production - baseline (positional VORP).
    for r in re_projected:
        baseline = baselines.get(r["position"], 0.0)
        r["league_value"] = round(r["production_score_overlay"] - baseline, 1)

    re_projected.sort(key=lambda r: r["league_value"], reverse=True)
    for i, r in enumerate(re_projected):
        r["overall_rank"] = i + 1

    # 4) Default-overlay rank delta. Map original (default-format) rank.
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
