"""Tests for the v2.4 pre-1999 PFR scraper + normalizer.

These tests run against a checked-in fixture (1990 rushing HTML) so they
are network-free and fast. The fixture was captured from the Wayback
Machine snapshot of pro-football-reference.com/years/1990/rushing.htm.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Make ``src/`` importable when the tests are run from the repo root.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty.sources.pro_football_reference_seasonal import (
    parse_season_table,
    parse_player_bio,
)


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "pfr_1990_rushing.html"


@pytest.fixture(scope="module")
def rushing_1990_html() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_season_table
# ---------------------------------------------------------------------------

def test_parser_finds_emmitt_and_sanders(rushing_1990_html):
    rows = parse_season_table(rushing_1990_html, "rushing", 1990)
    by_id = {r["pfr_id"]: r for r in rows}

    # The two anchor players the scoping doc names explicitly.
    assert "SmitEm00" in by_id, "Emmitt Smith rookie year missing"
    assert "SandBa00" in by_id, "Barry Sanders 1990 season missing"

    emmitt = by_id["SmitEm00"]
    assert emmitt["player_name"] == "Emmitt Smith"
    assert int(emmitt["rush_yds"]) == 937
    assert int(emmitt["rush_td"]) == 11
    assert emmitt["team"] == "DAL"

    sanders = by_id["SandBa00"]
    assert sanders["player_name"] == "Barry Sanders"
    assert int(sanders["rush_yds"]) == 1304
    assert int(sanders["rush_td"]) == 13


def test_parser_strips_name_markers(rushing_1990_html):
    rows = parse_season_table(rushing_1990_html, "rushing", 1990)
    # PFR appends "*" / "+" to HoF + All-Pro names. Our parser strips them.
    for r in rows:
        assert not r["player_name"].endswith("*"), r
        assert not r["player_name"].endswith("+"), r


def test_parser_attaches_season_and_table(rushing_1990_html):
    rows = parse_season_table(rushing_1990_html, "rushing", 1990)
    assert rows, "no rows parsed"
    assert all(r["season"] == 1990 for r in rows)
    assert all(r["table"] == "rushing" for r in rows)


# ---------------------------------------------------------------------------
# Normalizer (build_season_rows pieces)
# ---------------------------------------------------------------------------

def _load_builder():
    """Load the build script as a module without running main()."""
    spec = importlib.util.spec_from_file_location(
        "build_pre1999_corpus",
        ROOT / "scripts" / "build_pre1999_corpus.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_position_normalization():
    b = _load_builder()
    assert b._normalize_position("HB") == "RB"
    assert b._normalize_position("FB") == "RB"
    assert b._normalize_position("RB") == "RB"
    assert b._normalize_position("FL") == "WR"
    assert b._normalize_position("SE") == "WR"
    assert b._normalize_position("WR") == "WR"
    assert b._normalize_position("TE") == "TE"
    assert b._normalize_position("QB") == "QB"
    # Combo strings: take the first token.
    assert b._normalize_position("RB/FB") == "RB"
    assert b._normalize_position("WR-KR") == "WR"
    # Defensive positions get dropped.
    assert b._normalize_position("LB") is None
    assert b._normalize_position("DB") is None
    assert b._normalize_position("K") is None
    # Empty / missing.
    assert b._normalize_position("") is None
    assert b._normalize_position(None) is None


def test_universe_threshold():
    b = _load_builder()
    # Carries gate.
    assert b._qualifies(carries=50, targets=None, receptions=0, attempts=0)
    assert not b._qualifies(carries=49, targets=None, receptions=0, attempts=0)
    # Targets gate (1992+).
    assert b._qualifies(carries=0, targets=20, receptions=0, attempts=0)
    assert not b._qualifies(carries=0, targets=19, receptions=0, attempts=0)
    # Receptions fallback for pre-1992.
    assert b._qualifies(carries=0, targets=None, receptions=15, attempts=0)
    assert not b._qualifies(carries=0, targets=None, receptions=14, attempts=0)
    # Pass-attempts gate.
    assert b._qualifies(carries=0, targets=None, receptions=0, attempts=100)
    assert not b._qualifies(carries=0, targets=None, receptions=0, attempts=99)


def test_multi_team_collapse_keeps_combined_row(rushing_1990_html):
    """A player who was 2TM in 1990 should yield one collapsed row with
    ``recent_team`` set to the last *actual* team (not "2TM")."""
    b = _load_builder()
    rows = parse_season_table(rushing_1990_html, "rushing", 1990)
    collapsed = b._collapse_multi_team(rows)

    # Mike Rozier was 2TM (HOU → ATL) in 1990 — confirmed against PFR.
    rozier = [r for r in collapsed if r["pfr_id"] == "RoziMi00"]
    assert len(rozier) == 1, f"Rozier should have exactly one collapsed row, got {len(rozier)}"
    # The combined row totals must be preserved (PFR combined rush_yds).
    assert int(rozier[0]["rush_yds"]) > 0
    # And recent_team must point at one of his actual teams, not "2TM".
    assert rozier[0]["recent_team"] in {"HOU", "ATL"}, rozier[0]


def test_multi_team_pattern_excludes_pure_xtm_recent_team():
    """No collapsed row should have a synthetic XTM token in ``recent_team`` —
    but it may remain in ``team`` (we leave that as-is for debugging)."""
    b = _load_builder()
    # Build a tiny synthetic input mimicking PFR layout: one combined +
    # two per-team rows.
    rows = [
        {"pfr_id": "FakeXx00", "team": "2TM",  "name_display": "Fake", "rush_yds": "500"},
        {"pfr_id": "FakeXx00", "team": "DAL",  "name_display": "Fake", "rush_yds": "200"},
        {"pfr_id": "FakeXx00", "team": "WAS",  "name_display": "Fake", "rush_yds": "300"},
    ]
    collapsed = b._collapse_multi_team(rows)
    assert len(collapsed) == 1
    assert collapsed[0]["recent_team"] == "WAS"  # last per-team row
    # Totals from the combined row, not from per-team rows.
    assert collapsed[0]["rush_yds"] == "500"


# ---------------------------------------------------------------------------
# parse_player_bio
# ---------------------------------------------------------------------------

def test_parse_player_bio_extracts_birth_date():
    """The bio parser pulls ``data-birth`` from the necro-birth span."""
    html = """
    <html><head><title>Emmitt Smith Stats | Pro-Football-Reference.com</title></head>
    <body>
      <h1><span>Emmitt Smith</span></h1>
      <div id="info">
        <p>Born: <span id="necro-birth" data-birth="1969-05-15">May 15, 1969</span></p>
      </div>
    </body></html>
    """
    bio = parse_player_bio(html)
    assert bio["birth_date"] == "1969-05-15"
    assert bio["name"] == "Emmitt Smith"


def test_parse_player_bio_regex_fallback():
    """If the necro-birth id is missing, the regex fallback still works."""
    html = '<html><body><div data-birth="1954-07-25">Walter Payton</div></body></html>'
    bio = parse_player_bio(html)
    assert bio["birth_date"] == "1954-07-25"


def test_parse_player_bio_no_birth_returns_none():
    html = "<html><body>nothing useful</body></html>"
    bio = parse_player_bio(html)
    assert bio["birth_date"] is None
