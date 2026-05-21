"""similarity_career_arc — the DOMINANT signal in the composite.

This source wraps :mod:`dynasty.similarity.projection` as a ``BaseSource``
adapter so the existing scoring pipeline can ingest its output without
special-casing.

For each active NFL player (any player with a season >= 2023 and
sufficient games), it:

  1. Looks up the top-20 historical comp seasons at the same position
     and similar age.
  2. RE-SCORES each comp's remaining career under the active league
     format's scoring rules (v0.15.0 — was format-blind in v0.14).
  3. Aggregates the rescored future careers, time-discounted at 5%/yr.
  4. Converts to positional VORP (subtract replacement baseline, apply
     scarcity-cliff multiplier).
  5. Emits a 0..100 dynasty value (across all positions) plus the
     top-5 comps as JSON for the UI.

In v0.14 this source used a single projection set across formats. In
v0.15 we run the projection ONCE PER FORMAT because the format
fundamentally changes replacement baselines and per-stat-line scoring
(see ``dynasty.scoring_rules``).

The composite weights this source heavily (1.8 baseline) and applies
an additional per-(format, position) multiplier in
``composite_weights.py`` so QBs get the SF premium they deserve.

Phil's directive (2026-05-21):

  "Mahomes and Josh Allen for example are extremely valuable in a
   superflex league where you have to start 1 QB, and more often you
   are starting a QB in the superflex spot."

That premium lives here.
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
VORP_DEBUG_CACHE = _REPO_ROOT / "data" / "similarity_vorp_debug.json"


# Formats the projection runs for. Adding a format here is enough to
# get rankings for it; the composite scorer and report builder will
# pick them up automatically.
PROJECTION_FORMATS = ("sf_ppr", "1qb_ppr")


def _build_comps_cache(projections_by_format: dict) -> None:
    """Write the per-player comp list as JSON for the report builder to read.

    Keyed by player_id with the sf_ppr projection as the canonical
    record (comp tables are visually the same across formats — only
    numerical aggregates change). VORP debug data per-format lives
    in a separate JSON.
    """
    canonical = projections_by_format.get("sf_ppr") or next(iter(projections_by_format.values()), [])
    out = {}
    for p in canonical:
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


def _build_vorp_debug_cache(projections_by_format: dict) -> None:
    """Persist per-format VORP / scarcity diagnostics for the
    methodology page + tests. Includes per-position baselines and
    multipliers + a sample of top-25 by VORP.
    """
    debug = {}
    for fmt, projections in projections_by_format.items():
        if not projections:
            continue
        # Aggregate per-position diagnostics from the projections (they
        # all share the same baselines/multipliers within a format).
        by_pos_baseline: dict[str, float] = {}
        by_pos_mult: dict[str, float] = {}
        by_pos_count: dict[str, int] = {}
        for p in projections:
            by_pos_baseline.setdefault(p.position, p.replacement_baseline)
            by_pos_mult.setdefault(p.position, p.scarcity_multiplier)
            by_pos_count[p.position] = by_pos_count.get(p.position, 0) + 1
        top_by_vorp = sorted(projections, key=lambda p: p.vorp, reverse=True)[:25]
        debug[fmt] = {
            "league_format": fmt,
            "n_players_projected": len(projections),
            "per_position": {
                pos: {
                    "replacement_baseline": round(by_pos_baseline.get(pos, 0.0), 1),
                    "scarcity_multiplier": round(by_pos_mult.get(pos, 1.0), 3),
                    "n_players": by_pos_count.get(pos, 0),
                }
                for pos in ("QB", "RB", "WR", "TE")
            },
            "top_25_by_vorp": [
                {
                    "name": p.player_name,
                    "position": p.position,
                    "vorp": p.vorp,
                    "dynasty_value": p.dynasty_value,
                    "discounted_ppr": p.projected_discounted_ppr,
                    "baseline": p.replacement_baseline,
                    "scarcity_mult": p.scarcity_multiplier,
                }
                for p in top_by_vorp
            ],
        }
    VORP_DEBUG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    VORP_DEBUG_CACHE.write_text(json.dumps(debug, indent=2))


def load_comps_cache() -> dict:
    """Read the saved comparables JSON (used by the site renderer)."""
    if not COMPS_CACHE.exists():
        return {}
    try:
        return json.loads(COMPS_CACHE.read_text())
    except json.JSONDecodeError:
        return {}


def load_vorp_debug() -> dict:
    """Read the VORP diagnostics JSON (used by the methodology page)."""
    if not VORP_DEBUG_CACHE.exists():
        return {}
    try:
        return json.loads(VORP_DEBUG_CACHE.read_text())
    except json.JSONDecodeError:
        return {}


class SimilarityCareerArc(BaseSource):
    slug = "similarity_career_arc"
    name = "Similarity Career Arc"
    category = "model"
    update_frequency = "weekly"
    tos_compliant = True
    # DOMINANT weight — this is the heart of the v0.14+ model per Phil.
    default_weight = 1.8
    homepage = "internal: src/dynasty/similarity/"
    notes = (
        "KNN comparables over the PFR / nflverse player-season corpus. "
        "Top-20 nearest historical seasons at same position and age "
        "(\u00b11yr), aggregated into a projected remaining career value "
        "(time-discounted 5%/yr). v0.15.0: format-aware — comp seasons "
        "are re-scored under the active league_format's rules and "
        "converted to positional VORP using format-specific replacement "
        "baselines (QB24 in SF, QB12 in 1QB)."
    )

    def fetch(self) -> Iterator[RankingRecord]:
        # Lazy import so importing the source registry doesn't load the
        # full PFR corpus.
        from ..similarity.projection import (
            project_all_active_players,
            build_nfl_corpus,
        )

        # Build the corpus ONCE and reuse it across formats. The
        # per-format work is cheap (re-scoring + VORP rescale) once the
        # KNN search is done; keep the expensive corpus load shared.
        corpus = build_nfl_corpus()
        projections_by_format: dict[str, list] = {}
        for fmt in PROJECTION_FORMATS:
            projections_by_format[fmt] = project_all_active_players(
                corpus=corpus,
                league_format=fmt,
            )

        _build_comps_cache(projections_by_format)
        _build_vorp_debug_cache(projections_by_format)

        captured = datetime.utcnow()
        for fmt, projections in projections_by_format.items():
            # Rank overall and per-position by dynasty_value within this format.
            sorted_pool = sorted(projections, key=lambda p: p.dynasty_value, reverse=True)
            overall_rank = {p.player_id: i + 1 for i, p in enumerate(sorted_pool)}
            pos_counters: dict[str, int] = {}
            pos_rank: dict[str, int] = {}
            for p in sorted_pool:
                pos_counters[p.position] = pos_counters.get(p.position, 0) + 1
                pos_rank[p.player_id] = pos_counters[p.position]

            for p in projections:
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
