"""v3.6 - Prospect projection fixes (Phil 2026-05-28 round 4).

Phil's complaints round 4:
  - Jadarian Price: ranked high but has zero NFL-career comps. Why?
  - KC Concepcion: ranked #1 with no comparable college players. Why?
  - Mendoza: not even on the 2026 page despite being the #1 NFL pick.
  - Makai Lemon: looks good.

What v3.6 pins:

1. MEANINGFUL_NFL_CAREER_FP = 200 - a comp only counts toward
   confidence if its NFL career was at least one starter-quality
   season. Mendoza had 10 NFL comps but most were Connor-Cook-tier
   (6 fp). v3.6 says those don't count.
2. FULL_CONFIDENCE_NFL_COMPS = 12 (was 8) - slower transition to
   comp-only. Even with 8 meaningful comps the baseline still carries
   ~33% weight.
3. MIN_BASELINE_FRACTION = 0.30 - a drafted player can never project
   below 30% of their pick-tier baseline. Stops the comp-weighted
   projection from crushing #1 overall picks to near-zero when the
   college-similarity engine produces a bad comp pool.
4. Last-name + school fallback - the corpus match now handles
   nickname mismatches ("KC Concepcion" PFR vs "Kevin Concepcion"
   cfbfastR) so picks don't end up as empty stubs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_prospects_v3 as bp  # type: ignore


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_meaningful_nfl_comp_threshold():
    """Comps with career_fp >= 200 count toward n_meaningful_nfl_comps."""
    comps = [
        {"distance": 0.0, "nfl_career": {"career_fp": 1000.0, "peak3_fp_pg": 15.0, "seasons_played": 5}},
        {"distance": 0.5, "nfl_career": {"career_fp": 200.0, "peak3_fp_pg": 8.0, "seasons_played": 3}},
        {"distance": 1.0, "nfl_career": {"career_fp": 199.0, "peak3_fp_pg": 4.0, "seasons_played": 2}},
        {"distance": 1.5, "nfl_career": {"career_fp": 6.0, "peak3_fp_pg": 1.0, "seasons_played": 1}},
        {"distance": 2.0, "nfl_career": None},
    ]
    out = bp._project_arc(comps, position="QB", pick=1)
    assert out["n_comps_with_nfl"] == 4
    assert out["n_meaningful_nfl_comps"] == 2  # only the 1000 and 200 count


def test_floor_protects_high_pick_with_zero_career_fp_comps():
    """If all comps are zero-NFL or NFL-thin (Mendoza's pre-v3.6
    case), the floor must prevent projection collapse below 30%
    baseline.
    """
    no_nfl_comps = [
        {"distance": 0.5, "nfl_career": {"career_fp": 10.0, "peak3_fp_pg": 1.0, "seasons_played": 1}},
    ] * 8
    out = bp._project_arc(no_nfl_comps, position="QB", pick=1)
    # n_meaningful = 0 -> baseline dominates (3200), floor = 30% = 960.
    # Either way, projection should be high.
    assert out["projected_career_fp"] >= 960.0


def test_undrafted_no_pick_no_floor():
    """A player with no pick (UDFA / un-drafted, pick=None) doesn't
    get the v3.6 floor protection - we only apply the floor when the
    NFL spent a real pick.
    """
    thin_comps = [
        {"distance": 0.5, "nfl_career": {"career_fp": 5.0, "peak3_fp_pg": 0.5, "seasons_played": 1}},
    ] * 5
    out = bp._project_arc(thin_comps, position="WR", pick=None)
    assert not out.get("floor_applied")


def test_high_confidence_meaningful_comps_use_comp_weighted():
    """When the comp pool has FULL_CONFIDENCE_NFL_COMPS=12 meaningful
    NFL careers, the projection should be the pure comp-weighted
    number (no baseline blending).
    """
    strong_comps = [
        {"distance": 0.5, "nfl_career": {"career_fp": 800.0, "peak3_fp_pg": 12.0, "seasons_played": 5}},
    ] * 12
    out = bp._project_arc(strong_comps, position="WR", pick=20)
    assert out["n_meaningful_nfl_comps"] == 12
    assert out["projection_confidence"] == pytest.approx(1.0)
    # Comp-only would be 800 (since all comps are identical with weight 1.0).
    assert out["projected_career_fp"] == pytest.approx(800.0, rel=1e-2)


# ---------------------------------------------------------------------------
# Integration tests (only run when real data is present)
# ---------------------------------------------------------------------------

DATA_CORPUS_OK = (
    Path(__file__).resolve().parents[1] / "data" / "historical_ncaa_football"
).exists()
DATA_PFR_OK = (
    Path(__file__).resolve().parents[1] / "data" / "pfr" / "draft_classes_all.json"
).exists()
DATA_PLAYERS_OK = (
    Path(__file__).resolve().parents[1] / "data" / "nflverse" / "players.csv.gz"
).exists()

_skip_no_data = pytest.mark.skipif(
    not (DATA_CORPUS_OK and DATA_PFR_OK and DATA_PLAYERS_OK),
    reason="real data not present in this environment",
)


@pytest.fixture(scope="module")
def built_prospects():
    if not (DATA_CORPUS_OK and DATA_PFR_OK and DATA_PLAYERS_OK):
        pytest.skip("data not present")
    from dynasty.engine.prospect_similarity import (
        build_prospect_corpus, NameCollisionResolver, DEFAULT_BRIDGE_FILE,
    )
    corpus = build_prospect_corpus()
    resolver = NameCollisionResolver.from_file(DEFAULT_BRIDGE_FILE)
    players_csv = Path("data/nflverse/players.csv.gz")
    name_to_gsis = bp._load_nfl_name_to_gsis(players_csv)
    players_meta = bp._load_nfl_players_meta(players_csv)
    nfl_careers = bp._load_nfl_careers(
        Path("data/nflverse/player_stats_season.csv.gz")
    )
    return bp.build_prospect_records(
        corpus=corpus,
        resolver=resolver,
        nfl_careers=nfl_careers,
        ktc={},
        draft_classes=(2024, 2025, 2026, 2027),
        top_k=25,
        pfr_path=Path("data/pfr/draft_classes_all.json"),
        drafted_only=True,
        name_to_gsis=name_to_gsis,
        players_meta=players_meta,
    )


@_skip_no_data
def test_mendoza_projection_is_sensible_for_a_number_one_pick(built_prospects):
    """v3.6: Mendoza (R1 #1 overall) must project well above 2000 fp.
    Pre-v3.6 he was at 91.5 because his comp-weighted dominated with
    10 sub-meaningful NFL comps. Post-v3.6 his confidence drops
    sharply and the R1_top10 QB baseline (3200) dominates.
    """
    mendoza = next(
        (r for r in built_prospects.get(2026, [])
         if "Mendoza" in r["name"]),
        None,
    )
    assert mendoza is not None
    assert mendoza["projection"]["projected_career_fp"] >= 2000.0, (
        f"Mendoza projection {mendoza['projection']['projected_career_fp']} "
        f"- expected >= 2000 under v3.6"
    )


@_skip_no_data
def test_concepcion_now_matched_via_last_name_fallback(built_prospects):
    """v3.6: PFR has 'KC Concepcion' but cfbfastR has him as 'Kevin
    Concepcion'. The last-name+school fallback must surface the
    corpus record so his prospect page shows actual college stats
    and a comp grid (not an empty stub).
    """
    concepcion = next(
        (r for r in built_prospects.get(2026, [])
         if r["name"] == "Kevin Concepcion"),
        None,
    )
    assert concepcion is not None, (
        "v3.6 should match 'KC Concepcion' (PFR) to 'Kevin Concepcion' (corpus)"
    )
    assert concepcion.get("corpus_match") is True
    assert concepcion.get("comps"), (
        "Concepcion has no comps - the corpus match did not fire"
    )


@_skip_no_data
def test_jadarian_price_projection_uses_baseline_not_dominated_by_thin_comps(built_prospects):
    """v3.6: Jadarian Price (R1 #32 RB) has a comp pool full of
    college backups with no NFL careers. With v3.6's MEANINGFUL_NFL
    threshold his confidence drops to ~0 and the R1 RB baseline
    (1200) anchors the projection. He should NOT collapse to ~0 from
    a comp_weighted of 0; he should NOT be unfairly inflated above
    his pick-tier baseline either.
    """
    price = next(
        (r for r in built_prospects.get(2026, [])
         if r["name"] == "Jadarian Price"),
        None,
    )
    if price is None:
        pytest.skip("Jadarian Price not in built_prospects")
    proj = price["projection"]
    # Should land near the R1 RB baseline.
    assert 800.0 <= proj["projected_career_fp"] <= 1500.0, (
        f"Price projection {proj['projected_career_fp']} "
        f"- expected ~1200 (R1 RB baseline) under v3.6"
    )
