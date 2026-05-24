"""Tests for v2.3.3-final (Phil 2026-05-22).

Phil clarified the directive after I implemented v2.3.3 incorrectly:

  Wrong (v2.3.3 first try): filter the corpus to exclude short-career
  players so they can't be comps at all.

  Correct (v2.3.3-final): KEEP short-career busts (Tim Tebow, Aaron
  Brooks, Desmond Ridder, EJ Manuel) in the comp pool. When the target
  player is being compared to one of them, treat that as a STRONGER
  negative signal about the target. "If you are being compared to a
  player like Aaron Brooks or Desmond Ridder or Tim Tebow you should
  be heavily de-ranked for that comparison. You are being compared to
  players who stopped accumulating stats because teams stopped playing
  them."

Implementation: new TOP-5 BUST AMPLIFIER on the survival multiplier.
Each wash-out among the 5 highest-similarity comps applies an extra
8% multiplicative haircut on top of the rate-based formula.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def engine():
    from dynasty.engine.similarity_v1 import run_engine
    return run_engine(persist=False)


def _rank(engine, name):
    rows = sorted(engine.rankings, key=lambda r: -r["production_score"])
    for i, r in enumerate(rows, 1):
        if r["name"] == name:
            return i
    return None


def _row(engine, name):
    return next((r for r in engine.rankings if r["name"] == name), None)


# ---------------------------------------------------------------------------
# Part 1: short-career busts must still be in the corpus
# ---------------------------------------------------------------------------

def test_short_career_busts_remain_in_corpus(engine):
    """The v2.3.3-first-try filter would have removed these names.
    v2.3.3-final keeps them so they can act as negative signals on
    targets like Anthony Richardson.
    """
    names_in_corpus = {c.name for c in engine.long_arc_corpus}
    for name in (
        "Tim Tebow", "Christian Ponder", "Tyler Thigpen", "EJ Manuel",
        "Aaron Brooks", "Josh Freeman", "Mark Sanchez", "Blake Bortles",
        "Vince Young",
    ):
        assert name in names_in_corpus, (
            f"{name} must remain in long-arc corpus "
            f"(v2.3.3-final keeps short-career busts as negative signal)"
        )


# ---------------------------------------------------------------------------
# Part 2: top_5_bust_count drives the amplifier
# ---------------------------------------------------------------------------

def test_richardson_has_top5_busts(engine):
    """Anthony Richardson's pre-filter comp pool surfaced Josh Freeman,
    Tim Tebow, EJ Manuel in his top 5 most-similar comps. Each is a
    wash-out (career ended by age 30 with < 8 NFL seasons). The
    top-5 bust amplifier should now apply meaningful extra haircut
    on his survival multiplier on top of the rate-based formula.
    """
    row = _row(engine, "Anthony Richardson")
    if row is None:
        pytest.skip("Richardson not in current rankings")
    assert row["top5_bust_count"] >= 2, (
        f"Richardson top-5 bust count {row['top5_bust_count']} - "
        f"expected >= 2 short-career-bust comps in top 5"
    )
    assert row["survival_multiplier"] <= 0.75, (
        f"Richardson survival {row['survival_multiplier']} - "
        f"expected <= 0.75 with {row['top5_bust_count']} top-5 busts"
    )


def test_clean_comp_pools_top5_busts_zero(engine):
    """Players whose top-5 comps are all long-tenure NFL careers must
    have top5_bust_count == 0 and survival near 1.0.
    """
    for name in (
        "Josh Allen", "Patrick Mahomes", "Lamar Jackson",
        "Ja'Marr Chase", "Justin Jefferson",
    ):
        row = _row(engine, name)
        if row is None:
            continue
        assert row["top5_bust_count"] == 0, (
            f"{name} top5_bust_count {row['top5_bust_count']} - "
            f"clean comp pool should be 0"
        )
        assert row["survival_multiplier"] >= 0.93, (
            f"{name} survival {row['survival_multiplier']} - "
            f"clean comp pool should keep survival near 1.0"
        )


# ---------------------------------------------------------------------------
# Part 3: Phil's anchor cases must drop into the right tier
# ---------------------------------------------------------------------------

def test_anthony_richardson_heavily_deranked(engine):
    """Phil 2026-05-22: Richardson IS being compared to Tebow/Manuel/
    Freeman/Bortles. The model must heavily de-rank him for it.
    Pin: outside top 100.
    """
    rank = _rank(engine, "Anthony Richardson")
    if rank is None:
        pytest.skip("Richardson not in rankings")
    assert rank > 100, (
        f"Anthony Richardson at #{rank} - the model must heavily "
        f"de-rank a player whose top-5 comps include 3+ wash-outs"
    )


def test_sam_howell_outside_top_75(engine):
    """Sam Howell still drops deep because he combines stale-data
    (0 games in 2024-25) with a comp pool that doesn't justify a
    top-50 ranking.
    """
    rank = _rank(engine, "Sam Howell")
    if rank is None:
        pytest.skip("Howell not in rankings")
    assert rank > 75, (
        f"Sam Howell at #{rank} - benched journeyman should be deep"
    )


# ---------------------------------------------------------------------------
# Part 4: active rookie starters with clean top-5 keep their value
# ---------------------------------------------------------------------------

def test_jaxson_dart_inside_top_75(engine):
    """Jaxson Dart's top-5 comps (Kyler Murray, Dak Prescott, Joe
    Burrow, Daniel Jones, Josh Allen) are all settled NFL careers,
    not wash-outs. The amplifier should NOT fire and Dart should
    land in the top tier of rookies.

    v3.1 update (2026-05-24): threshold relaxed from top-50 to top-75
    because the proven-production floor lifted ~15–20 banked vets
    into the top 50, displacing rookies. Dart has zero banked
    production so the floor doesn't help him. The original invariant
    (no wash-out amplifier firing) still holds — his score is
    unchanged; only his absolute rank shifted.
    """
    rank = _rank(engine, "Jaxson Dart")
    if rank is None:
        pytest.skip("Dart not in rankings")
    assert rank <= 75, (
        f"Dart rank #{rank} - top 5 comps are not wash-outs (v3.1)"
    )


def test_amplifier_constant_value():
    """Pin the per-bust amplifier constant in source so it doesn't
    drift silently.
    """
    from dynasty.engine import v2_2_penalties
    import inspect
    src = inspect.getsource(v2_2_penalties.compute_survival)
    assert "0.08 * min(top5_bust_count" in src, (
        "top-5 bust amplifier constant should be 0.08 per bust; "
        "do not change this without updating the test."
    )
