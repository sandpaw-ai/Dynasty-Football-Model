"""v2.4 PR 3 — penalty retune + pre-1999 haircut tests.

Validates:
  1. ``is_pre1999_comp`` returns True for a comp whose snapshot season < 1999.
  2. ``is_pre1999_comp`` returns False for a CROSSOVER player used at a
     post-1999 snapshot age — the haircut is about pre-1999 data quality,
     not the player.
  3. The 0.9× haircut is baked into ``CompMatch.similarity`` inside
     ``find_comps``.
  4. Derrick Henry's flag-ON top-25 comp pool includes at least one of
     {Walter Payton, Emmitt Smith, Marcus Allen, Eric Dickerson, Tony
     Dorsett, John Riggins, Earl Campbell}. HARD ASSERTION — the whole
     point of v2.4 is to surface these.
  5. Per-position bust-rate baselines are exposed as constants.

Run with ``pytest tests/test_v2_4_penalty_retune.py -v``.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from dynasty.engine.fantasy_arc_similarity import (
    CompMatch,
    PRE1999_COMP_WEIGHT_HAIRCUT,
    PRE1999_SNAPSHOT_CUTOFF,
    build_career_stage_percentile_table,
    find_comps,
    is_pre1999_comp,
    pre1999_haircut_weight,
    snapshot_season_for_comp,
    BASE_FORMAT,
)
from dynasty.engine.similarity_v1 import (
    RETIRED_THROUGH_SEASON,
    _build_arcs,
    _career_to_arc_seasons,
    build_era_pace_table,
    load_corpus,
)
from dynasty.engine.fantasy_arc import build_career_arc, SUPPORTED_FORMATS
from dynasty.engine.v2_2_penalties import (
    BUST_RATE_BASELINE,
    BUST_RATE_BASELINE_V231,
    DURABLE_CAREER_BASELINE,
)


# Hard assertion: pre-1999 RB legends that Henry MUST be comped against.
HENRY_REQUIRED_COMPS = {
    "Walter Payton",
    "Emmitt Smith",
    "Marcus Allen",
    "Eric Dickerson",
    "Tony Dorsett",
    "John Riggins",
    "Earl Campbell",
}


# ---------------------------------------------------------------------------
# 1. Pre-1999 comp helpers
# ---------------------------------------------------------------------------

def test_pre1999_haircut_constant():
    assert PRE1999_COMP_WEIGHT_HAIRCUT == 0.9
    assert PRE1999_SNAPSHOT_CUTOFF == 1999


def test_pre1999_haircut_weight_returns_09_for_pre1999():
    # Build a minimal fake CareerArc with one pre-1999 season.
    from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint

    arc = CareerArc(
        player_id="pfr_PaytWa00", name="Walter Payton", position="RB",
        last_season=1987, rookie_season=1975, retired=True, is_long_arc=True,
        career_arc=[
            SeasonArcPoint(season=1985, age=31, games=16, era=1),
        ],
    )
    assert is_pre1999_comp(arc, snapshot_age=31) is True
    assert pre1999_haircut_weight(arc, snapshot_age=31) == pytest.approx(0.9)


def test_pre1999_haircut_weight_returns_1_for_post1999():
    """A crossover player used at a 1999+ snapshot age does NOT take the
    haircut — the haircut is about data quality uncertainty, not the player.
    """
    from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint

    arc = CareerArc(
        player_id="00-0015165", name="Emmitt Smith", position="RB",
        last_season=2004, rookie_season=1990, retired=True, is_long_arc=True,
        career_arc=[
            SeasonArcPoint(season=1995, age=26, games=16, era=1),
            SeasonArcPoint(season=2000, age=31, games=16, era=1),
        ],
    )
    # Snapshot age 26 → 1995 season → pre-1999 → haircut
    assert is_pre1999_comp(arc, snapshot_age=26) is True
    assert pre1999_haircut_weight(arc, snapshot_age=26) == pytest.approx(0.9)
    # Snapshot age 31 → 2000 season → post-1999 → NO haircut
    assert is_pre1999_comp(arc, snapshot_age=31) is False
    assert pre1999_haircut_weight(arc, snapshot_age=31) == pytest.approx(1.0)


def test_snapshot_season_for_comp_resolves_exact_age_match():
    from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint

    arc = CareerArc(
        player_id="00-0015165", name="Emmitt Smith", position="RB",
        last_season=2004, rookie_season=1990, retired=True, is_long_arc=True,
        career_arc=[
            SeasonArcPoint(season=1995, age=26, games=16, era=1),
            SeasonArcPoint(season=2000, age=31, games=16, era=1),
        ],
    )
    assert snapshot_season_for_comp(arc, 26) == 1995
    assert snapshot_season_for_comp(arc, 31) == 2000


def test_snapshot_season_for_comp_falls_back_to_closest():
    """When the snapshot age isn't exactly in the arc (post AGE_WINDOW
    widening), return the closest qualifying season."""
    from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint

    arc = CareerArc(
        player_id="pfr_test", name="Test Player", position="RB",
        last_season=1990, rookie_season=1988, retired=True, is_long_arc=True,
        career_arc=[
            SeasonArcPoint(season=1988, age=22, games=16, era=1),
            SeasonArcPoint(season=1989, age=23, games=16, era=1),
        ],
    )
    # Age 24 isn't in the arc; the closest qualifying season is 1989 (age 23).
    assert snapshot_season_for_comp(arc, 24) == 1989


# ---------------------------------------------------------------------------
# 2. find_comps applies the haircut in CompMatch.similarity
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def henry_comps_flag_on():
    """Build the engine state with USE_PRE1999_CORPUS=True and return
    Derrick Henry's top-25 comp list (CompMatch objects).
    """
    careers = load_corpus(use_pre1999=True)
    pace = build_era_pace_table(careers, use_pre1999=True)

    long_arc_corpus = []
    for c in careers.values():
        if len(c.seasons) < 2:
            continue
        if not c.is_long_arc(through=RETIRED_THROUGH_SEASON):
            continue
        if c.is_retired(through=RETIRED_THROUGH_SEASON):
            long_arc_corpus.append(c)
        else:
            trimmed = c.with_completed_seasons_only(2025)
            if len(trimmed.seasons) >= 2:
                long_arc_corpus.append(trimmed)

    arcs = _build_arcs(careers.values(), pace)
    long_arc_arcs = []
    for c in long_arc_corpus:
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        arc = build_career_arc(
            player_id=c.player_id, name=c.name, position=c.position,
            last_season=c.last_season, rookie_season=c.rookie_season,
            retired=c.is_retired(through=RETIRED_THROUGH_SEASON),
            is_long_arc=True, seasons=seasons, pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        long_arc_arcs.append(arc)

    percentile_table = build_career_stage_percentile_table(
        long_arc_arcs, league_format=BASE_FORMAT,
    )

    henry = next(c for c in careers.values()
                 if c.name == "Derrick Henry" and c.position == "RB")
    target_arc = arcs[henry.player_id]
    age_now = henry.seasons[-1].age
    comps = find_comps(
        target=target_arc, long_arc_corpus=long_arc_arcs,
        target_age=age_now, league_format=BASE_FORMAT,
        percentile_table=percentile_table, k=25,
    )
    return comps


def test_compmatch_carries_haircut_diagnostics(henry_comps_flag_on):
    """Every CompMatch returned by find_comps must carry the
    raw_similarity and pre1999_haircut_applied diagnostic fields.
    """
    assert henry_comps_flag_on, "Henry should have comps with the flag on"
    for c in henry_comps_flag_on:
        assert hasattr(c, "raw_similarity")
        assert hasattr(c, "pre1999_haircut_applied")
        if c.pre1999_haircut_applied:
            assert c.similarity == pytest.approx(c.raw_similarity * 0.9, rel=1e-6)
        else:
            assert c.similarity == pytest.approx(c.raw_similarity, rel=1e-6)


def test_pre1999_comps_take_haircut(henry_comps_flag_on):
    """Henry's comp pool MUST contain at least one pre-1999-snapshot comp
    AND that comp's similarity MUST be 0.9× its raw_similarity.
    """
    pre1999 = [c for c in henry_comps_flag_on if c.pre1999_haircut_applied]
    assert pre1999, "Henry's flag-on comp pool should contain pre-1999 comps"
    for c in pre1999:
        assert c.similarity == pytest.approx(c.raw_similarity * 0.9, rel=1e-6), (
            f"{c.arc.name} (snapshot age {c.snapshot_age}) carries "
            f"similarity={c.similarity}, raw={c.raw_similarity} — "
            f"expected sim = 0.9 × raw"
        )


def test_crossover_post1999_comps_take_no_haircut(henry_comps_flag_on):
    """If Emmitt Smith appears in Henry's comp pool with a snapshot age
    that lands in his 1999+ seasons (age 30+ for Emmitt), he MUST NOT
    take the haircut — that would penalize a 2000-season comp for
    something that isn't a data quality issue.
    """
    emmitt = [c for c in henry_comps_flag_on if c.arc.name == "Emmitt Smith"]
    if not emmitt:
        pytest.skip("Emmitt Smith not in Henry's comp pool — see "
                    "docs/V2.4-VALIDATION.md")
    for c in emmitt:
        # Emmitt's 1999+ ages are 30-35. If the snapshot age is in that
        # range, the haircut MUST NOT have been applied.
        if c.snapshot_age >= 30:
            assert c.pre1999_haircut_applied is False, (
                f"Emmitt-at-{c.snapshot_age} ({c.arc.name}) should not take "
                "the haircut — snapshot lands in 1999+ years"
            )
            assert c.similarity == pytest.approx(c.raw_similarity, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. Henry MUST get Payton / Smith / Allen / Dickerson / Dorsett / Riggins / Campbell
# ---------------------------------------------------------------------------

def test_henry_gets_pre1999_legend_comp(henry_comps_flag_on):
    """HARD ASSERTION (this is the whole point of v2.4): with
    USE_PRE1999_CORPUS=True, Derrick Henry's top-25 comp pool MUST contain
    at least one of the workhorse-RB tail (Payton, Smith, Allen, Dickerson,
    Dorsett, Riggins, Campbell).
    """
    names = {c.arc.name for c in henry_comps_flag_on}
    hits = names & HENRY_REQUIRED_COMPS
    assert hits, (
        f"Henry's flag-on top-25 contains NONE of {HENRY_REQUIRED_COMPS}. "
        f"Got: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# 4. Bust-rate baseline constants
# ---------------------------------------------------------------------------

def test_bust_rate_baseline_constants_exist():
    """v2.4 exposes per-position bust-rate baselines for diagnostics."""
    assert set(BUST_RATE_BASELINE.keys()) >= {"QB", "RB", "WR", "TE"}
    for pos in ("QB", "RB", "WR", "TE"):
        assert 0.0 < BUST_RATE_BASELINE[pos] < 1.0


def test_bust_rate_baseline_qb_moved_down_from_v231():
    """The QB bust rate is the biggest mover: pre-1999 added a generation
    of long-career pocket passers (Marino, Montana, Kelly, Moon, Elway)
    who didn't bust by age 30, so the unified-corpus QB baseline is lower
    than the 1999+-only baseline.
    """
    assert BUST_RATE_BASELINE["QB"] < BUST_RATE_BASELINE_V231["QB"]


def test_durable_career_baseline_constants_exist():
    assert set(DURABLE_CAREER_BASELINE.keys()) >= {"QB", "RB", "WR", "TE"}
    for pos in ("QB", "RB", "WR", "TE"):
        assert 0.0 < DURABLE_CAREER_BASELINE[pos] < 1.0
