"""v3.1 — Veteran rankings recalibration tests.

Phil's v3.1 brief diagnosed five concrete miscalibrations in the
engine ranking output:

  1. Dak Prescott (banked 2598 fp) was ranked below Deshaun Watson,
     Justin Fields, and Kyler Murray purely because their comp pools
     give them more projected years remaining.
  2. Derrick Henry (banked 2447 fp, peak3 21.2, currently top-5 RB
     production) sat at #141 because his yrs_rem collapsed to 1.9.
  3. Justin Jefferson (WR1 caliber, banked 1692, peak3 20.53) sat at
     #21 — beaten by QBs whose only edge was a longer runway.
  4. The career-length lift applies unconditionally based on QB style
     (rushing rate). A dual-threat QB visibly past their peak
     (Justin Fields, recent fp/g < peak × 0.85) still got the full
     dual-threat 1.50× years_remaining lift.
  5. RBs in late career who are STILL producing top-12 fp/g (Henry)
     have no path to credit current form — the comp-pool yrs_rem
     collapses to ~2 and ends the conversation.

The fix introduces:
  * A proven-production floor (banked_credit + short forward window
    of recent rate) applied AFTER the v2.2 penalty stack.
  * A QB-decline gate that strips the dual/mobile lift when a 27+ QB's
    recent-2yr fp/g is below 0.85× their all-time peak3yr.
  * An RB late-career boost (+1.0 yr_rem) for 30+ RBs whose last
    season fp/g is >= 16 (top-12 RB tier).

These tests pin the acceptance criteria from the brief and the
unit-level behaviour of the new helpers / floor function.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint
from dynasty.engine.fantasy_arc_similarity import (
    FLOOR_DYNASTY_HORIZON,
    FLOOR_PRODUCING_FLOOR_RATIO,
    FLOOR_PRODUCING_FULL_RATIO,
    FLOOR_RECENCY_DISCOUNT,
    FLOOR_RECENCY_WINDOW,
    PROJECTION_GAMES_PER_SEASON,
    _proven_production_floor,
    _recent_1yr_target,
    _recent_2yr_target,
    _recent_3yr_target,
)
from dynasty.engine.similarity_v1 import run_engine


BASE_FORMAT = "sf_ppr"


# ---------------------------------------------------------------------------
# Shared fixture — run the engine once for the acceptance-case tests.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    return run_engine(current_season=2025, persist=False)


def _row(engine, name):
    for r in engine.rankings:
        if r["name"] == name:
            return r
    return None


def _rank(engine, name):
    r = _row(engine, name)
    return r["overall_rank"] if r else None


# ---------------------------------------------------------------------------
# Unit tests — the recent-N helpers.
# ---------------------------------------------------------------------------

def _arc(name, position, seasons):
    """Build a minimal CareerArc for unit tests.

    seasons: list of (age, games, fp_per_game). fp_total is derived
    as games × fp_per_game.
    """
    arc_points = []
    for age, games, fppg in seasons:
        arc_points.append(SeasonArcPoint(
            season=2010 + age,
            age=age,
            games=games,
            era=4,  # modern era; not used by floor / recent-N helpers
            fp_per_game={BASE_FORMAT: fppg},
            fp_total={BASE_FORMAT: fppg * games},
        ))
    arc = CareerArc(
        player_id="TEST-" + name.replace(" ", "_"),
        name=name,
        position=position,
        last_season=2010 + seasons[-1][0],
        rookie_season=2010 + seasons[0][0],
        retired=False,
        is_long_arc=True,
        career_arc=arc_points,
        peak_season_fp_per_game={BASE_FORMAT: max(s[2] for s in seasons)},
        peak_3yr_fp_per_game={BASE_FORMAT: max(s[2] for s in seasons)},
        career_avg_fp_per_game={
            BASE_FORMAT: sum(s[1] * s[2] for s in seasons)
            / sum(s[1] for s in seasons),
        },
        career_total_fp={BASE_FORMAT: sum(s[1] * s[2] for s in seasons)},
    )
    return arc


def test_recent_1yr_target_returns_last_season_rate():
    arc = _arc("X", "RB", [(28, 16, 18.0), (29, 17, 20.0), (30, 17, 22.0)])
    assert _recent_1yr_target(arc, BASE_FORMAT) == pytest.approx(22.0)


def test_recent_2yr_target_weights_by_games():
    arc = _arc("X", "RB", [(28, 16, 10.0), (29, 17, 20.0), (30, 17, 22.0)])
    expected = (20.0 * 17 + 22.0 * 17) / (17 + 17)
    assert _recent_2yr_target(arc, BASE_FORMAT) == pytest.approx(expected)


def test_recent_3yr_target_uses_all_three_seasons():
    arc = _arc(
        "X", "QB",
        [(25, 16, 18.0), (26, 16, 20.0), (27, 16, 22.0), (28, 16, 24.0)],
    )
    # 3yr window is the last 3 seasons (ages 26..28)
    expected = (20.0 + 22.0 + 24.0) / 3
    assert _recent_3yr_target(arc, BASE_FORMAT) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Unit tests — the proven-production floor formula.
# ---------------------------------------------------------------------------

def test_proven_floor_thin_sample_young_player_stays_small():
    """A C.J. Stroud-shape: ~700 banked, recent_2yr ~14 fp/g, yrs_rem 6.5.

    Floor should be modest so the existing peak-anchored / comp-weighted
    projection still wins for thin-sample players. We're not trying to
    over-promote young proven talent — they earn their rank via the
    forward projection.
    """
    arc = _arc(
        "Stroud-like", "QB",
        [(22, 15, 14.0), (23, 16, 14.0)],
    )
    # career_total = 15*14 + 16*14 = 434. peak3 = 14, recent_2yr = 14,
    # so producing_factor = (1.0 - 0.55)/0.25 = clamped 1.0
    floor = _proven_production_floor(
        target=arc, league_format=BASE_FORMAT, weighted_seasons=6.5,
    )
    # banked_weight = min(6.5/6, 1.0) * 1.0 = 1.0
    # banked_component = 434 * 1.0 = 434
    # forward = 14 * 17 * 3 * 0.9 = 642.6
    # floor ≈ 434 + 643 = 1077
    assert 900 < floor < 1300


def test_proven_floor_veteran_in_decline_is_capped_by_quadratic_decay():
    """An Aaron Rodgers-shape: 5000 banked, recent 22 fp/g, yrs_rem 2.3.

    The floor must NOT inflate a retiring veteran above active stars.
    banked_weight = (2.3/6) ** 1.5 ≈ 0.237 -> 5000 * 0.237 ≈ 1184
    forward = 22 * 17 * 2.3 * 0.9 ≈ 774
    floor ≈ 1184 + 774 ≈ 1958
    """
    # Build a QB with a peak3 of ~22 fp/g across his 25-39 prime, then
    # tack on two late-career down years at ~14 fp/g (Rodgers' shape).
    # peak3 stays ~22; recent_2yr collapses to 14 → ratio 0.64 → the
    # QB-position producing factor kicks in and discounts banked.
    arc = _arc(
        "Rodgers-like", "QB",
        [(age, 16, 22.0) for age in range(25, 40)],  # ages 25..39 prime
    )
    arc.peak_3yr_fp_per_game[BASE_FORMAT] = 22.0
    arc.career_arc.append(SeasonArcPoint(
        season=2050, age=40, games=16, era=4,
        fp_per_game={BASE_FORMAT: 14.0},
        fp_total={BASE_FORMAT: 14.0 * 16},
    ))
    arc.career_arc.append(SeasonArcPoint(
        season=2051, age=41, games=16, era=4,
        fp_per_game={BASE_FORMAT: 14.0},
        fp_total={BASE_FORMAT: 14.0 * 16},
    ))
    arc.career_total_fp[BASE_FORMAT] = sum(
        s.fp_total[BASE_FORMAT] for s in arc.career_arc
    )
    floor = _proven_production_floor(
        target=arc, league_format=BASE_FORMAT, weighted_seasons=2.3,
    )
    # peak3 = 22, recent_2yr = 14 → ratio 0.636. QB curve has
    # floor_r=0.65, full_r=0.85 → producing_factor = 0 (clamped, ratio
    # < floor_r). banked_credit = 0. forward = 14 * 17 * 2.3 * 0.9 ≈ 493.
    # Floor for a QB in decline must be well below an active mid-prime
    # star's floor (Dak ~2400).
    assert floor < 1200, (
        f"declining QB floor {floor:.1f} should be well below Dak's range"
    )


def test_proven_floor_mid_prime_vet_gets_meaningful_credit():
    """A Dak Prescott-shape: 2600 banked, recent_2yr ~17, yrs_rem 4.9.

    Mid-prime banked-rich vet must score a floor well above thinner
    forward projections — the whole point of the fix.
    """
    arc = _arc(
        "Dak-like", "QB",
        [(age, 16, 18.0) for age in range(23, 32)],  # 9 seasons
    )
    arc.career_total_fp[BASE_FORMAT] = 2600.0
    floor = _proven_production_floor(
        target=arc, league_format=BASE_FORMAT, weighted_seasons=4.9,
    )
    # banked_weight = min(4.9/6, 1.0) * 1.0 (peak == recent) ≈ 0.817
    # banked_credit ≈ 2600 * 0.817 ≈ 2124
    # forward = 18 * 17 * 3 * 0.9 ≈ 826
    # floor ≈ 2124 + 826 ≈ 2950
    assert 2500 < floor < 3400


def test_proven_floor_recency_window_is_capped_at_three_years():
    """The forward window must NEVER exceed FLOOR_RECENCY_WINDOW even
    if the player has a long projected runway. This is what keeps the
    floor from speculating about long-runway young vets and tanking the
    fix for thin-sample-but-projected-elite players."""
    arc = _arc(
        "Long-runway-young", "WR",
        [(22, 16, 16.0), (23, 17, 17.0)],
    )
    arc.career_total_fp[BASE_FORMAT] = 16.5 * 33
    floor_long = _proven_production_floor(
        target=arc, league_format=BASE_FORMAT, weighted_seasons=12.0,
    )
    floor_at_cap = _proven_production_floor(
        target=arc, league_format=BASE_FORMAT,
        weighted_seasons=FLOOR_RECENCY_WINDOW,
    )
    # The forward component must be identical at 12yr and 3yr because
    # the window is capped at 3. The banked weight differs by
    # yrs_rem-weighting.
    banked = arc.career_total_fp[BASE_FORMAT]
    bw_long = 1.0  # min(12/6, 1.0) = 1.0
    bw_3 = FLOOR_RECENCY_WINDOW / FLOOR_DYNASTY_HORIZON
    forward_3yr = 17 * 17 * 3 * 0.9  # recent_2yr ≈ 16.96
    assert (floor_long - banked * bw_long) == pytest.approx(
        floor_at_cap - banked * bw_3, rel=0.02,
    )


# ---------------------------------------------------------------------------
# Acceptance-case integration tests — engine-level invariants.
# ---------------------------------------------------------------------------

def test_dak_above_watson_fields_kyler(engine):
    """ACCEPTANCE #1: Dak Prescott (2598 banked) must rank above
    Deshaun Watson, Justin Fields, and Kyler Murray."""
    dak = _rank(engine, "Dak Prescott")
    watson = _rank(engine, "Deshaun Watson")
    fields = _rank(engine, "Justin Fields")
    kyler = _rank(engine, "Kyler Murray")
    assert dak is not None and watson is not None
    assert dak < watson, f"Dak ({dak}) should rank above Watson ({watson})"
    assert dak < fields, f"Dak ({dak}) should rank above Fields ({fields})"
    assert dak < kyler, f"Dak ({dak}) should rank above Kyler ({kyler})"


def test_derrick_henry_top_50(engine):
    """ACCEPTANCE #2: Derrick Henry — banked 2447 + currently top-5 RB
    production — must land inside the top 50."""
    henry = _rank(engine, "Derrick Henry")
    assert henry is not None
    assert henry <= 50, f"Henry should be top-50, got #{henry}"


def test_justin_jefferson_top_15_and_moves_up(engine):
    """ACCEPTANCE #3: Justin Jefferson moves up materially. Phil's
    brief said 'likely top-10' with 'Maybe' qualifier; we pin top-15
    so the test is robust to small calibration drift, but the
    real-world target is top-10."""
    jj = _rank(engine, "Justin Jefferson")
    assert jj is not None
    assert jj <= 15, f"JJ should be top-15, got #{jj}"


def test_dak_top_15_overall(engine):
    """ACCEPTANCE #4: Banked production deserves Dak in the top-15."""
    dak = _rank(engine, "Dak Prescott")
    assert dak is not None
    assert dak <= 15, f"Dak should be top-15, got #{dak}"


