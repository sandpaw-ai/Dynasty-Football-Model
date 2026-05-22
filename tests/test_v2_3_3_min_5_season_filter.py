"""Tests for the v2.3.3 changes (Phil 2026-05-22).

Three issues:
  1. Sam Howell, Anthony Richardson, Justin Fields ranked too high
     despite comp pools full of wash-outs.
  2. Cam Ward and Jaxson Dart ranked too low because their pools
     were polluted with unproven 2-3 year active QBs.
  3. Phil's structural directive: any player without 5 years of NFL
     experience should not be a comp.

Fix layers in v2.3.3:
  - Hard >=5-NFL-season filter at corpus construction for both engines.
  - Stronger survival_multiplier formula now that the corpus is clean.
  - Stale-data flag (< 12 games over last 2 seasons) disables the
    Bayesian pull-toward-baseline for journeymen / extended-injury QBs.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def engine():
    from dynasty.engine.similarity_v1 import run_engine
    return run_engine(persist=False)


def _rank(engine, name: str):
    rows = sorted(engine.rankings, key=lambda r: -r["production_score"])
    for i, r in enumerate(rows, 1):
        if r["name"] == name:
            return i
    return None


def _row(engine, name: str):
    return next((r for r in engine.rankings if r["name"] == name), None)


# ---------------------------------------------------------------------------
# Part 1: >=5-NFL-season comp filter applied at the corpus level
# ---------------------------------------------------------------------------

def test_long_arc_corpus_excludes_short_career_players(engine):
    """Tim Tebow (3 seasons), Christian Ponder (3), Tyler Thigpen (2),
    EJ Manuel (4), Brock Osweiler (4) used to be in the v2.0 comp pool
    because they're 'retired' (long-arc includes retired criterion).
    The v2.3.3 hard >=5-season floor removes them.
    """
    from dynasty.engine.similarity_v1 import MIN_GAMES_PER_SEASON
    names_in_corpus = {c.name for c in engine.long_arc_corpus}
    # Anchor names from Phil's bug report and the pre-fix Richardson /
    # Fields / Howell comp pools. Each has < 5 completed seasons in
    # the nflverse corpus.
    for short_career in (
        "Tim Tebow", "Christian Ponder", "Tyler Thigpen",
        "EJ Manuel",
    ):
        assert short_career not in names_in_corpus, (
            f"{short_career} should be excluded from long-arc corpus "
            f"(<5 NFL seasons)"
        )
    # Sanity check: every player in the corpus has >=5 completed seasons.
    for c in engine.long_arc_corpus:
        n_completed = sum(
            1 for s in c.seasons if s.games >= MIN_GAMES_PER_SEASON
        )
        assert n_completed >= 5, (
            f"{c.name} in corpus with only {n_completed} completed seasons"
        )


def test_richardson_comp_pool_excludes_unproven_actives(engine):
    """Anthony Richardson's comp pool used to include Tebow (3), EJ
    Manuel (4), Christian Ponder (3). Post-filter every comp must have
    >=5 NFL seasons.
    """
    richardson = _row(engine, "Anthony Richardson")
    if richardson is None:
        pytest.skip("Anthony Richardson not in current rankings")
    comps = engine.comps.get(richardson["player_id"], [])
    assert comps, "Richardson should still have a comp pool"
    for c in comps:
        seasons = c.get("seasons_played")
        assert seasons is None or seasons >= 5, (
            f"Richardson comp {c['name']} has {seasons} seasons "
            f"(should be filtered by v2.3.3 >=5 floor)"
        )


def test_dart_comp_pool_excludes_unproven_actives(engine):
    """Jaxson Dart's comp pool used to include Anthony Richardson
    (2), Bo Nix (2), CJ Stroud (3), Caleb Williams (2). Per Phil's
    directive these unsettled active QBs should NOT be comps.
    """
    dart = _row(engine, "Jaxson Dart")
    if dart is None:
        pytest.skip("Jaxson Dart not in current rankings")
    comps = engine.comps.get(dart["player_id"], [])
    assert comps, "Dart should still have a comp pool"
    for c in comps:
        seasons = c.get("seasons_played")
        assert seasons is None or seasons >= 5, (
            f"Dart comp {c['name']} has {seasons} seasons "
            f"(should be filtered by v2.3.3 >=5 floor)"
        )
    comp_names = {c["name"] for c in comps}
    for unproven in (
        "Anthony Richardson", "Bo Nix", "C.J. Stroud", "Caleb Williams",
    ):
        assert unproven not in comp_names, (
            f"{unproven} (<5 NFL seasons) should not appear in Dart comps"
        )


# ---------------------------------------------------------------------------
# Part 2: Phil's overrated players move down significantly
# ---------------------------------------------------------------------------

def test_sam_howell_drops_below_top_50(engine):
    """Phil 2026-05-22: 'Sam Howell ranking is way too high. Look at
    all of the washed out comparisons.'

    Howell played 1 NFL season in 2023 (17 G) and 0 since. Stale-data
    flag disables the Bayesian pull-toward-baseline so his projection
    multiplies straight by confidence=0.531 instead of being lifted
    toward the QB top-50 median.
    """
    rank = _rank(engine, "Sam Howell")
    if rank is None:
        pytest.skip("Howell not in current rankings")
    assert rank > 50, (
        f"Sam Howell at #{rank} - with 0 recent games and a comp pool "
        f"full of wash-outs he should be deep, not in the top 50"
    )
    row = _row(engine, "Sam Howell")
    assert row["is_stale_data"] is True, (
        "Howell should be flagged stale (0 games in 2024-25)"
    )


def test_anthony_richardson_drops_below_top_50(engine):
    """Phil 2026-05-22: Anthony Richardson is way too high. He played
    11 games in 2024 and 0 meaningful starts in 2025 - stale data.
    """
    rank = _rank(engine, "Anthony Richardson")
    if rank is None:
        pytest.skip("Richardson not in current rankings")
    assert rank > 50, (
        f"Anthony Richardson at #{rank} - should be deep after the "
        f"survival + stale-data adjustments"
    )
    row = _row(engine, "Anthony Richardson")
    assert row["is_stale_data"] is True, (
        "Richardson should be flagged stale (11 games over 2024-25)"
    )


def test_cam_ward_active_not_stale(engine):
    """Cam Ward played a full rookie year (17 games in 2025) - NOT
    stale even though it's his only NFL year. Stale fires only on
    players with < RECENT_STARTER_GAMES_TWO_YEAR=12 games over the
    two most recent seasons.
    """
    row = _row(engine, "Cam Ward")
    if row is None:
        pytest.skip("Cam Ward not in current rankings")
    assert row["is_stale_data"] is False, (
        f"Cam Ward (recent_games={row.get('recent_games_two_year')}) "
        f"should NOT be flagged stale"
    )


def test_dart_active_not_stale(engine):
    """Jaxson Dart played 14 games as a rookie - above the 12-game
    threshold, so NOT stale. He should keep his Bayesian pull-toward-
    baseline.
    """
    row = _row(engine, "Jaxson Dart")
    if row is None:
        pytest.skip("Dart not in current rankings")
    assert row["is_stale_data"] is False
    assert row.get("recent_games_two_year", 0) >= 12


# ---------------------------------------------------------------------------
# Part 3: stale-data flag preserves legit current starters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,reason", [
    ("Josh Allen", "8 seasons of full starter snaps"),
    ("Patrick Mahomes", "perennial starter"),
    ("Jalen Hurts", "perennial starter"),
    ("Lamar Jackson", "perennial starter"),
    ("Justin Jefferson", "5+ seasons of full starter snaps"),
    ("Ja'Marr Chase", "5+ seasons of full starter snaps"),
    ("Bo Nix", "2 full NFL seasons - active 2nd-year starter"),
    ("Jayden Daniels", "rookie + injured 2025 but 24 games over 2 years"),
])
def test_active_starters_not_flagged_stale(engine, name, reason):
    """The stale-data flag must NOT fire on active starting QBs/WRs.
    Anchor a representative set across tenure profiles.
    """
    row = _row(engine, name)
    if row is None:
        pytest.skip(f"{name} not in rankings")
    assert row["is_stale_data"] is False, (
        f"{name} flagged stale despite being an active starter "
        f"({reason}); recent_games={row.get('recent_games_two_year')}"
    )


# ---------------------------------------------------------------------------
# Part 4: corpus depth still supports comp selection
# ---------------------------------------------------------------------------

def test_corpus_per_position_minimum_depth(engine):
    """After the >=5-season filter the corpus is smaller but must still
    support top-K=20 comp selection per position.
    """
    from collections import Counter
    by_pos = Counter(c.position for c in engine.long_arc_corpus)
    assert by_pos["QB"] >= 40, f"QB corpus only {by_pos['QB']}"
    assert by_pos["RB"] >= 80, f"RB corpus only {by_pos['RB']}"
    assert by_pos["WR"] >= 120, f"WR corpus only {by_pos['WR']}"
    assert by_pos["TE"] >= 70, f"TE corpus only {by_pos['TE']}"
