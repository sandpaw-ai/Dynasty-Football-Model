"""v2.3.5 unit tests for the age-aware similarity fix.

Two engines, two bugs, one PR. These tests cover the synthetic-vector
checks that don't need the full nflverse corpus to run. The
end-to-end snapshot test (Johnny Wilson must not comp with Steve Smith
Sr. / Santana Moss) lives in test_v2_3_5_snapshot.py and writes its
output to tests/snapshots/v2.3.5_comp_shifts.json.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.engine import fantasy_arc_similarity as cumulative
from dynasty.engine import rookie_nfl_fp_arc as rookie
from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint


BASE_FORMAT = "sf_ppr"


# ---------------------------------------------------------------------------
# Cumulative-engine vector dimension + age dimension presence
# ---------------------------------------------------------------------------

def test_cumulative_vector_dim_is_11():
    """v2.3.5: the cumulative-engine vector is 11-dim (was 10-dim)."""
    assert cumulative.VECTOR_DIM == 11
    assert len(cumulative.FEATURE_WEIGHTS) == 11


def test_cumulative_age_weight_is_strong():
    """v2.3.5: v[10] (current_age) is a STRONG feature weight, not a
    tie-breaker. Anything below 1.0 would put it in the same bucket as
    pre-v2.3.5 noise dimensions."""
    assert cumulative.FEATURE_WEIGHTS[10] >= 3.0
    assert cumulative.AGE_SCALE > 0


def _synth_arc(player_id: str, position: str, ages: list[int],
               fp_per_game: float) -> CareerArc:
    """Build a synthetic CareerArc with N seasons at the given ages and
    a flat per-season fp/G. Used to construct controlled distance tests.
    """
    seasons = []
    for i, age in enumerate(ages):
        seasons.append(SeasonArcPoint(
            season=2000 + i,
            age=age,
            games=16,
            era=4,
            fp_total={BASE_FORMAT: fp_per_game * 16},
            fp_per_game={BASE_FORMAT: fp_per_game},
        ))
    return CareerArc(
        player_id=player_id,
        name=player_id,
        position=position,
        last_season=2000 + len(ages) - 1,
        rookie_season=2000,
        retired=True,
        is_long_arc=True,
        career_arc=seasons,
        career_total_fp={BASE_FORMAT: fp_per_game * 16 * len(ages)},
        peak_season_fp_per_game={BASE_FORMAT: fp_per_game},
        peak_3yr_fp_per_game={BASE_FORMAT: fp_per_game},
        career_avg_fp_per_game={BASE_FORMAT: fp_per_game},
    )


def test_cumulative_age_dimension_proportional_to_age_gap():
    """Two arcs identical in fp/G profile but different in snapshot age
    should produce strictly increasing distance as the age gap widens.
    """
    # Empty percentile table — synthetic test, percentile dim falls back
    # to 0.5 (the default) for unknown (position, stage) buckets.
    pct = cumulative.CareerStagePercentileTable(by_pos_stage={})

    target = _synth_arc("target", "WR", [22], 15.0)
    same_age = _synth_arc("same", "WR", [22], 15.0)
    one_year = _synth_arc("plus1", "WR", [23], 15.0)
    three_year = _synth_arc("plus3", "WR", [25], 15.0)

    tv = cumulative.build_arc_vector(target, 22, BASE_FORMAT, pct)
    v_same = cumulative.build_arc_vector(same_age, 22, BASE_FORMAT, pct)
    v_one = cumulative.build_arc_vector(one_year, 23, BASE_FORMAT, pct)
    v_three = cumulative.build_arc_vector(three_year, 25, BASE_FORMAT, pct)

    assert tv is not None and v_same is not None
    assert v_one is not None and v_three is not None

    d_same = cumulative._weighted_distance(tv.values, v_same.values)
    d_one = cumulative._weighted_distance(tv.values, v_one.values)
    d_three = cumulative._weighted_distance(tv.values, v_three.values)

    assert d_same == pytest.approx(0.0, abs=1e-9)
    assert d_one > d_same
    assert d_three > d_one
    # The 3-year gap should be meaningfully larger than the 1-year gap
    # (the whole point of the fix). At AGE_SCALE=0.5, weight=5.0:
    #   1yr gap squared contribution: 5.0 * (0.5)^2 = 1.25 -> sqrt ~1.12
    #   3yr gap squared contribution: 5.0 * (1.5)^2 = 11.25 -> sqrt ~3.35
    assert d_three > 2 * d_one


def test_cumulative_age_dominates_marginal_fp_match():
    """A same-age but slightly-different-fp comp should rank closer than
    an exact-fp but 3-year-older comp. Phil's bug: 24yo Wilson was
    comping with 22yo Smith Sr. on same fp; the fix has the same-age
    comp win."""
    pct = cumulative.CareerStagePercentileTable(by_pos_stage={})

    target = _synth_arc("target", "WR", [24], 5.0)
    # Same age, fp/G off by 0.5
    same_age_close_fp = _synth_arc("same_age", "WR", [24], 5.5)
    # Different age (3 yrs younger), exact same fp/G
    diff_age_same_fp = _synth_arc("diff_age", "WR", [21], 5.0)

    tv = cumulative.build_arc_vector(target, 24, BASE_FORMAT, pct)
    v_same_age = cumulative.build_arc_vector(same_age_close_fp, 24, BASE_FORMAT, pct)
    v_diff_age = cumulative.build_arc_vector(diff_age_same_fp, 21, BASE_FORMAT, pct)

    d_same_age = cumulative._weighted_distance(tv.values, v_same_age.values)
    d_diff_age = cumulative._weighted_distance(tv.values, v_diff_age.values)

    # The same-age comp (slight fp gap) should be CLOSER than the diff-age
    # comp (no fp gap but 3-year age delta). This is the entire point of
    # the v2.3.5 fix.
    assert d_same_age < d_diff_age, (
        f"Age fix broken: same-age comp distance {d_same_age:.3f} should be "
        f"less than diff-age comp distance {d_diff_age:.3f}"
    )


# ---------------------------------------------------------------------------
# Rookie-engine weight + corpus tests
# ---------------------------------------------------------------------------

def test_rookie_age_weight_meaningful():
    """v2.3.5: FEATURE_WEIGHTS[9] (age_at_rookie_year) was 0.2 pre-fix,
    bumped to 2.5 so age dominates a small fp/G match."""
    assert rookie.FEATURE_WEIGHTS[9] >= 2.0


def _synth_rookie_arc(pid: str, name: str, position: str,
                       rookie_season: int, rookie_age: int,
                       rookie_fp_per_game: float, rookie_games: int = 16,
                       post_rookie_seasons: int = 0,
                       post_rookie_fp_per_game: float = 10.0) -> CareerArc:
    seasons = [SeasonArcPoint(
        season=rookie_season,
        age=rookie_age,
        games=rookie_games,
        era=4,
        fp_total={BASE_FORMAT: rookie_fp_per_game * rookie_games},
        fp_per_game={BASE_FORMAT: rookie_fp_per_game},
    )]
    for i in range(post_rookie_seasons):
        seasons.append(SeasonArcPoint(
            season=rookie_season + 1 + i,
            age=rookie_age + 1 + i,
            games=16,
            era=4,
            fp_total={BASE_FORMAT: post_rookie_fp_per_game * 16},
            fp_per_game={BASE_FORMAT: post_rookie_fp_per_game},
        ))
    return CareerArc(
        player_id=pid,
        name=name,
        position=position,
        last_season=rookie_season + post_rookie_seasons,
        rookie_season=rookie_season,
        retired=True,
        is_long_arc=False,
        career_arc=seasons,
    )


def test_bust_aware_corpus_includes_year1_only_players():
    """v2.3.5: with bust_aware=True (default) and the new default
    require_post_rookie_season=False, a year-1-only bust appears in the
    corpus. Pre-v2.3.5 it was filtered out."""
    bust = _synth_rookie_arc(
        "bust1", "Bust Player", "WR",
        rookie_season=2010, rookie_age=22, rookie_fp_per_game=5.0,
        post_rookie_seasons=0,
    )
    survivor = _synth_rookie_arc(
        "surv1", "Surv Player", "WR",
        rookie_season=2010, rookie_age=22, rookie_fp_per_game=5.0,
        post_rookie_seasons=4, post_rookie_fp_per_game=10.0,
    )
    raw = {
        ("bust1", 2010): {"receiving_yards": 200},
        ("surv1", 2010): {"receiving_yards": 200},
    }

    # Default v2.3.5 behaviour: bust appears.
    corpus = rookie.build_rookie_corpus(
        arcs=[bust, survivor],
        raw_stats_by_pid_season=raw,
        league_format=BASE_FORMAT,
    )
    pids = {p.player_id for p in corpus}
    assert "bust1" in pids
    assert "surv1" in pids
    # Bust has zero post-rookie fp.
    bust_profile = next(p for p in corpus if p.player_id == "bust1")
    assert bust_profile.post_rookie_total_fp == 0.0

    # Back-compat: bust_aware=False excludes the bust.
    legacy = rookie.build_rookie_corpus(
        arcs=[bust, survivor],
        raw_stats_by_pid_season=raw,
        league_format=BASE_FORMAT,
        bust_aware=False,
    )
    legacy_pids = {p.player_id for p in legacy}
    assert "bust1" not in legacy_pids
    assert "surv1" in legacy_pids

    # Belt-and-braces: explicit require_post_rookie_season=True also excludes.
    legacy2 = rookie.build_rookie_corpus(
        arcs=[bust, survivor],
        raw_stats_by_pid_season=raw,
        league_format=BASE_FORMAT,
        require_post_rookie_season=True,
    )
    assert "bust1" not in {p.player_id for p in legacy2}


def test_bust_rate_in_comps_reported():
    """v2.3.5: RookieProjectionResult.bust_rate_in_comps reports the
    fraction of top-K comps with no realised year-2+ season."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(rookie.RookieProjectionResult)}
    assert "bust_rate_in_comps" in field_names

    # Build a target with one bust and one survivor in the corpus and
    # confirm the field is populated.
    target = _synth_rookie_arc(
        "target", "Target Player", "WR",
        rookie_season=2024, rookie_age=24, rookie_fp_per_game=5.0,
        post_rookie_seasons=0,
    )
    bust = _synth_rookie_arc(
        "bust1", "Bust Player", "WR",
        rookie_season=2010, rookie_age=24, rookie_fp_per_game=5.0,
        post_rookie_seasons=0,
    )
    survivor = _synth_rookie_arc(
        "surv1", "Surv Player", "WR",
        rookie_season=2011, rookie_age=24, rookie_fp_per_game=5.5,
        post_rookie_seasons=4, post_rookie_fp_per_game=12.0,
    )
    raw = {
        ("target", 2024): {"receiving_yards": 200},
        ("bust1", 2010): {"receiving_yards": 200},
        ("surv1", 2011): {"receiving_yards": 220},
    }
    corpus = rookie.build_rookie_corpus(
        arcs=[bust, survivor],
        raw_stats_by_pid_season=raw,
        league_format=BASE_FORMAT,
    )
    assert len(corpus) == 2

    result = rookie.project_rookie(
        target_arc=target,
        target_rookie_stats=raw[("target", 2024)],
        target_rookie_age=24,
        target_rookie_games=16,
        rookie_corpus=corpus,
        league_format=BASE_FORMAT,
        k=20,
    )
    assert result.n_comps == 2
    # 1 of 2 comps busted -> bust_rate = 0.5.
    assert result.bust_rate_in_comps == pytest.approx(0.5)