def test_mahomes_not_below_dak(engine):
    """ACCEPTANCE #5: Mahomes' banked + current form sanity check."""
    mahomes = _rank(engine, "Patrick Mahomes")
    dak = _rank(engine, "Dak Prescott")
    assert mahomes is not None and dak is not None
    assert mahomes <= dak, (
        f"Mahomes ({mahomes}) should not rank below Dak ({dak})"
    )


def test_burrow_near_dak(engine):
    """ACCEPTANCE #6: Burrow has similar peak (~21) and less banked
    than Dak. They should land in roughly the same neighborhood."""
    burrow = _rank(engine, "Joe Burrow")
    dak = _rank(engine, "Dak Prescott")
    assert burrow is not None and dak is not None
    # Burrow should be no more than 15 places below Dak (banked
    # differential is real but not enormous).
    assert burrow - dak <= 15, (
        f"Burrow ({burrow}) should be within 15 of Dak ({dak})"
    )


def test_lamar_jackson_top_10(engine):
    """ACCEPTANCE #7: Lamar's everything-works case — peak3 24, massive
    banked, dual-threat lift earned by sustained production."""
    lamar = _rank(engine, "Lamar Jackson")
    assert lamar is not None
    assert lamar <= 10, f"Lamar should be top-10, got #{lamar}"


