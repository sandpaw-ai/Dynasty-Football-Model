"""v3.2 — survivorship-bias corpus expansion regression tests.

Phil's 2026-05 flag: the v1.1+ ``is_long_arc()`` gate dropped ~18% of
skill-position careers (715 players) from the comp pool — specifically
post-2022 short-career arcs (Heinicke, Bridgewater, Trevor Siemian,
Driskel, etc., and the WR/RB/TE analogues). This systematically inflated
young-player projections because the "didn't pan out" outcomes were
never represented in any comp distribution.

v3.2 fix:
  1. Build a SECOND broader arc set (``comp_pool_arcs``) including
     every skill-position career with ≥2 completed seasons. The
     original ``long_arc_arcs`` stays for the percentile table and
     career-length-era multipliers (they need high-info anchors).
  2. ``find_comps`` applies a career-stage gate: a comp candidate
     must have ≥ max(target_n_seasons, COMP_POOL_MIN_SEASONS=3)
     completed seasons.

These tests pin the expansion + the gate behaviour.
"""
import pytest

from dynasty.engine.similarity_v1 import run_engine
from dynasty.engine.fantasy_arc_similarity import COMP_POOL_MIN_SEASONS


@pytest.fixture(scope="module")
def engine():
    return run_engine()


def test_comp_pool_strictly_broader_than_long_arc(engine):
    """comp_pool_arcs must contain every long_arc_arcs entry plus more."""
    long_arc_ids = {a.player_id for a in engine.long_arc_arcs}
    comp_pool_ids = {a.player_id for a in engine.comp_pool_arcs}
    assert long_arc_ids <= comp_pool_ids, "long_arc set should subset comp pool"
    delta = len(comp_pool_ids) - len(long_arc_ids)
    assert delta >= 400, (
        f"v3.2 expects ~500+ short-career arcs added to the comp pool; got {delta}"
    )


def test_excluded_short_career_qbs_now_in_comp_pool(engine):
    """The specific QB busts/journeymen Phil flagged in the 2026-05
    diagnosis must now be in ``comp_pool_arcs``. Pre-v3.2 these were
    silently dropped."""
    names = {a.name for a in engine.comp_pool_arcs}
    for n in (
        "Taylor Heinicke",
        "Teddy Bridgewater",
        "Trevor Siemian",
        "Brandon Allen",
        "Jeff Driskel",
        "Matt Barkley",
    ):
        assert n in names, f"{n} should be in v3.2 comp_pool_arcs"


def test_excluded_short_career_skill_pos_now_in_comp_pool(engine):
    """Same check for the RB/WR/TE analogues."""
    names = {a.name for a in engine.comp_pool_arcs}
    for n in (
        "Damien Williams",     # RB
        "Willie Snead",        # WR
        "Martavis Bryant",     # WR
        "Logan Thomas",        # TE
        "C.J. Uzomah",         # TE
    ):
        assert n in names, f"{n} should be in v3.2 comp_pool_arcs"


def test_career_stage_gate_holds_for_every_comp(engine):
    """For every (target, comp) pair the engine outputs from the v2.0
    cumulative-arc engine, comp_n_seasons must satisfy the career-stage
    gate."""
    careers = engine.careers
    rookie_pids = {
        row["player_id"] for row in engine.rankings
        if row.get("engine") == "rookie_nfl_fp_arc"
    }
    violations = []
    for pid, comp_list in engine.comps.items():
        if pid in rookie_pids:
            continue
        target = careers.get(pid)
        if target is None:
            continue
        target_n = len(target.seasons)
        min_comp_n = max(target_n, COMP_POOL_MIN_SEASONS)
        for c in comp_list:
            comp_n = c.get("seasons_played")
            if comp_n is None:
                continue
            if comp_n < min_comp_n:
                violations.append(
                    (target.name, c["name"], comp_n, min_comp_n)
                )
    assert not violations, (
        f"{len(violations)} career-stage violations: {violations[:5]}"
    )


def test_stroud_comps_include_short_career_busts(engine):
    """C.J. Stroud (3rd-year QB) — top-25 comps must include a meaningful
    share of short-career flame-outs. Pre-v3.2 his comps were 100%
    long-arc-qualified (Mariota, Roethlisberger, Bledsoe, Carr, ...).
    Post-v3.2 the previously-excluded 4-7-season "didn't pan out" arcs
    are eligible. We require ≥3 of his top-25 comps have ≤5 seasons."""
    stroud = next((r for r in engine.rankings if r["name"] == "C.J. Stroud"), None)
    assert stroud is not None, "Stroud must be in rankings"
    comps = engine.comps.get(stroud["player_id"], [])[:25]
    assert len(comps) >= 10, f"Stroud should have plenty of comps, got {len(comps)}"
    short = [c for c in comps if c.get("seasons_played", 99) <= 5]
    assert len(short) >= 3, (
        f"Stroud expects ≥3 short-career anchor comps in top-25; got {len(short)}: "
        f"{[(c['name'], c.get('seasons_played')) for c in comps]}"
    )


def test_veteran_pool_approximately_unchanged_for_dak(engine):
    """Dak (10 NFL seasons): the career-stage gate is ≥10, which is
    nearly identical to the pre-v3.2 LONG_ARC_MIN_SEASONS=8 cutoff —
    his comp pool should be approximately unchanged.

    We can't measure pool-size directly per-target (find_comps returns
    top-K only), but we CAN check that his comps are all long-arc
    qualified (since min_comp_n=10 > LONG_ARC_MIN_SEASONS=8).
    """
    dak = next((r for r in engine.rankings if r["name"] == "Dak Prescott"), None)
    assert dak is not None
    comps = engine.comps.get(dak["player_id"], [])
    for c in comps:
        n = c.get("seasons_played", 0)
        assert n >= 10, (
            f"Dak comp {c['name']} has {n} seasons, expected ≥10 "
            "(career-stage-matched veteran pool)"
        )


def test_top1_anchors_preserved(engine):
    """Allen/Lamar must stay top-2; Mahomes must stay top-5; Dak top-10.

    These are the proven-production anchors from v3.1. The v3.2 fix
    should NOT shake them because their comp pool was already
    long-arc-only.
    """
    ranks = {r["name"]: i + 1 for i, r in enumerate(engine.rankings)}
    assert ranks.get("Josh Allen") <= 2, f"Allen rank={ranks.get('Josh Allen')}"
    assert ranks.get("Lamar Jackson") <= 2, f"Lamar rank={ranks.get('Lamar Jackson')}"
    assert ranks.get("Patrick Mahomes") <= 5, f"Mahomes rank={ranks.get('Patrick Mahomes')}"
    assert ranks.get("Dak Prescott") <= 10, f"Dak rank={ranks.get('Dak Prescott')}"


def test_comp_pool_min_seasons_floor():
    """The 3-season floor prevents 1-2-season noise comps even for
    very-early-career targets."""
    assert COMP_POOL_MIN_SEASONS == 3, (
        "Floor should be 3 — 1-2 season comps are too noisy to project from"
    )
