"""rookie_similarity_chain -- the rookie/prospect signal in the composite.

This source wraps :mod:`dynasty.similarity.rookie_projection` as a
``BaseSource`` adapter, in parallel to ``similarity_career_arc`` for veteran
NFL players. Together the two sources cover the whole player universe:

  * ``similarity_career_arc`` (PR #14)  -- for players with >= 2 NFL seasons
  * ``rookie_similarity_chain`` (PR #16) -- for pure rookies / draft
                                            prospects (0 NFL seasons) and
                                            blends 50/50 with the NFL value
                                            for players with exactly 1 NFL
                                            season.

Blend handling: this adapter EMITS the blended value already. It looks up
each rookie/2nd-year NFL prospect by name+college via the
``dynasty.similarity.bridge`` crosswalk, reads the realized NFL career
length, and decides:

  * 0 NFL seasons -> emit raw ``rookie_dynasty_value``
  * 1 NFL season  -> blend rookie_dynasty_value 0.5 + nfl_dynasty_value 0.5
                     (the NFL value is read from the existing
                     ``similarity_comps_cache.json`` from PR #14)
  * >= 2 NFL seasons -> do not emit (the PR #14 source already covers
                       them as ``similarity_career_arc``).

The composite weights this source at the same tier as
``similarity_career_arc`` (1.6) since it serves the same purpose for the
younger half of the player universe.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .base import BaseSource, RankingRecord
from .pro_football_reference import load_pfr_seasons


# Cache the rookie comparables JSON for the UI to render the comp tables.
_REPO_ROOT = Path(__file__).resolve().parents[3]
ROOKIE_COMPS_CACHE = _REPO_ROOT / "data" / "rookie_similarity_comps_cache.json"
# Reuse PR #14's NFL cache to blend.
NFL_COMPS_CACHE = _REPO_ROOT / "data" / "similarity_comps_cache.json"


def _build_comps_cache(projections) -> None:
    out = {}
    for p in projections:
        out[p.cfb_player_id] = {
            "cfb_player_id": p.cfb_player_id,
            "player_name": p.player_name,
            "position": p.position,
            "school": p.school,
            "query_season": p.query_season,
            "class_year": p.class_year,
            "n_comps": p.n_comps,
            "n_comps_with_nfl": p.n_comps_with_nfl,
            "avg_similarity": p.avg_similarity,
            "projected_career_seasons": p.projected_career_seasons,
            "projected_lifetime_fantasy_points": p.projected_lifetime_fantasy_points,
            "nfl_hit_rate": p.nfl_hit_rate,
            "rookie_dynasty_value": p.rookie_dynasty_value,
            "comparables_top5": [
                {
                    "name": c.comp_name,
                    "school": c.comp_school,
                    "class_year": c.comp_class_year,
                    "season": c.comp_season,
                    "similarity": c.similarity,
                    "nfl_player_id": c.nfl_player_id,
                    "nfl_display_name": c.nfl_display_name,
                    "realized_nfl_seasons": c.realized_nfl_seasons,
                    "realized_career_ppr": c.realized_career_ppr,
                    "out_of_nfl_after_college": c.out_of_nfl_after_college,
                }
                for c in p.comparables_top5
            ],
        }
    ROOKIE_COMPS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ROOKIE_COMPS_CACHE.write_text(json.dumps(out, indent=2))


def load_rookie_comps_cache() -> dict:
    if not ROOKIE_COMPS_CACHE.exists():
        return {}
    try:
        return json.loads(ROOKIE_COMPS_CACHE.read_text())
    except json.JSONDecodeError:
        return {}


def _nfl_seasons_played_by_gsis() -> dict[str, int]:
    """Return {gsis_id: count of NFL seasons played}."""
    out: Counter = Counter()
    for r in load_pfr_seasons():
        pid = r.get("player_id") or ""
        if not pid:
            continue
        try:
            int(r["season"])
        except (KeyError, ValueError, TypeError):
            continue
        out[pid] += 1
    return dict(out)


def _nfl_dynasty_value(comps_cache: dict, gsis_id: str) -> Optional[float]:
    """Look up the NFL similarity dynasty_value for ``gsis_id``."""
    entry = comps_cache.get(gsis_id)
    if not entry:
        return None
    dv = entry.get("dynasty_value")
    if dv is None:
        return None
    try:
        return float(dv)
    except (ValueError, TypeError):
        return None


class RookieSimilarityChain(BaseSource):
    slug = "rookie_similarity_chain"
    name = "Rookie Similarity Chain (college -> NFL)"
    category = "model"
    update_frequency = "weekly"
    tos_compliant = True
    # Slightly below similarity_career_arc since rookies are inherently
    # noisier (smaller sample of bridged careers, esp for 2024+ classes).
    # Still a top-tier signal for the rookie/prospect cohort it covers.
    default_weight = 1.6
    homepage = "internal: src/dynasty/similarity/rookie_projection.py"
    notes = (
        "College->NFL similarity chain. For each rookie/prospect, finds "
        "top-20 college comparables at same position/class, resolves "
        "each to their realized NFL career via the ncaa_to_nfl bridge, "
        "and aggregates a discounted lifetime fantasy projection. Blends "
        "50/50 with similarity_career_arc for players with exactly 1 NFL "
        "season; pure for 0-NFL-season prospects; no emission for "
        "players with >= 2 NFL seasons (handled by similarity_career_arc)."
    )

    LEAGUE_FORMATS = ("sf_ppr", "1qb_ppr")

    def fetch(self) -> Iterator[RankingRecord]:
        # Lazy import so the source registry doesn't load the NCAA corpus.
        from ..similarity.rookie_projection import project_all_rookies

        projections = project_all_rookies()
        _build_comps_cache(projections)

        # Load PR #14 NFL cache for the 1-NFL-season blend.
        nfl_cache: dict = {}
        if NFL_COMPS_CACHE.exists():
            try:
                nfl_cache = json.loads(NFL_COMPS_CACHE.read_text())
            except json.JSONDecodeError:
                nfl_cache = {}

        # Bridge -> NFL season counts to decide blend tier.
        from ..similarity.bridge import load_bridge
        bridge = load_bridge()
        # Build cfb_player_id -> nfl_pfr_player_id
        cfb_to_nfl = {
            pid: e["nfl_pfr_player_id"]
            for pid, e in bridge.items()
            if e.get("nfl_pfr_player_id")
        }
        nfl_seasons = _nfl_seasons_played_by_gsis()

        # Rank overall (after blend) and per-position
        scored: list[tuple[float, object]] = []
        for p in projections:
            nfl_pid = cfb_to_nfl.get(p.cfb_player_id)
            n_nfl = nfl_seasons.get(nfl_pid or "", 0)

            if n_nfl >= 2:
                # PR #14's similarity_career_arc owns this player.
                continue
            if n_nfl == 1:
                nfl_dv = _nfl_dynasty_value(nfl_cache, nfl_pid or "") or 0.0
                blended = 0.5 * p.rookie_dynasty_value + 0.5 * nfl_dv
            else:
                blended = p.rookie_dynasty_value
            scored.append((blended, p, nfl_pid, n_nfl))

        scored.sort(key=lambda x: x[0], reverse=True)
        overall_rank = {p.cfb_player_id: i + 1 for i, (_, p, _, _) in enumerate(scored)}
        pos_counters: dict[str, int] = {}
        pos_rank: dict[str, int] = {}
        for _, p, _, _ in scored:
            pos_counters[p.position] = pos_counters.get(p.position, 0) + 1
            pos_rank[p.cfb_player_id] = pos_counters[p.position]

        captured = datetime.utcnow()
        for value, p, nfl_pid, n_nfl in scored:
            for fmt in self.LEAGUE_FORMATS:
                yield RankingRecord(
                    source_slug=self.slug,
                    gsis_id=nfl_pid,
                    full_name=p.player_name,
                    position=p.position,
                    college=p.school,
                    overall_rank=overall_rank.get(p.cfb_player_id),
                    position_rank=pos_rank.get(p.cfb_player_id),
                    market_value=value,
                    league_format=fmt,
                    is_dynasty=True,
                    is_rookie_only=(n_nfl == 0),
                    captured_at=captured,
                )
