"""Tests for the KeepTradeCut consensus adapter + consensus-vs-model diff.

The KTC HTML page is captured as a static fixture under
``tests/fixtures/ktc_sample.html`` so these tests run offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dynasty.consensus import (
    Crosswalk,
    compare_to_consensus,
    normalize_name,
)
from dynasty.sources.keeptradecut import (
    extract_players_array,
    parse_ktc_html,
    snapshot_from_dict,
    snapshot_to_dict,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ktc_sample.html"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ktc_html() -> str:
    if not FIXTURE.exists():
        pytest.skip("KTC HTML fixture missing; run refresh_ktc_consensus first")
    return FIXTURE.read_text(encoding="utf-8")


def test_extract_players_array_yields_500_players(ktc_html: str):
    arr = extract_players_array(ktc_html)
    assert isinstance(arr, list)
    # KTC publishes the top 500 dynasty players. Allow some headroom in
    # case they change the page size, but anything under 400 means the
    # parser is silently dropping rows.
    assert len(arr) >= 400, f"Expected >=400 players, got {len(arr)}"


def test_parse_ktc_html_normalizes_mcmillan(ktc_html: str):
    snap = parse_ktc_html(ktc_html)
    by_name = {p.name: p for p in snap.players}
    mcm = by_name.get("Tetairoa McMillan")
    assert mcm is not None
    # Per the captured snapshot (2026-05-22).
    assert mcm.position == "WR"
    assert mcm.superflex.rank == 26
    assert mcm.one_qb.rank == 19
    assert mcm.mfl_id == "17071"
    # KTC stamps a value too \u2014 sanity check.
    assert mcm.superflex.value is not None and mcm.superflex.value > 0


def test_snapshot_round_trip(ktc_html: str):
    snap = parse_ktc_html(ktc_html)
    d = snapshot_to_dict(snap)
    snap2 = snapshot_from_dict(d)
    assert len(snap2.players) == len(snap.players)
    # First player must match across the round-trip.
    assert snap2.players[0].name == snap.players[0].name
    assert snap2.players[0].superflex.rank == snap.players[0].superflex.rank


# ---------------------------------------------------------------------------
# Name normalization fallback
# ---------------------------------------------------------------------------

def test_normalize_name_strips_punct_and_suffix():
    assert normalize_name("Tetairoa McMillan") == "tetairoamcmillan"
    assert normalize_name("Marvin Harrison Jr.") == "marvinharrison"
    assert normalize_name("Ja'Marr Chase") == "jamarrchase"
    assert normalize_name("D'Andre Swift") == "dandreswift"
    assert normalize_name("AJ Brown") == "ajbrown"


# ---------------------------------------------------------------------------
# Consensus diff
# ---------------------------------------------------------------------------

def _crosswalk_with(*pairs) -> Crosswalk:
    cw = Crosswalk()
    for ktc_id, gsis in pairs:
        cw.ktc_to_gsis[ktc_id] = gsis
    return cw


def test_compare_to_consensus_pairs_mcmillan_correctly(ktc_html: str):
    """End-to-end: model row + KTC row + crosswalk -> matched diff."""
    snap = parse_ktc_html(ktc_html)
    # Two model rows: McMillan at rank 21, a filler at rank 22 so the
    # production_score ordering is unambiguous.
    model_rankings = [
        {
            "player_id": "00-0040124",  # Tetairoa McMillan gsis_id
            "name": "Tetairoa McMillan",
            "position": "WR",
            "age": 23,
            "production_score": 1723.2,
            "slug": "tetairoa-mcmillan-040124",
        },
        {
            "player_id": "00-0040999",
            "name": "Filler Player",
            "position": "WR",
            "age": 25,
            "production_score": 100.0,
        },
    ]
    cmp = compare_to_consensus(
        model_rankings=model_rankings,
        ktc_snapshot=snap,
        crosswalk=_crosswalk_with((1771, "00-0040124")),
        league_format="sf_ppr",
    )
    matched = [r for r in cmp.rows if r.name == "Tetairoa McMillan"]
    assert len(matched) == 1
    row = matched[0]
    assert row.model_rank == 1  # Highest production_score among model_rankings
    assert row.consensus_rank == 26
    assert row.delta == 1 - 26  # -25
    assert row.consensus_value == 6589
    # Filler did not have a crosswalk entry \u2192 model_only count.
    assert cmp.n_model_only == 1


def test_compare_to_consensus_1qb_uses_oneqb_ranks(ktc_html: str):
    snap = parse_ktc_html(ktc_html)
    model_rankings = [{
        "player_id": "00-0040124",
        "name": "Tetairoa McMillan",
        "position": "WR",
        "age": 23,
        "production_score": 1723.2,
    }]
    cmp = compare_to_consensus(
        model_rankings=model_rankings,
        ktc_snapshot=snap,
        crosswalk=_crosswalk_with((1771, "00-0040124")),
        league_format="1qb_ppr",
    )
    assert cmp.rows[0].consensus_rank == 19  # KTC 1QB rank for McMillan


def test_compare_to_consensus_name_fallback(ktc_html: str):
    """When no ktc_id/mfl_id crosswalk hit, the matcher falls back to
    normalized (name, position)."""
    snap = parse_ktc_html(ktc_html)
    cw = Crosswalk()
    cw.name_to_gsis[("tetairoamcmillan", "WR")] = "00-0040124"
    model_rankings = [{
        "player_id": "00-0040124",
        "name": "Tetairoa McMillan",
        "position": "WR",
        "age": 23,
        "production_score": 1723.2,
    }]
    cmp = compare_to_consensus(
        model_rankings=model_rankings,
        ktc_snapshot=snap,
        crosswalk=cw,
        league_format="sf_ppr",
    )
    assert cmp.rows[0].consensus_rank == 26


def test_unmatched_ktc_rows_are_counted(ktc_html: str):
    """KTC players we can't resolve to a model gsis_id increment the
    `n_unmatched_consensus` counter rather than silently disappearing."""
    snap = parse_ktc_html(ktc_html)
    cmp = compare_to_consensus(
        model_rankings=[],  # No model rows \u2192 every KTC row is unmatched
        ktc_snapshot=snap,
        crosswalk=Crosswalk(),  # Empty crosswalk \u2192 everything falls through
        league_format="sf_ppr",
    )
    # With an empty crosswalk we degrade to name fallback only, and the
    # name index is empty too, so we expect every skill-position KTC row
    # to be unmatched.
    skill_count = sum(
        1 for p in snap.players if p.position in ("QB", "RB", "WR", "TE")
        and p.superflex.rank is not None
    )
    assert cmp.n_unmatched_consensus == skill_count
    assert len(cmp.rows) == 0


def test_consensus_only_counter_reflects_ktc_minus_model(ktc_html: str):
    """If a player is in KTC and crosswalkable but absent from the model
    rankings, they should show up under `n_consensus_only`."""
    snap = parse_ktc_html(ktc_html)
    cw = _crosswalk_with((1771, "00-0040124"))
    cmp = compare_to_consensus(
        model_rankings=[],  # No model rows at all
        ktc_snapshot=snap,
        crosswalk=cw,
        league_format="sf_ppr",
    )
    # Exactly one player matched the crosswalk (McMillan) but isn't in
    # the model rankings \u2192 consensus_only == 1.
    assert cmp.n_consensus_only == 1
