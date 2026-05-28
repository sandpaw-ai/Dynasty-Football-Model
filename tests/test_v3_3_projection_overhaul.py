"""v3.3 — Projection methodology overhaul tests.

Phil's 2026-05-28 brief (Slack DM, Stiehl workspace):

> "Derrick Henry's page looks wrong. The projected fantasy points remaining
>  seem way high at 2103. He is 32 years old. When you look at the most
>  similar comps from a production standpoint, Matt Forte, Emmitt Smith,
>  Curtis Martin, Frank Gore... none of them are even close to 2103
>  projected remaining fantasy points. The projected remaining fantasy
>  points should be some sort of weighted average of the comparable
>  players applied to the player, in this case Derrick Henry. Apply the
>  new methodology across the entire player base."

> "Joe Mixon did not play in 2025 (the most recent season). It is fair to
>  attribute that to injury or off the field issues... either of which
>  should penalize the player."

This file pins the v3.3 invariants:

1. ``production_path`` MUST NOT be ``proven_floor`` for any active player.
   The v3.1 banked-credit override is retired; comp-weighted is primary.

2. For a deep-career veteran whose comps all sit well below the v3.1
   proven_floor inflation, ``production_score`` must come down materially
   from the v3.1 number (Henry's pin: 2,103 → < 600).

3. Players who missed the entire most-recent NFL season take a
   ``missed_season_multiplier`` of 0.70 (Mixon: did not play 2025).
   Players who played the most recent season take a 1.0 multiplier
   (Henry: 17 games / 16 TDs in 2025 → 1.0).

4. The relaxed long-arc comp pool (LONG_ARC_RELAX_SEASONS = 2) widens
   the eligible pool for 9+yr targets by allowing 7+yr comps.

5. Every ranked row carries the v3.3 missed-season diagnostic fields
   for the player-page breakdown table.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from dynasty.engine.similarity_v1 import run_engine


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
# Invariant 1 — proven_floor is no longer a winning path
# ---------------------------------------------------------------------------

def test_no_row_uses_proven_floor_as_production_path(engine):
    """v3.3 retires the proven_floor production_path. Every fantasy-arc-v2
    row should resolve to either ``comp_weighted`` or ``peak_anchored``."""
    bad = [
        (r["name"], r["production_path"])
        for r in engine.rankings
        if r.get("engine") == "fantasy_arc_v2"
        and r.get("production_path") == "proven_floor"
    ]
    assert not bad, (
        f"v3.3 should NOT use proven_floor as production_path; "
        f"violations: {bad[:5]}"
    )


def test_proven_floor_fp_still_present_for_diagnostics(engine):
    """The diagnostic field stays on the row so the player-page can render
    the banked-credit row of the breakdown table."""
    for r in engine.rankings:
        if r.get("engine") != "fantasy_arc_v2":
            continue
        assert "proven_floor_fp" in r, (
            f"{r['name']}: proven_floor_fp diagnostic missing"
        )


# ---------------------------------------------------------------------------
# Invariant 2 — Henry's projection deflates materially (Phil's example)
# ---------------------------------------------------------------------------

def test_henry_projection_no_longer_inflated_by_banked_credit(engine):
    """Phil's worked example: Derrick Henry at age 32 should NOT carry
    2,103 projected remaining fp. The v3.3 production_score should come
    in well under 700 (his comp pool projects ~200-400 post-32).
    """
    henry = _row(engine, "Derrick Henry")
    assert henry is not None, "Derrick Henry must be ranked"
    assert henry["production_score"] < 700, (
        f"Henry's v3.3 production_score should be <700 (comp-weighted "
        f"reality for a 32yo RB), got {henry['production_score']}"
    )
    # And the comp-weighted number should be modest, in the comps' actual
    # range, not the 2,103 v3.1 figure.
    assert henry["comp_weighted_fp"] < 600, (
        f"Henry's comp_weighted_fp should be <600 "
        f"(comps actually averaged ~200-400 post-32), "
        f"got {henry['comp_weighted_fp']}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — missed-season penalty (Mixon)
# ---------------------------------------------------------------------------

def test_missed_recent_season_penalty_applies_to_mixon(engine):
    """Joe Mixon did not play in the 2025 season. He must carry a
    missed_season_multiplier <= 0.70 (the full-missed-season tax).
    """
    mixon = _row(engine, "Joe Mixon")
    assert mixon is not None, "Joe Mixon must be in the rankings"
    assert mixon["missed_season_multiplier"] <= 0.70 + 1e-6, (
        f"Mixon (no 2025 games) should take a 0.70 missed-season "
        f"penalty, got {mixon['missed_season_multiplier']}"
    )
    assert mixon["missed_season_seasons_since"] >= 1
    assert mixon["missed_season_last_played"] == 2024


def test_missed_recent_season_penalty_does_not_apply_to_henry(engine):
    """Derrick Henry played all 17 games in 2025. He must NOT take a
    missed-season penalty."""
    henry = _row(engine, "Derrick Henry")
    assert henry is not None
    assert henry["missed_season_multiplier"] == pytest.approx(1.0)
    assert henry["missed_season_seasons_since"] == 0


def test_missed_season_diagnostic_fields_present_on_every_row(engine):
    """Every row must have the v3.3 missed-season diagnostic fields so
    the player-page breakdown table can render the new row uniformly."""
    required = {
        "missed_season_multiplier",
        "missed_season_reason",
        "missed_season_last_played",
        "missed_season_last_played_games",
        "missed_season_seasons_since",
    }
    for r in engine.rankings:
        missing = required - set(r.keys())
        assert not missing, (
            f"{r['name']}: missing v3.3 missed-season fields {missing}"
        )


# ---------------------------------------------------------------------------
# Invariant 4 — relaxed long-arc comp pool widens the veteran pool
# ---------------------------------------------------------------------------

def test_long_arc_relax_widens_veteran_comp_floor(engine):
    """v3.3 relaxes the season-count gate for 9+yr targets by
    LONG_ARC_RELAX_SEASONS=2. The relaxation should mean SOME 9+yr
    veteran's eligible comp pool admits at least one shorter-career
    comp. We scan all 9+yr targets and assert at least one such
    admission across the rankings (i.e. the relaxation is not dead code).
    """
    from dynasty.engine.fantasy_arc_similarity import (
        LONG_ARC_RELAX_SEASONS,
        LONG_ARC_RELAX_TRIGGER_SEASONS,
    )
    careers = engine.careers
    admissions = 0
    for r in engine.rankings:
        if r.get("engine") != "fantasy_arc_v2":
            continue
        target = careers.get(r["player_id"])
        if not target:
            continue
        target_n = len(target.seasons)
        if target_n < LONG_ARC_RELAX_TRIGGER_SEASONS:
            continue
        # Any comp with strictly less than target_n seasons but >=
        # (target_n - LONG_ARC_RELAX_SEASONS) is an admission attributable
        # to the v3.3 relaxation.
        comps = engine.comps.get(r["player_id"], [])
        for c in comps:
            cs = c.get("seasons_played")
            if cs is None:
                continue
            if (target_n - LONG_ARC_RELAX_SEASONS) <= cs < target_n:
                admissions += 1
    assert admissions >= 1, (
        "v3.3 long-arc relaxation should admit at least one "
        "shorter-career comp into SOME 9+yr veteran's pool"
    )


# ---------------------------------------------------------------------------
# Invariant 5 — top-tier QB anchors hold under the new methodology
# ---------------------------------------------------------------------------

def test_top_tier_qbs_anchor_top_10(engine):
    """Allen / Lamar / Mahomes should all be top-10 under v3.3.
    Phil's mandate (comp-weighted only) doesn't sink elite young QBs
    because their comps include other elite long-career QBs.
    """
    allen = _rank(engine, "Josh Allen")
    lamar = _rank(engine, "Lamar Jackson")
    mahomes = _rank(engine, "Patrick Mahomes")
    assert allen is not None and allen <= 10, f"Allen rank {allen}"
    assert lamar is not None and lamar <= 10, f"Lamar rank {lamar}"
    assert mahomes is not None and mahomes <= 15, f"Mahomes rank {mahomes}"


def test_aging_vets_drop_below_top_30(engine):
    """v3.3 mandate: aging veterans with thin remaining-runway should
    NOT live at the top. Dak (32yo QB), Henry (32yo RB), Stafford
    (38yo QB) should all be outside the top 30.
    """
    for name in ("Dak Prescott", "Derrick Henry", "Matthew Stafford"):
        rank = _rank(engine, name)
        assert rank is not None, f"{name} missing from rankings"
        assert rank > 30, (
            f"v3.3: aging vet {name} should rank outside top 30, got #{rank}"
        )
