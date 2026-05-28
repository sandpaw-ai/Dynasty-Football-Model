"""v3.0 PR 4 — build_prospects_v3 tests.

Covers the orchestration script that wires PR 3's engine into a
projection-layer JSON artifact:

  * Per-class artifact schema (every prospect has the brief's fields)
  * Aggregated prospects_all.json mirrors the per-class shards
  * KTC join — when a prospect matches by (name, position) the ktc
    block is populated and delta math = ktc_pos_rank - model_pos_rank
  * Hit-label thresholds at the boundaries (18, 12, <6 with 3+ seasons)
  * Projection math — similarity-weighted comp average (computed by
    hand) matches what _project_arc produces
  * Idempotency — running with the same inputs twice produces
    byte-identical files
  * Skill-positions-only invariant — nothing outside QB/RB/WR/TE
  * Draft-class boundary — last_season=2024 → draft_class=2025

Tests run NETWORK-FREE and use a synthetic corpus where the real
corpus is too expensive to load (the orchestration script does load
real data when called as a CLI in CI, but unit tests construct
ProspectVectors directly).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from dynasty.engine.prospect_similarity import (
    NameCollisionResolver,
    ProspectVector,
)

import build_prospects_v3 as bp  # type: ignore


# ---------------------------------------------------------------------------
# Fixtures — small synthetic corpus
# ---------------------------------------------------------------------------

def _pv(name, pid, position, school, first, last, age, *,
        adj_avg=15.0, adj_peak=18.0, adj_final=15.0, tier_mult=1.0,
        career_stage=3) -> ProspectVector:
    raw = {
        "adj_fp_pg_avg": adj_avg,
        "adj_fp_pg_peak": adj_peak,
        "adj_fp_pg_final": adj_final,
        "career_stage_length": float(career_stage),
        "conference_tier_mult_avg": tier_mult,
        "position_ord": 1.0,
    }
    feats = {
        "adj_fp_pg_avg": (adj_avg - 12.0) / 4.0,
        "adj_fp_pg_peak": (adj_peak - 14.0) / 4.0,
        "adj_fp_pg_final": (adj_final - 12.0) / 4.0,
        "career_stage_length": float(career_stage),
        "age_at_last_season": float(age),
        "conference_tier_mult_avg": 0.0,
        "position_ord": 1.0,
    }
    return ProspectVector(
        cfb_player_id=pid,
        player_name=name,
        position=position,
        school_last=school,
        first_season=first,
        last_season=last,
        career_stage_length=career_stage,
        age_at_last_season=float(age),
        age_inferred=False,
        conference_tier_last="P5",
        raw_features=raw,
        features=feats,
        notes=[],
    )


@pytest.fixture
def synthetic_corpus():
    return [
        # Target draft class 2024 (last_season=2023) — Caleb-like target
        _pv("Caleb Williams", "T_CALEB", "QB", "USC", 2021, 2023, 21,
            adj_avg=22.0, adj_peak=26.0, adj_final=25.0),
        _pv("Drake Maye", "T_MAYE", "QB", "North Carolina", 2021, 2023, 21,
            adj_avg=20.0, adj_peak=23.0, adj_final=22.0),
        # Historical comps (older draft classes — last_season pre-2021)
        # bridge to nflverse via gsis-ish ids in the resolver below.
        _pv("Joe Burrow", "H_BURROW", "QB", "LSU", 2018, 2019, 22,
            adj_avg=24.0, adj_peak=28.0, adj_final=28.0),
        _pv("Patrick Mahomes", "H_MAHOMES", "QB", "Texas Tech", 2014, 2016, 21,
            adj_avg=23.0, adj_peak=27.0, adj_final=27.0),
        _pv("Sam Darnold", "H_DARNOLD", "QB", "USC", 2016, 2017, 21,
            adj_avg=19.0, adj_peak=21.0, adj_final=20.0),
        _pv("Trevor Lawrence", "H_LAWRENCE", "QB", "Clemson", 2018, 2020, 21,
            adj_avg=21.0, adj_peak=24.0, adj_final=23.0),
        _pv("Mac Jones", "H_MAC", "QB", "Alabama", 2017, 2020, 22,
            adj_avg=20.0, adj_peak=23.0, adj_final=23.0),
        _pv("Zach Wilson", "H_ZACH", "QB", "BYU", 2018, 2020, 21,
            adj_avg=22.0, adj_peak=25.0, adj_final=24.0),
        # An RB historical comp — sanity for skill-positions invariance
        _pv("Bijan Robinson", "H_BIJAN", "RB", "Texas", 2020, 2022, 21,
            adj_avg=20.0, adj_peak=23.0, adj_final=22.0),
        # Non-skill ineligible would be filtered by the engine; we just
        # don't include it (corpus is already skill-only by contract).
    ]


@pytest.fixture
def synthetic_resolver():
    """A bridge that maps the historical comps to nflverse gsis ids."""
    rows = {
        "H_BURROW": {"nfl_pfr_player_id": "00-0036442", "nfl_display_name": "Joe Burrow",
                     "nfl_position": "QB", "last_college_season": 2019, "college": "LSU",
                     "match_strategy": "test"},
        "H_MAHOMES": {"nfl_pfr_player_id": "00-0033873", "nfl_display_name": "Patrick Mahomes",
                      "nfl_position": "QB", "last_college_season": 2016, "college": "Texas Tech",
                      "match_strategy": "test"},
        "H_DARNOLD": {"nfl_pfr_player_id": "00-0034857", "nfl_display_name": "Sam Darnold",
                      "nfl_position": "QB", "last_college_season": 2017, "college": "USC",
                      "match_strategy": "test"},
        "H_LAWRENCE": {"nfl_pfr_player_id": "00-0036971", "nfl_display_name": "Trevor Lawrence",
                       "nfl_position": "QB", "last_college_season": 2020, "college": "Clemson",
                       "match_strategy": "test"},
        "H_MAC": {"nfl_pfr_player_id": "00-0036972", "nfl_display_name": "Mac Jones",
                  "nfl_position": "QB", "last_college_season": 2020, "college": "Alabama",
                  "match_strategy": "test"},
        "H_ZACH": {"nfl_pfr_player_id": "00-0036973", "nfl_display_name": "Zach Wilson",
                   "nfl_position": "QB", "last_college_season": 2020, "college": "BYU",
                   "match_strategy": "test"},
        "H_BIJAN": {"nfl_pfr_player_id": "00-0039051", "nfl_display_name": "Bijan Robinson",
                    "nfl_position": "RB", "last_college_season": 2022, "college": "Texas",
                    "match_strategy": "test"},
    }
    return NameCollisionResolver(rows)


@pytest.fixture
def synthetic_nfl_careers():
    return {
        # Elite (peak3 ≥ 18)
        "00-0036442": {"career_fp": 1900, "peak3_fp_pg": 22.5, "seasons_played": 5,
                       "max_year": 2024, "min_year": 2020},
        "00-0033873": {"career_fp": 3200, "peak3_fp_pg": 26.0, "seasons_played": 8,
                       "max_year": 2024, "min_year": 2017},
        # Starter (12 ≤ peak3 < 18)
        "00-0036971": {"career_fp": 1500, "peak3_fp_pg": 14.0, "seasons_played": 4,
                       "max_year": 2024, "min_year": 2021},
        # Bust (3+ seasons, peak3 < 6)
        "00-0034857": {"career_fp": 700, "peak3_fp_pg": 5.5, "seasons_played": 6,
                       "max_year": 2024, "min_year": 2018},
        # Bust-ish (just under starter, but also under 6)
        "00-0036972": {"career_fp": 320, "peak3_fp_pg": 4.2, "seasons_played": 4,
                       "max_year": 2024, "min_year": 2021},
        # Unknown — only 1 season (too few to call bust)
        "00-0036973": {"career_fp": 80, "peak3_fp_pg": 5.0, "seasons_played": 1,
                       "max_year": 2021, "min_year": 2021},
        "00-0039051": {"career_fp": 600, "peak3_fp_pg": 19.0, "seasons_played": 2,
                       "max_year": 2024, "min_year": 2023},
    }


@pytest.fixture
def synthetic_ktc():
    return {
        ("caleb williams", "QB"): {
            "ktc_rank_sf": 11, "ktc_pos_rank_sf": 2, "ktc_value_sf": 7700,
            "ktc_rank_1qb": 30, "ktc_pos_rank_1qb": 3, "ktc_value_1qb": 6300,
            "is_rookie": False, "ktc_team": "CHI",
        },
        ("drake maye", "QB"): {
            "ktc_rank_sf": 14, "ktc_pos_rank_sf": 3, "ktc_value_sf": 7400,
            "ktc_rank_1qb": 38, "ktc_pos_rank_1qb": 4, "ktc_value_1qb": 5900,
            "is_rookie": False, "ktc_team": "NE",
        },
    }


# ---------------------------------------------------------------------------
# 1. Hit label thresholds
# ---------------------------------------------------------------------------

def test_hit_label_elite():
    assert bp._hit_label({"peak3_fp_pg": 22.5, "seasons_played": 5}) == "elite"
    # boundary
    assert bp._hit_label({"peak3_fp_pg": 18.0, "seasons_played": 2}) == "elite"


def test_hit_label_starter():
    assert bp._hit_label({"peak3_fp_pg": 14.0, "seasons_played": 4}) == "starter"
    assert bp._hit_label({"peak3_fp_pg": 12.0, "seasons_played": 1}) == "starter"


def test_hit_label_bust_requires_min_seasons():
    # 5.0 fp/g with 4 seasons → bust
    assert bp._hit_label({"peak3_fp_pg": 5.0, "seasons_played": 4}) == "bust"
    # 5.0 fp/g with 1 season → unknown (too short to judge)
    assert bp._hit_label({"peak3_fp_pg": 5.0, "seasons_played": 1}) == "unknown"


def test_hit_label_unknown_for_no_career():
    assert bp._hit_label(None) == "unknown"
    assert bp._hit_label({}) == "unknown"


# ---------------------------------------------------------------------------
# 2. Projection math (similarity-weighted)
# ---------------------------------------------------------------------------

def test_project_arc_weighted_average_matches_hand_calc():
    """v3.4: the comp-only weighted average is still computed (now
    surfaced as ``comp_only_career_fp``) and matches the hand calc.
    The returned ``projected_career_fp`` is a confidence-blend of the
    comp number with the pick-tier baseline; we test that here with
    a thin sample (n_with_nfl=3 < FULL_CONFIDENCE_NFL_COMPS=8).
    """
    comps = [
        {"distance": 0.0, "nfl_career": {"career_fp": 1000.0, "peak3_fp_pg": 20.0,
                                          "seasons_played": 5}},
        {"distance": 1.0, "nfl_career": {"career_fp": 500.0, "peak3_fp_pg": 10.0,
                                          "seasons_played": 3}},
        {"distance": 3.0, "nfl_career": {"career_fp": 100.0, "peak3_fp_pg": 4.0,
                                          "seasons_played": 1}},
    ]
    # weights: 1/(1+0)=1.0, 1/(1+1)=0.5, 1/(1+3)=0.25; tot=1.75
    # comp_only career: (1*1000 + 0.5*500 + 0.25*100)/1.75 = 728.57
    # No position/pick supplied — baseline defaults to UDFA tier (low).
    out = bp._project_arc(comps)
    assert out["comp_only_career_fp"] == pytest.approx(728.6, rel=1e-2)
    assert out["n_comps_with_nfl"] == 3
    # With known position+pick we can pin the blended output exactly.
    out_r1 = bp._project_arc(comps, position="RB", pick=10)  # R1_top10
    # confidence = 3/8 = 0.375; baseline R1_top10 RB = 1600
    # blended = 0.375*728.57 + 0.625*1600 = 273.21 + 1000 = 1273.21
    expected = 0.375 * 728.57 + 0.625 * 1600.0
    assert out_r1["projected_career_fp"] == pytest.approx(expected, rel=1e-2)
    assert out_r1["projection_confidence"] == pytest.approx(0.375, rel=1e-2)


def test_project_arc_no_nfl_careers():
    """v3.4: zero NFL careers in the comp pool → fall back to the
    pick-tier baseline (was: 0.0 pre-v3.4, which buried real draftees
    like Fernando Mendoza below undrafted small-school WRs)."""
    comps = [
        {"distance": 0.0, "nfl_career": None},
        {"distance": 1.0, "nfl_career": None},
    ]
    out = bp._project_arc(comps, position="QB", pick=1)  # R1_top10
    # confidence = 0/8 = 0; baseline R1_top10 QB = 3200
    assert out["projected_career_fp"] == pytest.approx(3200.0)
    assert out["n_comps_with_nfl"] == 0
    assert out["projection_source"].startswith("blend_0.00") or out["projection_source"].endswith("R1_top10")
    # Unknown position/pick falls back to a low UDFA baseline.
    out_u = bp._project_arc(comps)
    assert out_u["n_comps_with_nfl"] == 0
    assert out_u["projected_career_fp"] < 200  # UDFA-ish


def test_project_arc_empty():
    """Empty comps + known pick → pure pick-tier baseline."""
    out = bp._project_arc([], position="WR", pick=20)  # R1
    assert out["projected_career_fp"] == pytest.approx(1250.0)
    assert out["n_comps_with_nfl"] == 0


# ---------------------------------------------------------------------------
# 3. Full record schema
# ---------------------------------------------------------------------------

def test_build_prospect_record_full_schema(synthetic_corpus, synthetic_resolver,
                                            synthetic_nfl_careers):
    target = next(pv for pv in synthetic_corpus if pv.cfb_player_id == "T_CALEB")
    rec = bp.build_prospect_record(
        target=target,
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        top_k=10,
    )
    # Required identity fields
    for key in ("name", "slug", "position", "school", "draft_class",
                "last_season_year", "age", "age_inferred",
                "production", "projection", "comps"):
        assert key in rec, f"missing {key}"
    assert rec["draft_class"] == 2024  # last_season=2023 + 1
    assert rec["position"] == "QB"
    assert rec["name"] == "Caleb Williams"
    # production fields
    assert "adj_career_fp_pg" in rec["production"]
    assert "peak_season_fp_pg" in rec["production"]
    # projection fields
    for key in ("projected_career_fp", "projected_peak3_fp_pg",
                "projected_years_in_league", "n_comps_with_nfl"):
        assert key in rec["projection"]
    # Comps - all QB, all hit_label populated
    assert len(rec["comps"]) > 0
    for c in rec["comps"]:
        assert c.get("name")
        assert c.get("slug")
        assert "similarity" in c
        assert "distance" in c
        assert c["hit_label"] in {"elite", "starter", "bust", "unknown"}


def test_skill_positions_only_invariant(synthetic_corpus, synthetic_resolver,
                                         synthetic_nfl_careers, synthetic_ktc):
    by_class = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc=synthetic_ktc,
        draft_classes=(2024,),
        drafted_only=False,
    )
    for cls, rows in by_class.items():
        for r in rows:
            assert r["position"] in bp.SKILL_POSITIONS


def test_draft_class_boundary(synthetic_corpus, synthetic_resolver,
                               synthetic_nfl_careers, synthetic_ktc):
    by_class = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc=synthetic_ktc,
        draft_classes=(2024,),
        drafted_only=False,
    )
    # last_season=2023 → draft_class=2024 only
    assert 2024 in by_class
    for r in by_class[2024]:
        assert r["last_season_year"] == 2023
    # Caleb (last_season=2023) IS in class 2024
    names_2024 = {r["name"] for r in by_class[2024]}
    assert "Caleb Williams" in names_2024


# ---------------------------------------------------------------------------
# 4. KTC join + delta math
# ---------------------------------------------------------------------------

def test_ktc_delta_math(synthetic_corpus, synthetic_resolver,
                         synthetic_nfl_careers, synthetic_ktc):
    by_class = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc=synthetic_ktc,
        draft_classes=(2024,),
        drafted_only=False,
    )
    caleb = next(r for r in by_class[2024] if r["name"] == "Caleb Williams")
    assert caleb["ktc"] is not None
    assert caleb["ktc"]["ktc_pos_rank_sf"] == 2
    # Caleb has the highest projection at QB in this synthetic set, so
    # model_pos_rank should be 1. delta = ktc_pos_rank - model_pos_rank = 2 - 1 = 1
    assert caleb["model_pos_rank"] == 1
    assert caleb["ktc_delta_pos"] == 1


def test_ktc_missing_leaves_null_delta(synthetic_corpus, synthetic_resolver,
                                        synthetic_nfl_careers):
    # No KTC entry at all
    by_class = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc={},
        draft_classes=(2024,),
        drafted_only=False,
    )
    for r in by_class[2024]:
        assert r["ktc"] is None
        assert r["ktc_delta_pos"] is None
        assert r["ktc_delta_overall"] is None


# ---------------------------------------------------------------------------
# 5. Aggregated artifact + idempotency
# ---------------------------------------------------------------------------

def test_aggregated_all_artifact_round_trips(synthetic_corpus, synthetic_resolver,
                                              synthetic_nfl_careers, synthetic_ktc, tmp_path):
    by_class = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc=synthetic_ktc,
        draft_classes=(2024,),
        drafted_only=False,
    )
    bp._write_artifacts(by_class, tmp_path)
    all_path = tmp_path / "prospects_all.json"
    assert all_path.exists()
    payload = json.loads(all_path.read_text())
    assert payload["n_prospects"] == sum(len(v) for v in by_class.values())
    # Every per-class file is present
    for cls in by_class:
        assert (tmp_path / f"prospects_{cls}.json").exists()


def test_idempotency_byte_identical(synthetic_corpus, synthetic_resolver,
                                     synthetic_nfl_careers, synthetic_ktc, tmp_path):
    by_class = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc=synthetic_ktc,
        draft_classes=(2024,),
        drafted_only=False,
    )
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    bp._write_artifacts(by_class, d1)
    # Re-run the build with same inputs (in case there's hidden non-determinism)
    by_class2 = bp.build_prospect_records(
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        ktc=synthetic_ktc,
        draft_classes=(2024,),
        drafted_only=False,
    )
    bp._write_artifacts(by_class2, d2)
    a = (d1 / "prospects_all.json").read_bytes()
    b = (d2 / "prospects_all.json").read_bytes()
    assert a == b, "non-deterministic prospect artifact"


# ---------------------------------------------------------------------------
# 6. Hit labels propagate into comps
# ---------------------------------------------------------------------------

def test_comp_hit_labels_populate(synthetic_corpus, synthetic_resolver,
                                   synthetic_nfl_careers):
    target = next(pv for pv in synthetic_corpus if pv.cfb_player_id == "T_CALEB")
    rec = bp.build_prospect_record(
        target=target,
        corpus=synthetic_corpus,
        resolver=synthetic_resolver,
        nfl_careers=synthetic_nfl_careers,
        top_k=10,
    )
    # At least one comp has each kind of hit_label given the synthetic data
    labels = [c["hit_label"] for c in rec["comps"]]
    # Burrow + Mahomes should be 'elite' if they survive the position/stage
    # filter. Window allows |Δ age| ≤ 2 and |Δ stage_len| ≤ 1; Caleb is
    # age=21 stage=3, Burrow age=22 stage=2, Mahomes age=21 stage=3 — both
    # pass.
    assert "elite" in labels


# ---------------------------------------------------------------------------
# 7. Slug stability
# ---------------------------------------------------------------------------

def test_slug_stable_and_url_safe():
    assert bp._slugify("Caleb Williams", "12345") == "caleb-williams-12345"
    assert bp._slugify("Ja'Marr Chase", "sr_chase-1") == "ja-marr-chase-chase1"
    # Edge: empty pid
    assert bp._slugify("Test Name", "").startswith("test-name")
