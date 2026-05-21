"""similarity_career_arc — the new DOMINANT signal in the composite.

This source wraps :mod:`dynasty.similarity.projection` as a ``BaseSource``
adapter so the existing scoring pipeline can ingest its output without
special-casing.

For each active NFL player (any player with a season >= 2023 and
sufficient games), it:

  1. Looks up the top-20 historical comp seasons at the same position
     and similar age.
  2. Aggregates their realized future careers, time-discounted at 5%/yr.
  3. Emits a 0..100 dynasty value (per position) plus the top-5 comps
     as JSON for the UI.

The composite weights this source heavily (1.8, vs 0.8 for nfl_impact)
because it encodes both current skill (via the vectorization features)
and longevity (via the comp careers). Phil's directive:

  "Let's make similarity scores the heart of the model."

This is that heart.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .base import BaseSource, RankingRecord


# Cache the comparables JSON for the UI to render the comp tables.
_REPO_ROOT = Path(__file__).resolve().parents[3]
COMPS_CACHE = _REPO_ROOT / "data" / "similarity_comps_cache.json"


def _build_comps_cache(projections) -> None:
    """Write the per-player comp list as JSON for the report builder to read."""
    out = {}
    for p in projections:
        out[p.player_id] = {
            "player_id": p.player_id,
            "player_name": p.player_name,
            "position": p.position,
            "query_season": p.query_season,
            "query_age": p.query_age,
            "n_comps": p.n_comps,
            "avg_similarity": p.avg_similarity,
            "projected_remaining_years": p.projected_remaining_years,
            "projected_total_remaining_ppr": p.projected_total_remaining_ppr,
            "dynasty_value": p.dynasty_value,
            "comparables": [
                {
                    "name": c.comp_name,
                    "team_or_school": c.comp_team_or_school,
                    "season": c.comp_season,
                    "age": c.comp_age,
                    "similarity": c.similarity,
                    "remaining_seasons": c.remaining_seasons,
                    "years_played_after": c.years_played_after,
                }
                for c in p.comparables
            ],
        }
    COMPS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    COMPS_CACHE.write_text(json.dumps(out, indent=2))


def load_comps_cache() -> dict:
    """Read the saved comparables JSON (used by the site renderer)."""
    if not COMPS_CACHE.exists():
        return {}
    try:
        return json.loads(COMPS_CACHE.read_text())
    except json.JSONDecodeError:
        return {}


class SimilarityCareerArc(BaseSource):
    slug = "similarity_career_arc"
    name = "Similarity Career Arc"
    category = "model"
    update_frequency = "weekly"
    tos_compliant = True
    # DOMINANT weight — this is the heart of the v0.14 model per Phil.
    default_weight = 1.8
    homepage = "internal: src/dynasty/similarity/"
    notes = (
        "KNN comparables over the PFR / nflverse player-season corpus. "
        "Top-20 nearest historical seasons at same position and age "
        "(\u00b11yr), aggregated into a projected remaining career value "
        "(time-discounted 5%/yr). Encodes both current skill AND "
        "longevity \u2014 the latter is what makes younger players "
        "rightly more valuable in dynasty."
    )

    def fetch(self) -> Iterator[RankingRecord]:
        # Lazy import so importing the source registry doesn't load the
        # full PFR corpus.
        from ..similarity.projection import project_all_active_players

        projections = project_all_active_players()
        _build_comps_cache(projections)

        # Rank overall and per-position by dynasty_value
        projections_sorted = sorted(projections, key=lambda p: p.dynasty_value, reverse=True)
        overall_rank = {p.player_id: i + 1 for i, p in enumerate(projections_sorted)}
        pos_counters: dict[str, int] = {}
        pos_rank: dict[str, int] = {}
        for p in projections_sorted:
            pos_counters[p.position] = pos_counters.get(p.position, 0) + 1
            pos_rank[p.player_id] = pos_counters[p.position]

        captured = datetime.utcnow()
        for p in projections:
            for fmt in ("sf_ppr", "1qb_ppr"):
                yield RankingRecord(
                    source_slug=self.slug,
                    gsis_id=p.player_id,
                    full_name=p.player_name,
                    position=p.position,
                    overall_rank=overall_rank.get(p.player_id),
                    position_rank=pos_rank.get(p.player_id),
                    market_value=p.dynasty_value,
                    league_format=fmt,
                    is_dynasty=True,
                    captured_at=captured,
                )
