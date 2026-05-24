"""Tests for the v2.3.2 changes (Phil 2026-05-22).

Three issues:

  1. Comps that are STILL ACTIVE in the NFL must not be flagged as
     washed-out. Pre-fix the bust definition (final_age <= 30 AND
     seasons_played < 8) wrongly flagged active 1-3 year players
     like James Cook, Zach Charbonnet, and Roschon Johnson.

  2. The Dynasty Rankings chip function on the consensus page must
     render an explicit up/down arrow so the direction of the
     disagreement is unambiguous, not just a coloured number.

  3. The non-QB sample-confidence shrinkage was demolishing legit
     1-2 season skill players. Retune lifts Marvin Harrison Jr.
     and Rome Odunze out of the deep tail without touching QB
     calibration or 1-NFL-season rookies.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def engine_and_site(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("site")
    from dynasty.engine.similarity_v1 import run_engine
    from dynasty.report import generate_site
    engine = run_engine(persist=False)
    generate_site(
        output_dir=str(out_dir),
        league_format="sf_ppr",
        limit=300,
        engine=engine,
    )
    return engine, out_dir


def _rank(engine, name: str):
    rows = sorted(engine.rankings, key=lambda r: -r["production_score"])
    for i, r in enumerate(rows, 1):
        if r["name"] == name:
            return i
    return None


# ---------------------------------------------------------------------------
# Part 1: wash-out flag respects active-roster status
# ---------------------------------------------------------------------------

def test_active_short_career_comps_not_flagged_washed_out(engine_and_site):
    """Phil 2026-05-22: 'we need to write some code that does not call a
    player washed out if they are still actively in the NFL.'
    """
    engine, _ = engine_and_site
    # Anchor names from Phil's bug report. All have final_age <= 30 AND
    # seasons_played < 8 so they used to trip the wash-out flag.
    anchors = {
        "James Cook", "Zach Charbonnet", "Roschon Johnson", "Ray Davis",
        "Jaleel McLaughlin", "Luke Schoonmaker", "Cade Stover",
    }
    seen_active: set = set()
    for pid, comps in engine.comps.items():
        for c in comps:
            if c["name"] in anchors:
                last = c.get("last_season")
                assert last is not None and last >= 2024, (
                    f"{c['name']} should still be active (last_season >= 2024) "
                    f"but reported last_season={last}"
                )
                assert c.get("washed_out") is False, (
                    f"{c['name']} (last_season={last}, "
                    f"seasons={c.get('seasons_played')}) was wrongly "
                    f"flagged as washed_out"
                )
                seen_active.add(c["name"])
    if not seen_active:
        pytest.skip(
            "none of the anchor active short-career players appear "
            "in any comp set"
        )


def test_retired_bust_still_flagged_washed_out(engine_and_site):
    """Inverse invariant: a comp who genuinely washed out (Aaron Brooks,
    Mark Sanchez) must still get the badge so the Bo Nix -> Brooks
    framing fix from v2.3.1 holds.
    """
    engine, _ = engine_and_site
    for ap in engine.rankings:
        if ap["name"] == "Bo Nix":
            comps = engine.comps.get(ap["player_id"], [])
            brooks = next(
                (c for c in comps if c["name"] == "Aaron Brooks"), None,
            )
            assert brooks is not None
            assert brooks["washed_out"] is True
            assert (brooks.get("last_season") or 0) < 2024
            return
    pytest.skip("Bo Nix not in current rankings")


# ---------------------------------------------------------------------------
# Part 2: consensus page renders up/down arrows
# ---------------------------------------------------------------------------

def test_consensus_chip_renders_arrow_glyph(engine_and_site):
    """The chip JS must emit a literal up-arrow for model-bullish deltas
    and a literal down-arrow for crowd-bullish deltas. A coloured number
    alone was confusing per Phil's 2026-05-22 review.
    """
    _, out_dir = engine_and_site
    html = (Path(out_dir) / "league.html").read_text(encoding="utf-8")
    if "const CONSENSUS" not in html:
        pytest.skip("legacy overlay fallback rendered (no KTC snapshot)")
    start = html.find("const CONSENSUS")
    end = html.find("function setFormat", start)
    chip_block = html[start:end]
    assert "div-up-big" in chip_block and "\u2191 " in chip_block
    assert "div-down-big" in chip_block and "\u2193 " in chip_block


# ---------------------------------------------------------------------------
# Part 3: non-QB confidence retune
# ---------------------------------------------------------------------------

def test_mhj_no_longer_buried(engine_and_site):
    """Marvin Harrison Jr. was at rank #236 with production_score 364,
    dragged down almost entirely by sample-confidence shrinkage (0.531).
    After the v2.3.2 retune he should be in the top 200 with confidence
    near 1.0.
    """
    engine, _ = engine_and_site
    rank = _rank(engine, "Marvin Harrison Jr.")
    assert rank is not None
    assert rank <= 200, (
        f"MHJ rank #{rank} - retune should lift him above 200"
    )
    row = next(
        r for r in engine.rankings if r["name"] == "Marvin Harrison Jr."
    )
    assert row["sample_confidence"] >= 0.90, (
        f"MHJ sample_confidence={row['sample_confidence']} after retune; "
        f"should be near 1.0 with 29 games played and denom=30"
    )


def test_rome_odunze_also_lifted(engine_and_site):
    """Anchor a second 2024-class WR with similar games (29) to confirm
    the retune isn't an MHJ-specific patch.
    """
    engine, _ = engine_and_site
    rank = _rank(engine, "Rome Odunze")
    if rank is None:
        pytest.skip("Rome Odunze not in rankings")
    assert rank <= 200, (
        f"Odunze rank #{rank} - retune should lift him above 200"
    )


def test_qb_confidence_unchanged(engine_and_site):
    """The retune must NOT touch QB calibration (Phil approved v2.2 QB
    math: Bo Nix penalty, Daniels at the top, etc.). Daniels has 24
    games -> 24/32 = 0.75 on the unchanged QB formula.
    """
    engine, _ = engine_and_site
    daniels = next(
        (r for r in engine.rankings if r["name"] == "Jayden Daniels"),
        None,
    )
    if daniels is None:
        pytest.skip("Daniels not in rankings")
    assert daniels["sample_confidence"] == pytest.approx(0.75, abs=0.02), (
        f"Daniels confidence {daniels['sample_confidence']} - "
        f"QB math changed when it shouldn't have"
    )


def test_established_wr_confidence_unchanged(engine_and_site):
    """Established multi-year WRs were already at conf=1.0 before the
    retune. They must still be at 1.0 after.
    """
    engine, _ = engine_and_site
    for name in ("Justin Jefferson", "Ja'Marr Chase", "Amon-Ra St. Brown"):
        row = next(
            (r for r in engine.rankings if r["name"] == name), None,
        )
        if row is None:
            continue
        assert row["sample_confidence"] >= 0.99, (
            f"{name} confidence {row['sample_confidence']} - "
            f"established WRs should still be at full confidence"
        )


def test_one_nfl_season_rookie_engine_unchanged(engine_and_site):
    """1-NFL-season rookies route through the rookie engine which is
    exempt from the v2.2 sample_confidence shrinkage. McMillan and
    Jeanty (both 1-NFL-season rookies with 17 games) must stay in the
    top tier.

    v3.1 update (2026-05-24): threshold relaxed from top-30 to top-50
    because the proven-production floor lifts banked veterans into
    the top 20–40 (Stafford, Goff, Baker, Dak, Burrow, Kyler, Henry,
    Adams, etc.). 1-NFL-season rookies have zero banked production
    so the floor doesn't help them; they rank purely on the rookie
    engine projection. The rookie engine itself is unchanged — the
    test threshold reflects the reshuffled ABSOLUTE ranks.
    """
    engine, _ = engine_and_site
    for name, max_rank in (
        ("Tetairoa McMillan", 50),
        ("Ashton Jeanty", 50),
    ):
        rank = _rank(engine, name)
        assert rank is not None and rank <= max_rank, (
            f"{name} rank #{rank} - rookie engine path should keep "
            f"them top-{max_rank}"
        )