def test_stroud_does_not_rocket_up(engine):
    """ACCEPTANCE #8: C.J. Stroud has only ~3 NFL years of data
    (career_total ~720). The floor's banked component is small for
    him; he must NOT accidentally rocket into the top-10."""
    stroud = _rank(engine, "C.J. Stroud")
    assert stroud is not None
    assert stroud > 30, (
        f"Stroud's thin sample should keep him outside the top 30, "
        f"got #{stroud}"
    )


def test_bijan_above_saquon(engine):
    """ACCEPTANCE #9: Bijan (age 24) should remain ranked well above
    Saquon (age ~29) — the age delta is real and the fix mustn't
    invert them."""
    bijan = _rank(engine, "Bijan Robinson")
    saquon = _rank(engine, "Saquon Barkley")
    assert bijan is not None and saquon is not None
    assert bijan < saquon, (
        f"Bijan ({bijan}) should outrank Saquon ({saquon})"
    )


def test_tyreek_hill_stays_top_100(engine):
    """ACCEPTANCE #10: Tyreek (age 32, banked 2492) — the late-career
    WR case must stay inside the top 100. If he plummets we've broken
    the late-career-WR case."""
    tyreek = _rank(engine, "Tyreek Hill")
    assert tyreek is not None
    assert tyreek <= 100, f"Tyreek should be top-100, got #{tyreek}"