def test_rookie_age_gap_dominates_small_fp_gap():
    """With the v2.3.5 age weight bump, a same-age slight-fp-gap comp
    should rank closer than a 3-year-younger same-fp comp."""
    # Build vectors directly via the engine's helper.
    # Same target: WR, age 24, 5.0 fp/G.
    target_vec = [
        5.0,         # v[0] fp/G
        16.0 / 17,   # v[1] games/17
        12.5,        # v[2] passing yards/G (just nonzero)
        2.5,
        12.5,
        0.0,
        0.0,
        0.05,
        0.0,         # v[8] completion rate
        24.0,        # v[9] age
        3.0,         # v[10] position WR
    ]
    same_age_close_fp = target_vec.copy()
    same_age_close_fp[0] = 5.5  # +0.5 fp/G

    diff_age_same_fp = target_vec.copy()
    diff_age_same_fp[9] = 21.0  # 3 yrs younger

    d_same_age = rookie._weighted_distance(target_vec, same_age_close_fp)
    d_diff_age = rookie._weighted_distance(target_vec, diff_age_same_fp)

    # 3-yr age gap: 2.5 * 9 = 22.5 -> sqrt ~4.74
    # 0.5 fp/G gap: 8.0 * 0.25 = 2.0 -> sqrt ~1.41
    assert d_diff_age > d_same_age, (
        f"Rookie age weight bump broken: 3-yr-younger same-fp distance "
        f"{d_diff_age:.3f} should be > same-age 0.5fp-off distance "
        f"{d_same_age:.3f}"
    )


def test_busts_contribute_zero_to_projection():
    """v2.3.5 invariant: bust comps (no year 2+) contribute zero
    realised post-rookie fantasy points to the projection. This is what
    makes bust_aware=True safe \u2014 the comp count includes busts, but the
    projection naturally pulls down to reflect the bust-heavy pool."""
    bust = _synth_rookie_arc(
        "bust1", "Bust", "WR",
        rookie_season=2010, rookie_age=24, rookie_fp_per_game=5.0,
        post_rookie_seasons=0,
    )
    pts, n = rookie.project_year_2_plus(
        comp_arc=bust, rookie_season=2010, league_format=BASE_FORMAT,
    )
    assert pts == 0.0
    assert n == 0
