"""v0.14.0 — similarity engine + composite invariants.

These tests run against the committed PFR / nflverse corpus under
``data/nflverse/``. They do NOT hit the network.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


# Make the package importable from tests/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Point DB at a tmp file BEFORE any dynasty modules import the engine.
# We force-set DATABASE_URL even if another test file already imported
# dynasty.db.session — the engine and SessionLocal get rebuilt by
# re-importing the module so all subsequent `get_session()` calls hit
# the tmp DB. This mirrors the per-file DB isolation that test_manager,
# test_prefetch_leagues, and test_weights already use.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

import importlib  # noqa: E402
import dynasty.config as _config_mod  # noqa: E402
importlib.reload(_config_mod)
import dynasty.db.session as _session_mod  # noqa: E402
importlib.reload(_session_mod)


# ---------------------------------------------------------------------------
# Cache sanity
# ---------------------------------------------------------------------------


def test_pfr_cache_present_and_sane():
    from dynasty.sources.pro_football_reference import cache_summary

    s = cache_summary()
    assert s["n_player_seasons"] > 10_000, "PFR / nflverse cache appears empty"
    assert s["n_players"] > 10_000
    assert s["min_season"] <= 2000
    assert s["max_season"] >= 2024


# ---------------------------------------------------------------------------
# Vectorization determinism
# ---------------------------------------------------------------------------


def test_vectorize_is_deterministic():
    from dynasty.similarity.vectorize import (
        build_nfl_corpus,
        compute_zscore_stats,
        vectorize,
    )

    corpus = build_nfl_corpus()
    stats = compute_zscore_stats(corpus)
    # Re-vectorize the same player twice — must be identical
    sample = next(c for c in corpus if c.player_name == "Justin Jefferson" and c.season == 2020)
    v1 = vectorize(sample, stats)
    v2 = vectorize(sample, stats)
    assert v1 == v2

    # Order-independence: re-shuffle the corpus and zstats must agree on the
    # query vector (within float tolerance)
    import random

    shuffled = corpus[:]
    random.Random(0).shuffle(shuffled)
    stats2 = compute_zscore_stats(shuffled)
    v3 = vectorize(sample, stats2)
    for a, b in zip(v1, v3):
        assert abs(a - b) < 1e-9


# ---------------------------------------------------------------------------
# KNN sensible matches
# ---------------------------------------------------------------------------


def test_young_high_target_wr_matches_other_young_high_target_wrs():
    from dynasty.similarity.vectorize import (
        build_nfl_corpus,
        compute_zscore_stats,
    )
    from dynasty.similarity.comparables import find_comparables

    corpus = build_nfl_corpus()
    stats = compute_zscore_stats(corpus)
    # Justin Jefferson's 2020 rookie season — elite high-target young WR
    jj_2020 = next(c for c in corpus if c.player_name == "Justin Jefferson" and c.season == 2020)
    comps = find_comparables(jj_2020, corpus, stats, k=20)
    # Sanity: top-10 should mostly be high-volume WRs at 21-22yo
    top10_names = [c.comp_name for c in comps[:10]]
    # Should include at least a couple of well-known high-target young WRs.
    # The exact list shifts based on which historical seasons the cosine
    # ranks highest; the invariant is qualitative — the top-10 must be
    # *recognizable productive young WRs*, not random journeymen.
    known_high_target_young_wrs = {
        "Odell Beckham", "A.J. Green", "DeAndre Hopkins", "Mike Evans",
        "Larry Fitzgerald", "Calvin Johnson", "Randy Moss", "Julio Jones",
        "Andre Johnson", "Amari Cooper", "Keenan Allen", "Stefon Diggs",
        "Brandin Cooks", "JuJu Smith-Schuster", "Jeremy Maclin", "Randall Cobb",
        "Percy Harvin", "Sammy Watkins", "DK Metcalf", "Jaylen Waddle",
        "Garrett Wilson", "Chris Olave",
    }
    overlap = set(top10_names) & known_high_target_young_wrs
    assert len(overlap) >= 3, f"expected 3+ known elite young WR comps, got {top10_names}"


# ---------------------------------------------------------------------------
# Composite invariants
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def composite_built():
    """Run the full sync + score pipeline against the module-scoped tmp DB.

    DATABASE_URL is pinned at import time (top of this file) so all
    ``dynasty.*`` imports below see the same engine.
    """
    from dynasty.db.session import init_db
    from dynasty.sync import sync_sleeper_players, sync_source
    from dynasty.scoring import compute_composite_scores

    init_db()
    try:
        sync_sleeper_players()
    except Exception:
        # Network may not be available; the seeded RankingRecords still
        # auto-create Player rows via _resolve_player’s upsert path.
        pass
    for slug in (
        "fantasycalc",
        "dynastyprocess",
        "brainy_ballers",
        "nfl_draft_capital",
        "ras",
        "nfl_impact",
        "similarity_career_arc",
    ):
        try:
            sync_source(slug)
        except Exception:
            pass
    compute_composite_scores(league_format="sf_ppr")
    yield


def _rank_by_name(name: str) -> int | None:
    from dynasty.db.session import get_session
    from dynasty.db.models import Player, CompositeScore
    from sqlalchemy import select

    with get_session() as s:
        row = s.execute(
            select(Player, CompositeScore)
            .join(CompositeScore, Player.id == CompositeScore.player_id)
            .where(Player.full_name == name)
            .where(CompositeScore.league_format == "sf_ppr")
        ).first()
        if not row:
            return None
        return row[1].overall_rank


def test_no_single_source_player_in_top_50(composite_built):
    """v0.14.0 invariant (the 'Luke Grimm fix'): no player with only one
    qualifying source contribution can rank in the top 50.

    The v0.13 bug was that DynastyProcess returning ``market_value=100``
    for Luke Grimm — his ONLY source contribution — vaulted him to #1.
    The v0.14 coverage penalty + Bayesian prior pull crushes single-source
    entries even if they have a max value. We test the general property
    rather than the specific Luke Grimm name because DynastyProcess's CSV
    drifts week-to-week (he may or may not be present on any given day).
    """
    from dynasty.db.session import get_session
    from dynasty.db.models import Player, CompositeScore
    from sqlalchemy import select

    with get_session() as s:
        rows = s.execute(
            select(Player, CompositeScore)
            .join(CompositeScore, Player.id == CompositeScore.player_id)
            .where(CompositeScore.league_format == "sf_ppr")
            .order_by(CompositeScore.overall_rank)
        ).all()

    assert len(rows) > 50, "too few players scored to evaluate top-50 invariant"
    offenders = []
    for p, cs in rows[:50]:
        try:
            b = json.loads(cs.breakdown_json) if cs.breakdown_json else {}
        except Exception:
            b = {}
        meta = b.get("_meta") if isinstance(b, dict) else None
        if not meta:
            continue
        nq = int(meta.get("qualifying_sources", 0))
        if nq < 2:
            offenders.append((p.full_name, cs.overall_rank, nq, meta))
    assert not offenders, (
        f"v0.14 invariant violated — {len(offenders)} single-source players in top 50: "
        f"{offenders[:5]}"
    )


def test_top_young_wr_or_rb_in_top_30(composite_built):
    """At least one of the elite young WR/RB profiles must be in the top 30."""
    candidates = [
        "Bijan Robinson", "Ja'Marr Chase", "Malik Nabers", "Jahmyr Gibbs",
        "Brian Thomas", "CeeDee Lamb", "Justin Jefferson",
    ]
    ranks = [_rank_by_name(n) for n in candidates]
    in_top_30 = [(n, r) for n, r in zip(candidates, ranks) if r and r <= 30]
    assert len(in_top_30) >= 3, f"expected 3+ elite young profiles in top 30, got {in_top_30}"


def test_aging_vet_qb_drops_relative_to_current_skill(composite_built):
    """A 40yo QB with strong current skill (high nfl_impact rank) must rank
    LOWER in the composite than in nfl_impact alone, because the similarity
    engine projects short remaining career.

    Aaron Rodgers (age 40, season 2024) is the canonical case.
    """
    from dynasty.db.session import get_session
    from dynasty.db.models import Player, CompositeScore, Ranking, Source
    from sqlalchemy import select

    with get_session() as s:
        impact = s.execute(select(Source).where(Source.slug == "nfl_impact")).scalar_one_or_none()
        sim = s.execute(select(Source).where(Source.slug == "similarity_career_arc")).scalar_one_or_none()
        rodgers = s.execute(select(Player).where(Player.full_name == "Aaron Rodgers")).scalar_one_or_none()
        assert rodgers is not None, "Aaron Rodgers not in DB"
        cs = s.execute(
            select(CompositeScore)
            .where(CompositeScore.player_id == rodgers.id)
            .where(CompositeScore.league_format == "sf_ppr")
        ).scalar_one_or_none()
        assert cs is not None
        if impact:
            r_impact = s.execute(
                select(Ranking)
                .where(Ranking.player_id == rodgers.id)
                .where(Ranking.source_id == impact.id)
                .where(Ranking.league_format == "sf_ppr")
            ).scalar_one_or_none()
            if r_impact and r_impact.overall_rank:
                # Composite rank must be materially worse than current-skill rank
                assert cs.overall_rank > r_impact.overall_rank, (
                    f"Aaron Rodgers composite #{cs.overall_rank} vs current-skill "
                    f"#{r_impact.overall_rank} — similarity engine isn't penalizing "
                    f"his age."
                )
        if sim:
            r_sim = s.execute(
                select(Ranking)
                .where(Ranking.player_id == rodgers.id)
                .where(Ranking.source_id == sim.id)
                .where(Ranking.league_format == "sf_ppr")
            ).scalar_one_or_none()
            if r_sim and r_sim.market_value is not None:
                # Similarity dynasty value should be modest for a 40yo
                assert r_sim.market_value < 50.0, (
                    f"Aaron Rodgers similarity dynasty_value {r_sim.market_value} too high "
                    f"for a 40yo"
                )


# ---------------------------------------------------------------------------
# Overlay correlation table sanity
# ---------------------------------------------------------------------------


def test_correlation_table_present_and_well_formed():
    from dynasty.overlays import load_correlation_table

    table = load_correlation_table()
    assert "ras" in table and "brainy_ballers_srs" in table
    for pos in ("QB", "RB", "WR", "TE"):
        assert pos in table["ras"]
        v = float(table["ras"][pos])
        assert -1.0 <= v <= 1.0
