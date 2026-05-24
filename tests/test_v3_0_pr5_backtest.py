"""v3.0 PR 5 — Back-test harness tests.

Covers the deterministic, network-free back-test that gates PR 6's
shipment of the prospects UI:

  * Spearman ρ matches a hand-calculated value for a synthetic input
  * Leakage prevention — comp pool excludes target's class − 1 and later
  * Gate threshold logic — each gate classifies pass / soft / hard fail
  * Per-class breakdown contains all hold-out classes
  * Deterministic output: same inputs → same summary
  * Hit-label propagation through the harness mirrors PR 4's labels
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from dynasty.engine.prospect_similarity import (  # noqa: E402
    NameCollisionResolver,
    ProspectVector,
)

import backtest_v3_engine as bt  # noqa: E402
import build_prospects_v3 as bp  # noqa: E402


# ---------------------------------------------------------------------------
# Spearman ρ — analytic check
# ---------------------------------------------------------------------------

def test_spearman_perfect_positive():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 20, 30, 40, 50]
    assert bt._spearman_rho(xs, ys) == pytest.approx(1.0)


def test_spearman_perfect_negative():
    xs = [1, 2, 3, 4, 5]
    ys = [50, 40, 30, 20, 10]
    assert bt._spearman_rho(xs, ys) == pytest.approx(-1.0)


def test_spearman_with_ties():
    # Two pairs perfectly correlated, one tie
    xs = [1.0, 2.0, 2.0, 4.0]
    ys = [10.0, 20.0, 20.0, 40.0]
    # All ties resolve to average ranks → perfectly correlated
    assert bt._spearman_rho(xs, ys) == pytest.approx(1.0)


def test_spearman_short_input():
    assert bt._spearman_rho([], []) == 0.0
    assert bt._spearman_rho([1.0], [2.0]) == 0.0


# ---------------------------------------------------------------------------
# Gate threshold logic
# ---------------------------------------------------------------------------

def test_evaluate_gates_pass():
    summary = {
        "hit_at_10": 0.30,    # > 0.22
        "bust_at_10": 0.60,    # > 0.55
        "spearman_rho": 0.40,  # > 0.28
        "ktc_h2h": 0.55,       # > 0.50
    }
    g = bt._evaluate_gates(summary)
    for name, gate in g.items():
        assert gate["status"] == "pass", f"{name} should pass: {gate}"


def test_evaluate_gates_soft_fail():
    # Within 5% of every target (just under)
    summary = {
        "hit_at_10": 0.22 * 0.97,
        "bust_at_10": 0.55 * 0.97,
        "spearman_rho": 0.28 * 0.97,
        "ktc_h2h": 0.50 * 0.97,
    }
    g = bt._evaluate_gates(summary)
    for name, gate in g.items():
        assert gate["status"] == "soft_fail", f"{name} should soft-fail: {gate}"


def test_evaluate_gates_hard_fail():
    # 20% short on each metric
    summary = {
        "hit_at_10": 0.22 * 0.7,
        "bust_at_10": 0.55 * 0.7,
        "spearman_rho": 0.28 * 0.7,
        "ktc_h2h": 0.50 * 0.7,
    }
    g = bt._evaluate_gates(summary)
    for name, gate in g.items():
        assert gate["status"] == "hard_fail", f"{name} should hard-fail: {gate}"


def test_overall_status_classification():
    pass_gates = {
        "a": {"status": "pass"}, "b": {"status": "pass"},
    }
    assert bt._overall_status(pass_gates) == "pass"
    soft = {"a": {"status": "pass"}, "b": {"status": "soft_fail"}}
    assert bt._overall_status(soft) == "soft_fail"
    hard = {"a": {"status": "pass"}, "b": {"status": "hard_fail"}}
    assert bt._overall_status(hard) == "hard_fail"
    mixed = {"a": {"status": "soft_fail"}, "b": {"status": "hard_fail"}}
    assert bt._overall_status(mixed) == "hard_fail"


# ---------------------------------------------------------------------------
# Leakage prevention — synthetic corpus
# ---------------------------------------------------------------------------

def _pv(name, pid, last, position="QB", *, adj=15.0, age=21.0,
        career_stage=3) -> ProspectVector:
    raw = {"adj_fp_pg_avg": adj, "adj_fp_pg_peak": adj + 3,
           "adj_fp_pg_final": adj, "career_stage_length": float(career_stage),
           "conference_tier_mult_avg": 1.0, "position_ord": 1.0}
    feats = {"adj_fp_pg_avg": (adj - 12) / 4.0,
             "adj_fp_pg_peak": (adj + 3 - 14) / 4.0,
             "adj_fp_pg_final": (adj - 12) / 4.0,
             "career_stage_length": float(career_stage),
             "age_at_last_season": float(age),
             "conference_tier_mult_avg": 0.0, "position_ord": 1.0}
    return ProspectVector(
        cfb_player_id=pid, player_name=name, position=position,
        school_last="Test U", first_season=last - career_stage + 1,
        last_season=last, career_stage_length=career_stage,
        age_at_last_season=age, age_inferred=False,
        conference_tier_last="P5",
        raw_features=raw, features=feats, notes=[],
    )


def test_leakage_excludes_same_class_minus_one():
    """When the target's draft_class is 2019 (last_season=2018), the
    comp pool must exclude any player whose class_year >= 2018 — i.e.
    last_season >= 2017. Only players with last_season ≤ 2016 are
    allowed.
    """
    target = _pv("Target", "T", 2018)
    corpus = [
        target,
        _pv("ContemporaryA", "A", 2017),  # class 2018 = target.class - 1 → BLOCKED
        _pv("ContemporaryB", "B", 2018),  # class 2019 = target.class     → BLOCKED
        _pv("HistoricalOK", "H1", 2016),  # class 2017                    → ALLOWED
        _pv("HistoricalOK2", "H2", 2015), # class 2016                    → ALLOWED
    ]
    proj, comps = bt._project_for_holdout(
        target=target,
        full_corpus=corpus,
        resolver=NameCollisionResolver({}),
        nfl_careers={},
    )
    names = [c["name"] for c in comps]
    assert "ContemporaryA" not in names, names
    assert "ContemporaryB" not in names, names
    # At least one historical comp survives
    assert any(n.startswith("Historical") for n in names), names


def test_target_excluded_from_own_comps():
    target = _pv("Target", "T", 2018)
    corpus = [target,
              _pv("HistoricalOK", "H1", 2015),
              _pv("HistoricalOK2", "H2", 2014)]
    _, comps = bt._project_for_holdout(
        target=target, full_corpus=corpus,
        resolver=NameCollisionResolver({}), nfl_careers={},
    )
    for c in comps:
        assert c["name"] != "Target", "target leaked into its own comps"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic_summary():
    """Same corpus + bridge + careers → same summary numbers."""
    # Build a tiny synthetic case that exercises the evaluator
    corpus = [
        _pv("Holdout1", "T1", 2017, "QB", adj=18.0),
        _pv("Holdout2", "T2", 2018, "RB", adj=17.0),
        _pv("Hist1", "H1", 2010, "QB", adj=20.0),
        _pv("Hist2", "H2", 2011, "RB", adj=18.0),
        _pv("Hist3", "H3", 2012, "QB", adj=19.0),
    ]
    resolver = NameCollisionResolver({
        "T1": {"nfl_pfr_player_id": "gsis-T1", "nfl_display_name": "Holdout1",
               "nfl_position": "QB", "last_college_season": 2016, "college": "Test U",
               "match_strategy": "test"},
        "T2": {"nfl_pfr_player_id": "gsis-T2", "nfl_display_name": "Holdout2",
               "nfl_position": "RB", "last_college_season": 2017, "college": "Test U",
               "match_strategy": "test"},
        "H1": {"nfl_pfr_player_id": "gsis-H1", "nfl_display_name": "Hist1",
               "nfl_position": "QB", "last_college_season": 2009, "college": "Test U",
               "match_strategy": "test"},
        "H2": {"nfl_pfr_player_id": "gsis-H2", "nfl_display_name": "Hist2",
               "nfl_position": "RB", "last_college_season": 2010, "college": "Test U",
               "match_strategy": "test"},
        "H3": {"nfl_pfr_player_id": "gsis-H3", "nfl_display_name": "Hist3",
               "nfl_position": "QB", "last_college_season": 2011, "college": "Test U",
               "match_strategy": "test"},
    })
    nfl = {
        "gsis-T1": {"career_fp": 1200, "peak3_fp_pg": 19.0, "seasons_played": 5,
                    "max_year": 2024, "min_year": 2017},
        "gsis-T2": {"career_fp": 500,  "peak3_fp_pg": 8.0,  "seasons_played": 3,
                    "max_year": 2024, "min_year": 2018},
        "gsis-H1": {"career_fp": 2000, "peak3_fp_pg": 22.0, "seasons_played": 8,
                    "max_year": 2017, "min_year": 2010},
        "gsis-H2": {"career_fp": 800,  "peak3_fp_pg": 12.0, "seasons_played": 5,
                    "max_year": 2015, "min_year": 2011},
        "gsis-H3": {"career_fp": 1500, "peak3_fp_pg": 16.0, "seasons_played": 6,
                    "max_year": 2018, "min_year": 2012},
    }
    s1 = bt.evaluate(corpus, resolver, nfl, ktc={}, holdout_classes=(2017, 2018, 2019))
    s2 = bt.evaluate(corpus, resolver, nfl, ktc={}, holdout_classes=(2017, 2018, 2019))
    # Drop the gates dict (status strings reproduce identically anyway)
    # and verify the raw numeric summary is byte-identical
    keep = ("hit_at_10", "bust_at_10", "spearman_rho", "ktc_h2h",
            "n_holdouts", "n_scored")
    assert {k: s1[k] for k in keep} == {k: s2[k] for k in keep}


# ---------------------------------------------------------------------------
# Hit-label propagation
# ---------------------------------------------------------------------------

def test_actual_hit_label_uses_pr4_thresholds():
    # The harness's actual-label logic delegates to bp._hit_label,
    # so the thresholds are pinned to PR 4's contract.
    assert bt._hit_actual({"peak3_fp_pg": 18.5, "seasons_played": 3}) == "elite"
    assert bt._hit_actual({"peak3_fp_pg": 14.0, "seasons_played": 4}) == "starter"
    assert bt._hit_actual({"peak3_fp_pg": 4.0, "seasons_played": 5}) == "bust"
    assert bt._hit_actual({"peak3_fp_pg": 4.0, "seasons_played": 1}) == "unknown"
    assert bt._hit_actual(None) == "unknown"


# ---------------------------------------------------------------------------
# Per-class breakdown
# ---------------------------------------------------------------------------

def test_per_class_breakdown_present():
    corpus = [
        _pv("Holdout17", "T1", 2016, "QB", adj=18.0),
        _pv("Holdout18", "T2", 2017, "RB", adj=18.0),
        _pv("Hist1", "H1", 2008, "QB", adj=20.0),
        _pv("Hist2", "H2", 2009, "RB", adj=18.0),
    ]
    resolver = NameCollisionResolver({
        "T1": {"nfl_pfr_player_id": "gsis-T1", "nfl_display_name": "Holdout17",
               "nfl_position": "QB", "last_college_season": 2016, "college": "Test U",
               "match_strategy": "test"},
        "T2": {"nfl_pfr_player_id": "gsis-T2", "nfl_display_name": "Holdout18",
               "nfl_position": "RB", "last_college_season": 2017, "college": "Test U",
               "match_strategy": "test"},
        "H1": {"nfl_pfr_player_id": "gsis-H1", "nfl_display_name": "Hist1",
               "nfl_position": "QB", "last_college_season": 2008, "college": "Test U",
               "match_strategy": "test"},
        "H2": {"nfl_pfr_player_id": "gsis-H2", "nfl_display_name": "Hist2",
               "nfl_position": "RB", "last_college_season": 2009, "college": "Test U",
               "match_strategy": "test"},
    })
    nfl = {
        "gsis-T1": {"career_fp": 100, "peak3_fp_pg": 5.0, "seasons_played": 2,
                    "max_year": 2018, "min_year": 2017},
        "gsis-T2": {"career_fp": 200, "peak3_fp_pg": 10.0, "seasons_played": 3,
                    "max_year": 2020, "min_year": 2018},
        "gsis-H1": {"career_fp": 1500, "peak3_fp_pg": 20.0, "seasons_played": 6,
                    "max_year": 2014, "min_year": 2009},
        "gsis-H2": {"career_fp": 900, "peak3_fp_pg": 16.0, "seasons_played": 5,
                    "max_year": 2014, "min_year": 2010},
    }
    s = bt.evaluate(corpus, resolver, nfl, ktc={}, holdout_classes=(2017, 2018))
    assert 2017 in s["per_class"]
    assert 2018 in s["per_class"]


# ---------------------------------------------------------------------------
# Synthetic full-pass guard — verifies the harness CAN report success
# ---------------------------------------------------------------------------

def test_format_report_renders():
    summary = {
        "n_holdouts": 100, "n_scored": 50,
        "hit_at_10": 0.30, "hit_at_10_n": 15, "hit_at_10_of": 50,
        "bust_at_10": 0.60, "bust_at_10_n": 30, "bust_at_10_of": 50,
        "hit_at_10_legacy": 0.10, "hit_at_10_legacy_n": 5,
        "bust_at_10_legacy": 0.36, "bust_at_10_legacy_n": 18,
        "position_cutoffs": {"QB": {"elite_cutoff": 16.7, "bust_cutoff": 4.0, "n": 62}},
        "elite_percentile": 80, "bust_percentile": 30,
        "spearman_rho": 0.40,
        "ktc_h2h": 0.55, "ktc_h2h_n": 11, "ktc_h2h_of": 20,
        "per_class": {2017: {"n_scored": 10, "top10_elite": 3,
                              "top10_elite_legacy": 1},
                      2018: {"n_scored": 10, "top10_elite": 2,
                              "top10_elite_legacy": 0}},
    }
    summary["gates"] = bt._evaluate_gates(summary)
    out = bt._format_report(summary)
    assert "Hit@10" in out
    assert "Spearman" in out
    assert "OVERALL: PASS" in out
    # Both gate regimes appear in the rendered report
    assert "Legacy gate" in out
    assert "Position-aware gate" in out
    assert "PRIMARY" in out


# ---------------------------------------------------------------------------
# Position-aware percentile methodology (PR 5 revision)
# ---------------------------------------------------------------------------

def test_percentile_linear_interpolation():
    # NumPy-compatible linear interpolation on sorted input.
    vs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert bt._percentile(vs, 0) == 1.0
    assert bt._percentile(vs, 100) == 5.0
    assert bt._percentile(vs, 50) == pytest.approx(3.0)
    # 25th percentile of [1..5] is 2.0 with linear interpolation
    assert bt._percentile(vs, 25) == pytest.approx(2.0)
    assert bt._percentile([], 50) == 0.0
    assert bt._percentile([7.0], 95) == 7.0


def test_compute_position_percentiles_per_position():
    rows = [
        {"position": "QB", "actual_peak3_fp_pg": 20.0},
        {"position": "QB", "actual_peak3_fp_pg": 15.0},
        {"position": "QB", "actual_peak3_fp_pg": 10.0},
        {"position": "QB", "actual_peak3_fp_pg": 5.0},
        {"position": "QB", "actual_peak3_fp_pg": 1.0},
        {"position": "TE", "actual_peak3_fp_pg": 12.0},
        {"position": "TE", "actual_peak3_fp_pg": 8.0},
        {"position": "TE", "actual_peak3_fp_pg": 4.0},
        {"position": "TE", "actual_peak3_fp_pg": 1.0},
        {"position": "TE", "actual_peak3_fp_pg": 0.0},
        # non-skill positions filtered out
        {"position": "OL", "actual_peak3_fp_pg": 99.0},
    ]
    cuts = bt.compute_position_percentiles(rows, elite_pct=80, bust_pct=30)
    assert set(cuts.keys()) == {"QB", "TE"}
    # QB 80th percentile of [1,5,10,15,20] is rank=0.8*4=3.2 →
    # 15 + 0.2*(20-15) = 16.0 (NumPy-compatible linear interpolation)
    assert cuts["QB"]["elite_cutoff"] == pytest.approx(16.0, abs=0.01)
    # TE elite cutoff is much lower than QB — position-aware behaviour
    assert cuts["TE"]["elite_cutoff"] < cuts["QB"]["elite_cutoff"]
    assert cuts["QB"]["n"] == 5
    assert cuts["TE"]["n"] == 5


def test_position_aware_label_classification():
    cuts = {
        "QB": {"elite_cutoff": 17.0, "bust_cutoff": 5.0, "n": 5},
        "TE": {"elite_cutoff": 9.0, "bust_cutoff": 1.5, "n": 5},
    }
    # Elite — above 80th percentile
    assert bt.position_aware_label("QB", 18.0, 5, cuts) == "elite"
    # Elite at exactly cutoff
    assert bt.position_aware_label("QB", 17.0, 5, cuts) == "elite"
    # Bust — below 30th percentile (no seasons floor for primary gate)
    assert bt.position_aware_label("QB", 2.0, 1, cuts) == "bust"
    # Starter — between cutoffs
    assert bt.position_aware_label("QB", 10.0, 4, cuts) == "starter"
    # TE cutoffs are different (position-aware): 4.0 fp/g is starter for
    # TE but bust for QB
    assert bt.position_aware_label("TE", 4.0, 3, cuts) == "starter"
    assert bt.position_aware_label("QB", 4.0, 3, cuts) == "bust"
    # Unknown position
    assert bt.position_aware_label("K", 100.0, 10, cuts) == "unknown"


def test_position_aware_optional_seasons_floor():
    # Optional min_seasons_for_bust floor classifies short careers as
    # 'unknown' rather than 'bust'. Off by default for the primary gate.
    cuts = {"QB": {"elite_cutoff": 17.0, "bust_cutoff": 5.0, "n": 5}}
    # Default (floor=0): a 1-season flameout is a bust
    assert bt.position_aware_label("QB", 2.0, 1, cuts) == "bust"
    # With floor=3: same player is now 'unknown'
    assert bt.position_aware_label("QB", 2.0, 1, cuts,
                                   min_seasons_for_bust=3) == "unknown"
    # With floor=3: 4 seasons + low peak3 stays a bust
    assert bt.position_aware_label("QB", 2.0, 4, cuts,
                                   min_seasons_for_bust=3) == "bust"


def test_summary_exposes_position_cutoffs_and_legacy_metrics():
    """After evaluate(), the summary carries both the new position-aware
    metrics AND the legacy absolute-threshold metrics for comparison.
    """
    corpus = [
        _pv("Holdout1", "T1", 2017, "QB", adj=18.0),
        _pv("Holdout2", "T2", 2018, "RB", adj=17.0),
        _pv("Hist1", "H1", 2010, "QB", adj=20.0),
        _pv("Hist2", "H2", 2011, "RB", adj=18.0),
    ]
    resolver = NameCollisionResolver({
        "T1": {"nfl_pfr_player_id": "gsis-T1", "nfl_display_name": "Holdout1",
               "nfl_position": "QB", "last_college_season": 2016, "college": "Test U",
               "match_strategy": "test"},
        "T2": {"nfl_pfr_player_id": "gsis-T2", "nfl_display_name": "Holdout2",
               "nfl_position": "RB", "last_college_season": 2017, "college": "Test U",
               "match_strategy": "test"},
        "H1": {"nfl_pfr_player_id": "gsis-H1", "nfl_display_name": "Hist1",
               "nfl_position": "QB", "last_college_season": 2009, "college": "Test U",
               "match_strategy": "test"},
        "H2": {"nfl_pfr_player_id": "gsis-H2", "nfl_display_name": "Hist2",
               "nfl_position": "RB", "last_college_season": 2010, "college": "Test U",
               "match_strategy": "test"},
    })
    nfl = {
        "gsis-T1": {"career_fp": 1200, "peak3_fp_pg": 19.0, "seasons_played": 5,
                    "max_year": 2024, "min_year": 2017},
        "gsis-T2": {"career_fp": 500,  "peak3_fp_pg": 8.0,  "seasons_played": 3,
                    "max_year": 2024, "min_year": 2018},
        "gsis-H1": {"career_fp": 2000, "peak3_fp_pg": 22.0, "seasons_played": 8,
                    "max_year": 2017, "min_year": 2010},
        "gsis-H2": {"career_fp": 800,  "peak3_fp_pg": 12.0, "seasons_played": 5,
                    "max_year": 2015, "min_year": 2011},
    }
    s = bt.evaluate(corpus, resolver, nfl, ktc={},
                    holdout_classes=(2017, 2018, 2019))
    # Position-aware metrics live at the primary keys
    assert "hit_at_10" in s
    assert "bust_at_10" in s
    # Legacy metrics are also surfaced for comparison
    assert "hit_at_10_legacy" in s
    assert "bust_at_10_legacy" in s
    # Cutoffs by position are exposed for the docs / UI
    assert "position_cutoffs" in s
    assert s["elite_percentile"] == 80
    assert s["bust_percentile"] == 30
