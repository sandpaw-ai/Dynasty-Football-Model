"""Tests for the v2.3.1 similarity-transparency changes.

Covers:
  * Comp records use raw vector similarity (bounded in (0, 1]) for the
    user-facing ``similarity`` field while preserving the
    breakout-boosted score as ``ranking_similarity`` (rookie engine
    only; the v2.0 cumulative engine never boosted).
  * Every comp record carries ``seasons_played``, ``final_age``, and
    ``washed_out`` so the player page can flag short-career comps
    (Phil's Bo Nix \u2192 Aaron Brooks complaint).
  * The Dynasty Rankings page renders Phil's flipped colour mapping:
    negative delta (model > consensus) becomes green; positive delta
    becomes red.
  * The per-player page surfaces the calculation breakdown (comp-
    weighted projection, peak-anchored projection, the explicit
    survival / confidence / late-breakout multipliers, and the final
    score).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures: build the site ONCE for these tests.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("site")
    from dynasty.engine.similarity_v1 import run_engine
    from dynasty.report import generate_site
    engine = run_engine(persist=False)
    generate_site(
        output_dir=str(out_dir),
        league_format="sf_ppr",
        limit=300,
        engine=engine,
    )
    return out_dir, engine


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Part A: similarity bounded in (0, 1] for the user-facing field
# ---------------------------------------------------------------------------

def test_rookie_comp_similarity_bounded_zero_to_one(site):
    """Harold Fannin Jr. is the canonical bug repro: pre-fix his top
    comps showed similarity > 1 because the breakout-bias multiplied the
    raw vector similarity (capped at 1) by a factor that could exceed
    1. The user-facing ``similarity`` must now be the raw, capped
    value.
    """
    _, engine = site
    for ap in engine.rankings:
        if ap["name"] == "Harold Fannin Jr.":
            comps = engine.comps.get(ap["player_id"], [])
            assert comps, "Fannin should have comp records"
            for c in comps:
                sim = c["similarity"]
                assert 0.0 < sim <= 1.0, (
                    f"comp {c['name']} similarity {sim} not in (0, 1]"
                )
            # The breakout-bias version is preserved separately for
            # diagnostic transparency.
            top = comps[0]
            assert "ranking_similarity" in top
            # And for at least one Fannin comp the ranking score should
            # exceed the display score (that's what the boost was doing).
            boosted = [
                c for c in comps
                if c["ranking_similarity"] > c["similarity"] + 1e-6
            ]
            assert boosted, (
                "expected at least one rookie comp where the "
                "breakout-boosted ranking_similarity exceeds the "
                "raw display similarity"
            )
            return
    pytest.skip("Harold Fannin Jr. not in current rankings")


def test_v2_engine_comp_similarity_also_bounded(site):
    """The v2.0 cumulative-arc engine produces similarity natively in
    (0, 1] (no breakout boost). Verify with Bo Nix who routes through
    that engine.
    """
    _, engine = site
    for ap in engine.rankings:
        if ap["name"] == "Bo Nix":
            comps = engine.comps.get(ap["player_id"], [])
            assert comps
            for c in comps:
                assert 0.0 < c["similarity"] <= 1.0
                # On the v2.0 path display == ranking by design.
                assert c["similarity"] == c["ranking_similarity"]
            return
    pytest.skip("Bo Nix not in current rankings")


# ---------------------------------------------------------------------------
# Part B: wash-out flag + career-length diagnostics
# ---------------------------------------------------------------------------

def test_bo_nix_aaron_brooks_flagged_as_washed_out(site):
    """Bo Nix's headline comp is Aaron Brooks (last season age 30, 7
    NFL years). Phil's 2026-05-22 complaint: the model picks him as
    Nix's most similar but Brooks failed out. The wash-out badge must
    fire on Brooks's row.
    """
    _, engine = site
    for ap in engine.rankings:
        if ap["name"] == "Bo Nix":
            comps = engine.comps.get(ap["player_id"], [])
            brooks = next(
                (c for c in comps if c["name"] == "Aaron Brooks"), None,
            )
            assert brooks is not None, "Aaron Brooks must be in Nix comps"
            assert brooks["washed_out"] is True
            assert brooks["seasons_played"] is not None
            assert brooks["seasons_played"] < 8
            assert brooks["final_age"] is not None
            assert brooks["final_age"] <= 30
            return
    pytest.skip("Bo Nix not in current rankings")


def test_durable_comps_not_flagged(site):
    """Anchor case: Tom Brady (21 NFL seasons) must NOT be flagged
    washed-out even when he's a comp for some target.
    """
    _, engine = site
    for ap in engine.rankings:
        if ap["name"] == "Bo Nix":
            comps = engine.comps.get(ap["player_id"], [])
            brady = next(
                (c for c in comps if c["name"] == "Tom Brady"), None,
            )
            if brady is None:
                pytest.skip("Brady not in Nix's comp set")
            assert brady["washed_out"] is False
            assert brady["seasons_played"] >= 8
            return
    pytest.skip("Bo Nix not in current rankings")


# ---------------------------------------------------------------------------
# Part C: Marvin Harrison Jr. data separation
# ---------------------------------------------------------------------------

def test_marvin_harrison_sr_and_jr_separate_records(site):
    """Phil flagged a hypothesis that Jr might be merged with Sr (HOF
    WR). Confirm: separate gsis_id rows, separate birth dates, and
    only Jr appears in the active rankings.
    """
    _, engine = site
    by_name = [r for r in engine.rankings if "Marvin Harrison" in r["name"]]
    assert any(r["name"] == "Marvin Harrison Jr." for r in by_name), (
        "Jr should be in active rankings"
    )
    assert not any(
        r["name"] == "Marvin Harrison" and r["player_id"] == "00-0007024"
        for r in by_name
    ), "Sr (HOF, retired) should NOT appear in active rankings"
    jr = next(r for r in engine.rankings if r["name"] == "Marvin Harrison Jr.")
    # Sanity: separate gsis_id confirms the data split is clean.
    assert jr["player_id"] == "00-0039849"


# ---------------------------------------------------------------------------
# Part D: consensus delta colour flip (Phil 2026-05-22)
# ---------------------------------------------------------------------------

def test_consensus_chip_function_flips_colours(site):
    """The Dynasty Rankings page must map negative delta (model is
    HIGHER than consensus = model bullish) to ``div-up`` (green) and
    positive delta to ``div-down`` (red).
    """
    out_dir, _ = site
    html = _read(out_dir / "league.html")
    # Skip if no KTC snapshot was cached during the test build \u2014 the
    # legacy overlay fallback uses different chip semantics.
    if "const CONSENSUS" not in html:
        pytest.skip("legacy overlay fallback rendered (no KTC snapshot)")
    # Locate the consensus-page chip function and assert the polarity.
    start = html.find("const CONSENSUS")
    end = html.find("function setFormat", start)
    chip_block = html[start:end]
    # Negative deltas -> green (div-up); positives -> red (div-down).
    assert "d <= -10" in chip_block and "div-up-big" in chip_block
    assert "d < 0" in chip_block and "div-up\"" in chip_block
    assert "d >= 10" in chip_block and "div-down-big" in chip_block
    assert "d > 0" in chip_block and "div-down\"" in chip_block


# ---------------------------------------------------------------------------
# Part E: per-player calculation breakdown
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug_fragment", [
    "bo-nix",                # v2.0 engine path
    "harold-fannin-jr",      # rookie engine path
    "marvin-harrison-jr",    # confidence-shrinkage-dominated case
])
def test_player_page_renders_calculation_breakdown(site, slug_fragment):
    out_dir, _ = site
    players_dir = out_dir / "players"
    candidates = [
        p for p in os.listdir(players_dir) if slug_fragment in p
    ]
    assert candidates, f"no player page matching {slug_fragment}"
    page = _read(players_dir / candidates[0])
    # Headline
    assert "How this <span" in page or "How this " in page
    # Each penalty-stack row must be present
    assert "Comp-weighted projection" in page
    assert "Peak-anchored projection" in page
    assert "Raw projection (pre-penalty)" in page
    assert "Survival" in page
    assert "Sample confidence" in page
    assert "Late-breakout penalty" in page
    assert "Final production score" in page
    # The explicit weighted-average formula must be rendered.
    assert "post-age-fp" in page
