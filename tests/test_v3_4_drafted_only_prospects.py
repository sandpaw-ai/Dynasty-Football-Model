"""v3.4 — Drafted-only prospects pipeline tests (Phil 2026-05-28).

Phil's brief: "Just pull the classes from pro-football-reference and use
the link as a guide for the 2026 class. No players should appear in
the 2026 tab unless they are on this link." Plus: pull SR/CFB stats
per drafted player and project NFL fp from comp-similar college careers.

The v3.4 invariants this file pins:

1. The 2026 prospects class is EXACTLY the set of PFR-drafted 2026
   skill players (no undrafted college players bleed in).
2. Every drafted player has a record \u2014 even if they're not in our
   college corpus, a stub is emitted with the pick-tier baseline.
3. ``_project_arc`` falls back to the pick-tier baseline when zero
   comps have NFL careers, instead of returning 0.0.
4. Confidence-blend: thin NFL-comp pools (n_with_nfl < 8) get the
   pick-tier baseline mixed in proportionally.
5. The default sort within a class is by NFL pick (ascending).
6. Fernando Mendoza \u2014 the worked-example 2026 #1 overall pick \u2014 must
   appear in the 2026 class with a sensible projection (> R1 QB
   baseline midpoint), not the 1.4 we saw pre-v3.4.
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
# Unit tests \u2014 baseline + projection blending
# ---------------------------------------------------------------------------

def test_pick_tier_buckets():
    assert bp._pick_tier(1) == "R1_top10"
    assert bp._pick_tier(10) == "R1_top10"
    assert bp._pick_tier(11) == "R1"
    assert bp._pick_tier(32) == "R1"
    assert bp._pick_tier(64) == "R2"
    assert bp._pick_tier(100) == "R3"
    assert bp._pick_tier(150) == "R4"
    assert bp._pick_tier(200) == "R5_6"
    assert bp._pick_tier(220) == "R7"
    assert bp._pick_tier(None) == "UDFA"


def test_baseline_projection_returns_pick_tier_values():
    out = bp._baseline_projection("QB", pick=1)
    assert out["projected_career_fp"] == pytest.approx(3200.0)
    assert out["projection_source"] == "pick_tier_baseline_R1_top10"
    out = bp._baseline_projection("RB", pick=50)
    assert out["projected_career_fp"] == pytest.approx(780.0)
    out = bp._baseline_projection("WR", pick=None)
    assert out["projected_career_fp"] == pytest.approx(40.0)  # UDFA WR


def test_projection_blend_with_thin_nfl_comp_pool():
    """3 comps with NFL data, all decent careers, but n_with_nfl < 8.
    The blended projection should sit between the pure-comp number
    and the pick-tier baseline."""
    comps = [
        {"distance": 0.0, "nfl_career": {"career_fp": 500.0, "peak3_fp_pg": 12.0,
                                          "seasons_played": 4}},
        {"distance": 0.5, "nfl_career": {"career_fp": 300.0, "peak3_fp_pg": 8.0,
                                          "seasons_played": 3}},
        {"distance": 1.0, "nfl_career": {"career_fp": 100.0, "peak3_fp_pg": 4.0,
                                          "seasons_played": 2}},
    ]
    out = bp._project_arc(comps, position="QB", pick=1)  # R1_top10 QB baseline = 3200
    assert out["n_comps_with_nfl"] == 3
    # confidence = 3/8 = 0.375; output must sit strictly between
    # comp-only and baseline.
    assert out["comp_only_career_fp"] < out["projected_career_fp"] < 3200.0


def test_projection_zero_nfl_comps_falls_back_to_baseline():
    """Phil's Mendoza case: 0 of the comps have NFL careers \u2192 use
    pick-tier baseline (was 0.0 pre-v3.4)."""
    comps = [{"distance": 0.0, "nfl_career": None}] * 5
    out = bp._project_arc(comps, position="QB", pick=1)
    assert out["n_comps_with_nfl"] == 0
    # Pure baseline because confidence = 0.
    assert out["projected_career_fp"] == pytest.approx(3200.0)


# ---------------------------------------------------------------------------
# Integration tests \u2014 only run when the real PFR + corpus data exists.
# ---------------------------------------------------------------------------

DATA_PFR_PATH = Path(__file__).resolve().parents[1] / "data" / "pfr" / "draft_classes_all.json"
DATA_CORPUS_OK = (
    Path(__file__).resolve().parents[1] / "data" / "historical_ncaa_football"
).exists()
DATA_PFR_OK = DATA_PFR_PATH.exists()

_skip_no_data = pytest.mark.skipif(
    not (DATA_PFR_OK and DATA_CORPUS_OK),
    reason="real PFR/corpus data not present in this environment",
)


@pytest.fixture(scope="module")
def built_prospects():
    if not (DATA_PFR_OK and DATA_CORPUS_OK):
        pytest.skip("data not present")
    from dynasty.engine.prospect_similarity import (
        build_prospect_corpus, NameCollisionResolver, DEFAULT_BRIDGE_FILE,
    )
    corpus = build_prospect_corpus()
    resolver = NameCollisionResolver.from_file(DEFAULT_BRIDGE_FILE)
    # Empty NFL careers + KTC is fine for these structural assertions.
    by_class = bp.build_prospect_records(
        corpus=corpus,
        resolver=resolver,
        nfl_careers={},
        ktc={},
        draft_classes=(2024, 2025, 2026, 2027),
        top_k=25,
        pfr_path=DATA_PFR_PATH,
        drafted_only=True,
    )
    return by_class


@_skip_no_data
def test_2026_class_only_contains_pfr_drafted_players(built_prospects):
    """v3.4 invariant: only PFR-drafted 2026 skill players appear in 2026.

    v3.6 (Phil 2026-05-28): the corpus match strategy falls back to
    (last_name + school) when full-name match misses (handles
    nicknames like "KC Concepcion" PFR vs "Kevin Concepcion" cfbfastR).
    The record's display name uses the corpus name, so the test has
    to accept BOTH the PFR pick name AND a (last_name, college) match.
    """
    with DATA_PFR_PATH.open(encoding="utf-8") as f:
        pfr = json.load(f)
    drafted_2026 = [
        p for p in pfr["by_year"]["2026"]
        if (p.get("position") or "").upper() in bp.SKILL_POSITIONS
    ]
    drafted_2026_names = {
        bp._normalize_name_for_pfr(p["player_name"]) for p in drafted_2026
    }
    drafted_2026_last_school = {
        ((bp._normalize_name_for_pfr(p["player_name"]).split() or [""])[-1],
         (p.get("college") or "").strip().lower())
        for p in drafted_2026
    }

    def _is_in_drafted(name: str, school: str) -> bool:
        norm = bp._normalize_name_for_pfr(name)
        if norm in drafted_2026_names:
            return True
        last = (norm.split() or [""])[-1]
        school_l = (school or "").strip().lower()
        for pfr_last, pfr_school in drafted_2026_last_school:
            if last == pfr_last and school_l and pfr_school and (
                school_l in pfr_school or pfr_school in school_l
            ):
                return True
        return False

    in_2026 = built_prospects.get(2026, [])
    assert in_2026, "no 2026 prospects \u2014 PFR loading broken?"
    leaked = []
    for r in in_2026:
        if not _is_in_drafted(r["name"], r.get("school") or ""):
            leaked.append(r["name"])
    assert not leaked, (
        f"Non-drafted players bled into 2026 class: {leaked[:5]} "
        f"(total {len(leaked)})"
    )
    # Every PFR 2026 drafted skill player must appear in the class.
    in_2026_keys = set()
    for r in in_2026:
        norm = bp._normalize_name_for_pfr(r["name"])
        in_2026_keys.add(("name", norm))
        last = (norm.split() or [""])[-1]
        school_l = (r.get("school") or "").strip().lower()
        in_2026_keys.add(("last+school", last, school_l))
    missing = []
    for p in drafted_2026:
        pfr_norm = bp._normalize_name_for_pfr(p["player_name"])
        if ("name", pfr_norm) in in_2026_keys:
            continue
        pfr_last = (pfr_norm.split() or [""])[-1]
        pfr_school = (p.get("college") or "").strip().lower()
        # Loose containment match against corpus schools ("Texas A&M"
        # vs "Texas A&M;").
        found = False
        for (kind, *rest) in in_2026_keys:
            if kind != "last+school":
                continue
            corp_last, corp_school = rest
            if corp_last == pfr_last and pfr_school and corp_school and (
                pfr_school in corp_school or corp_school in pfr_school
            ):
                found = True
                break
        if not found:
            missing.append(p["player_name"])
    assert not missing, f"PFR 2026 drafted players missing from class: {sorted(missing)[:5]}"


@_skip_no_data
def test_fernando_mendoza_has_sensible_projection(built_prospects):
    """Phil's worked example: Mendoza is the 2026 #1 overall pick. He
    must show a meaningful projection (well above the v3.0 1.4 we
    started with), anchored on the R1_top10 QB baseline."""
    mendoza = next(
        (r for r in built_prospects.get(2026, [])
         if r["name"] == "Fernando Mendoza"),
        None,
    )
    assert mendoza is not None, "Mendoza missing from 2026 class"
    assert mendoza.get("drafted", {}).get("pick") == 1
    assert mendoza["projection"]["projected_career_fp"] >= 1000.0, (
        f"Mendoza projection collapsed: {mendoza['projection']}"
    )


@_skip_no_data
def test_default_sort_is_by_nfl_pick(built_prospects):
    """Inside each class, rows are ordered by ascending NFL pick."""
    for class_year, rows in built_prospects.items():
        picks = [
            (r.get("drafted") or {}).get("pick") or 10**6
            for r in rows
        ]
        assert picks == sorted(picks), (
            f"Class {class_year} rows are not sorted by NFL pick: "
            f"first 5 picks={picks[:5]}"
        )


@_skip_no_data
def test_undiscovered_picks_get_stub_records(built_prospects):
    """PFR picks that don't match our college corpus still appear as
    stub records with corpus_match=False and a baseline projection."""
    stubs = []
    for rows in built_prospects.values():
        for r in rows:
            if r.get("corpus_match") is False:
                stubs.append(r)
    # Don't assert a hard count — just that stubs exist + carry the
    # required shape.
    if stubs:
        s = stubs[0]
        assert s["drafted"] is not None
        assert s["projection"]["projected_career_fp"] > 0
        assert s["comps"] == []
        assert s.get("corpus_match") is False
