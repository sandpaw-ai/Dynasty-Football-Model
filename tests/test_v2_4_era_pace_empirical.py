"""v2.4 PR 3 — empirical era-pace snapshot tests.

Validates:
  1. The JSON snapshot at ``data/engine_v2/era_pace_multipliers_v2.4.json``
     exists and parses cleanly.
  2. ``build_era_pace_table`` returns the empirical_snapshot table when
     ``USE_PRE1999_CORPUS`` is on AND the snapshot exists.
  3. ``build_era_pace_table`` returns the corpus-derived table (source=corpus)
     when the flag is off — v1.x behaviour preserved byte-for-byte.
  4. ``EraPaceTable.get`` falls back to FALLBACK_MULTIPLIERS for cells that
     are missing from the carried multipliers.

Run with ``pytest tests/test_v2_4_era_pace_empirical.py -v``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from dynasty.engine.era_pace import (
    EMPIRICAL_MULTIPLIERS_PATH,
    EraPaceTable,
    FALLBACK_MULTIPLIERS,
    load_empirical_table,
)
from dynasty.engine.similarity_v1 import (
    build_era_pace_table,
    load_corpus,
)


SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / EMPIRICAL_MULTIPLIERS_PATH


# ---------------------------------------------------------------------------
# 1. JSON snapshot exists and parses
# ---------------------------------------------------------------------------

def test_empirical_snapshot_file_exists():
    assert SNAPSHOT_PATH.exists(), (
        f"Empirical era-pace snapshot missing at {SNAPSHOT_PATH}. "
        "Regenerate via the v2.4 PR 3 build."
    )


def test_empirical_snapshot_schema():
    raw = json.loads(SNAPSHOT_PATH.read_text())
    assert raw["source"] == "corpus"
    assert raw["n_careers"] > 3000  # 1999+ alone is ~3k; unified is ~4k
    mults = raw["multipliers"]
    assert set(mults.keys()) == {"QB", "RB", "WR", "TE"}
    # Era 4 anchor must be exactly 1.0 by construction.
    for pos, stats in mults.items():
        for stat, eras in stats.items():
            assert eras.get("4", 1.0) == pytest.approx(1.0, abs=1e-9), (
                f"{pos}.{stat} era 4 = {eras.get('4')}, must be 1.0"
            )


def test_load_empirical_table_returns_table():
    table = load_empirical_table()
    assert table is not None
    assert isinstance(table, EraPaceTable)
    assert table.source == "empirical_snapshot"
    # Sample known values from the v2.4 snapshot.
    rb_rush_era1 = table.get("RB", "rushing_yards", 1)
    assert 0.85 < rb_rush_era1 < 0.95, (
        f"RB rushing_yards era 1 empirical = {rb_rush_era1}, "
        "expected ~0.91 (pre-1999 RB rushing was higher per-game)"
    )


def test_load_empirical_table_missing_file_returns_none(tmp_path):
    missing = tmp_path / "no-snapshot-here.json"
    table = load_empirical_table(snapshot_path=missing)
    assert table is None


def test_load_empirical_table_unparseable_file_returns_none(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    table = load_empirical_table(snapshot_path=bad)
    assert table is None


# ---------------------------------------------------------------------------
# 2. build_era_pace_table — flag ON prefers empirical, flag OFF stays corpus
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def careers_off():
    return load_corpus(use_pre1999=False)


@pytest.fixture(scope="module")
def careers_on():
    return load_corpus(use_pre1999=True)


def test_flag_off_uses_corpus_path(careers_off):
    """With USE_PRE1999_CORPUS=False, ``build_era_pace_table`` returns a
    corpus-derived table (source='corpus') — v1.x behaviour preserved.
    """
    table = build_era_pace_table(careers_off, use_pre1999=False)
    assert table.source == "corpus"


def test_flag_on_prefers_empirical_snapshot(careers_on):
    """With USE_PRE1999_CORPUS=True AND the snapshot present,
    ``build_era_pace_table`` returns the snapshotted empirical table.
    """
    table = build_era_pace_table(careers_on, use_pre1999=True)
    assert table.source == "empirical_snapshot"


def test_flag_on_opt_out_of_snapshot(careers_on):
    """``prefer_snapshot=False`` falls back to fresh corpus derivation
    even with the flag on. Useful for testing & re-baseline runs.
    """
    table = build_era_pace_table(
        careers_on, use_pre1999=True, prefer_snapshot=False,
    )
    assert table.source == "corpus"


# ---------------------------------------------------------------------------
# 3. EraPaceTable.get falls back to FALLBACK_MULTIPLIERS for missing cells
# ---------------------------------------------------------------------------

def test_missing_cell_falls_back_to_fallback_multipliers():
    """An EraPaceTable carrying partial multipliers should fall back to
    FALLBACK_MULTIPLIERS cell-by-cell, NOT silently return 1.0.
    """
    partial = EraPaceTable(
        multipliers={"QB": {"passing_yards": {1: 1.25}}},
        source="hybrid",
    )
    # Cell present → returns carried value.
    assert partial.get("QB", "passing_yards", 1) == pytest.approx(1.25)
    # Cell missing → falls back to FALLBACK_MULTIPLIERS.
    fb_qb_pt_era2 = FALLBACK_MULTIPLIERS["QB"]["passing_tds"][2]
    assert partial.get("QB", "passing_tds", 2) == pytest.approx(fb_qb_pt_era2)
    # Position+stat both missing → falls back to FALLBACK_MULTIPLIERS RB
    # rushing_yards era 1 = 1.0.
    assert partial.get("RB", "rushing_yards", 1) == pytest.approx(
        FALLBACK_MULTIPLIERS["RB"]["rushing_yards"][1]
    )


def test_completely_unknown_returns_1():
    """Position not in any table → final degenerate fallback of 1.0."""
    partial = EraPaceTable(multipliers={}, source="hybrid")
    assert partial.get("K", "field_goals", 1) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 4. The empirical snapshot matches the published delta-doc expectations
# ---------------------------------------------------------------------------

def test_empirical_snapshot_known_cells():
    """Pin a few cells from the v2.4 empirical snapshot so future changes
    surface as test diffs. The values listed below are from
    ``docs/V2.4-ERA-PACE-DELTA.md``.
    """
    table = load_empirical_table()
    assert table is not None
    # RB receptions era 1 = 0.7619 (fallback was 1.20)
    assert table.get("RB", "receptions", 1) == pytest.approx(0.7619, abs=0.01)
    # WR receiving_yards era 1 = 0.6742
    assert table.get("WR", "receiving_yards", 1) == pytest.approx(0.6742, abs=0.01)
    # TE receptions era 1 = 0.9146
    assert table.get("TE", "receptions", 1) == pytest.approx(0.9146, abs=0.01)
    # QB rushing_yards era 1 = 1.6126 (clamped near the upper bound)
    assert table.get("QB", "rushing_yards", 1) == pytest.approx(1.6126, abs=0.01)