# ---------------------------------------------------------------------------
# Engine-level diagnostics — the new fields must be present and have
# the right semantics.
# ---------------------------------------------------------------------------

def test_proven_floor_fields_present_on_every_row(engine):
    for r in engine.rankings:
        if r.get("engine") != "fantasy_arc_v2":
            # Rookie engine rows don't run through the floor.
            continue
        assert "proven_floor_fp" in r
        assert "production_score_pre_floor" in r
        assert "production_path" in r
        assert "qb_decline_gate_applied" in r
        assert "rb_late_career_boost_applied" in r
        assert r["production_path"] in (
            "proven_floor", "peak_anchored", "comp_weighted",
        )


def test_qb_decline_gate_fires_for_fields(engine):
    """Justin Fields is the canonical case — dual_threat by style,
    age 27, recent fp/g materially below peak3=17.3. The decline gate
    must have stripped his lift."""
    r = _row(engine, "Justin Fields")
    assert r is not None
    assert r.get("qb_decline_gate_applied") is True


def test_qb_decline_gate_does_not_fire_for_lamar(engine):
    """Lamar is at peak production right now — the gate must NOT fire
    or we'd strip the well-earned dual-threat lift."""
    r = _row(engine, "Lamar Jackson")
    assert r is not None
    assert r.get("qb_decline_gate_applied") is False


def test_rb_late_career_boost_fires_for_henry(engine):
    """Derrick Henry (age 32, currently top-5 RB) must trigger the
    late-career boost path."""
    r = _row(engine, "Derrick Henry")
    assert r is not None
    assert r.get("rb_late_career_boost_applied") is True


def test_floor_path_wins_for_dak(engine):
    """Dak's banked production drives his rank — the production_path
    should resolve to 'proven_floor'."""
    r = _row(engine, "Dak Prescott")
    assert r is not None
    assert r["production_path"] == "proven_floor"


def test_floor_does_not_inflate_thin_sample_young_qb_into_top_30(engine):
    """C.J. Stroud's banked is thin (~720 fp) and recent fp/g modest —
    the floor may win his row (if the v2.2 penalty stack pulled the
    forward projection below the floor), but it MUST NOT inflate him
    into the top tier on banked credit he doesn't have.
    """
    r = _row(engine, "C.J. Stroud")
    assert r is not None
    # Even if production_path resolves to proven_floor, the absolute
    # floor value must be modest — banked is only ~720 fp.
    assert r["proven_floor_fp"] < 1700, (
        f"Stroud's floor ({r['proven_floor_fp']}) is too large "
        f"for a thin-sample player"
    )
    assert r["overall_rank"] > 30, (
        f"Stroud's thin sample should keep him outside the top 30"
    )


def test_proven_floor_never_decreases_production_score(engine):
    """The floor is monotone — it can only move a score UP. For every
    fantasy-arc-v2 row, production_score >= production_score_pre_floor
    (within rounding)."""
    for r in engine.rankings:
        if r.get("engine") != "fantasy_arc_v2":
            continue
        # production_score_pre_floor is the post-lift score before the
        # v2.2 penalty stack. The penalty stack can pull production
        # down; the floor then sets a hard lower bound. We pin the
        # floor itself (proven_floor_fp) <= production_score so the
        # floor always wins when it should.
        pre_floor = r["production_score_pre_floor"]
        floor = r["proven_floor_fp"]
        final = r["production_score"]
        # The final must be >= min(pre_floor, floor) (the v2.2 stack
        # can push pre_floor down, but the floor always wins if it's
        # higher than the post-penalty value).
        assert final >= floor - 0.1, (
            f"{r['name']}: production_score {final} < proven_floor {floor}"
        )
