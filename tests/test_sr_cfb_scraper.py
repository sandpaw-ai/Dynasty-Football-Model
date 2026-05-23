"""Tests for the v3.0 sports-reference.com CFB scraper + normalizer.

Network-free \u2014 every test reads from checked-in fixture HTML.

Fixtures captured from the Internet Archive Wayback Machine:

* ``sr_cfb_2010_passing.html`` \u2014 top-50 rows of /cfb/years/2010-passing.html
* ``sr_cfb_2010_rushing.html`` \u2014 top-50 rows of /cfb/years/2010-rushing.html
* ``sr_cfb_tim_tebow.html``    \u2014 Tim Tebow's player page (passing / rushing / scoring tables)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty.sources import sports_reference_cfb as sr  # noqa: E402

FIX_DIR = ROOT / "tests" / "fixtures"
PASSING_FIX = FIX_DIR / "sr_cfb_2010_passing.html"
RUSHING_FIX = FIX_DIR / "sr_cfb_2010_rushing.html"
TEBOW_FIX = FIX_DIR / "sr_cfb_tim_tebow.html"


@pytest.fixture(scope="module")
def passing_2010_html() -> str:
    return PASSING_FIX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rushing_2010_html() -> str:
    return RUSHING_FIX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tebow_html() -> str:
    return TEBOW_FIX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Leaderboard parser
# ---------------------------------------------------------------------------

def test_passing_leaderboard_parses_known_qb(passing_2010_html):
    rows = sr.parse_year_leaderboard(passing_2010_html, "passing", 2010)
    by_slug = {r["sr_slug"]: r for r in rows}
    # The 2010 passing leaderboard's #1 was Bryant Moniz of Hawaii \u2014
    # 5,040 passing yards, 39 TDs, WAC. (Verified against PFR-style
    # historical references.)
    assert "bryant-moniz-1" in by_slug
    moniz = by_slug["bryant-moniz-1"]
    assert moniz["player_name"] == "Bryant Moniz"
    assert moniz["team"] == "Hawaii"
    assert moniz["conference"] == "WAC"
    assert int(moniz["pass_yds"]) == 5040
    assert int(moniz["pass_td"]) == 39
    assert moniz["season"] == 2010
    assert moniz["table"] == "passing"


def test_passing_leaderboard_kellen_moore_2010(passing_2010_html):
    """Kellen Moore at Boise State (WAC) \u2014 the canonical low-SOS QB.

    This is the Phil case for the SOS adjustment landing in PR 2/3.
    """
    rows = sr.parse_year_leaderboard(passing_2010_html, "passing", 2010)
    by_slug = {r["sr_slug"]: r for r in rows}
    assert "kellen-moore-1" in by_slug, "Kellen Moore (Boise State 2010) missing"
    km = by_slug["kellen-moore-1"]
    assert km["team"] == "Boise State"
    assert km["conference"] == "WAC"
    assert int(km["pass_yds"]) == 3845
    assert int(km["pass_td"]) == 35


def test_rushing_leaderboard_parses_known_rb(rushing_2010_html):
    rows = sr.parse_year_leaderboard(rushing_2010_html, "rushing", 2010)
    by_slug = {r["sr_slug"]: r for r in rows}
    # The 2010 rushing leaderboard's #1 was LaMichael James (Oregon).
    assert "lamichael-james-1" in by_slug
    lj = by_slug["lamichael-james-1"]
    assert lj["player_name"] == "LaMichael James"
    assert lj["team"] == "Oregon"
    assert lj["conference"] == "Pac-10"
    assert int(lj["rush_yds"]) == 1731
    assert int(lj["rush_td"]) == 21


def test_leaderboard_strips_award_markers(passing_2010_html, rushing_2010_html):
    """SR appends ``*`` / ``+`` to All-American / HoF names."""
    for html, table in (
        (passing_2010_html, "passing"),
        (rushing_2010_html, "rushing"),
    ):
        rows = sr.parse_year_leaderboard(html, table, 2010)
        for r in rows:
            assert not r["player_name"].endswith("*"), r
            assert not r["player_name"].endswith("+"), r


def test_leaderboard_attaches_season_and_table(passing_2010_html):
    rows = sr.parse_year_leaderboard(passing_2010_html, "passing", 2010)
    assert rows, "no rows parsed from passing fixture"
    for r in rows:
        assert r["season"] == 2010
        assert r["table"] == "passing"
        assert r["sr_slug"], "every row must have a slug"


# ---------------------------------------------------------------------------
# Position normalization
# ---------------------------------------------------------------------------

def test_position_normalization_canonical():
    assert sr.normalize_position("QB") == "QB"
    assert sr.normalize_position("RB") == "RB"
    assert sr.normalize_position("WR") == "WR"
    assert sr.normalize_position("TE") == "TE"


def test_position_normalization_aliases():
    # Running back family
    assert sr.normalize_position("HB") == "RB"
    assert sr.normalize_position("TB") == "RB"
    assert sr.normalize_position("FB") == "RB"
    # Wide receiver family
    assert sr.normalize_position("FL") == "WR"
    assert sr.normalize_position("SE") == "WR"
    assert sr.normalize_position("SL") == "WR"
    assert sr.normalize_position("WB") == "WR"


def test_position_normalization_multi_token():
    """Slash-separated multi-position labels take the first skill token."""
    assert sr.normalize_position("QB/WR") == "QB"
    assert sr.normalize_position("RB/HB") == "RB"
    assert sr.normalize_position("WR/RB") == "WR"


def test_position_normalization_drops_non_skill():
    for pos in ("LB", "DB", "DE", "DT", "OG", "OT", "C", "K", "P", "LS"):
        assert sr.normalize_position(pos) is None, pos


def test_position_normalization_handles_empty():
    assert sr.normalize_position("") is None
    assert sr.normalize_position(None) is None


# ---------------------------------------------------------------------------
# Conference \u2192 tier mapping
# ---------------------------------------------------------------------------

def test_conference_tier_p5():
    for c in ("SEC", "Big Ten", "Big 12", "ACC", "Pac-10", "Pac-12"):
        for yr in (2000, 2005, 2010, 2013):
            assert sr.classify_conference_tier(c, yr) == "P5", (c, yr)


def test_conference_tier_big_east_year_sensitive():
    # P5 through 2012, then G5_top (became AAC functionally).
    assert sr.classify_conference_tier("Big East", 2007) == "P5"
    assert sr.classify_conference_tier("Big East", 2012) == "P5"
    assert sr.classify_conference_tier("Big East", 2013) == "G5_top"


def test_conference_tier_g5_top():
    # Mountain West post-2000 \u2192 G5_top
    assert sr.classify_conference_tier("MWC", 2010) == "G5_top"
    assert sr.classify_conference_tier("Mountain West", 2012) == "G5_top"
    # AAC (2013+) \u2192 G5_top
    assert sr.classify_conference_tier("AAC", 2013) == "G5_top"


def test_conference_tier_wac_peak():
    """WAC was G5_top during its 2007-2012 Boise-State peak; otherwise G5."""
    assert sr.classify_conference_tier("WAC", 2003) == "G5"
    assert sr.classify_conference_tier("WAC", 2008) == "G5_top"
    assert sr.classify_conference_tier("WAC", 2010) == "G5_top"


def test_conference_tier_g5():
    assert sr.classify_conference_tier("MAC", 2005) == "G5"
    assert sr.classify_conference_tier("Sun Belt", 2010) == "G5"
    assert sr.classify_conference_tier("C-USA", 2008) == "G5"


def test_conference_tier_fcs_fallback():
    assert sr.classify_conference_tier("Big Sky", 2010) == "FCS"
    assert sr.classify_conference_tier("", 2010) == "FCS"


# ---------------------------------------------------------------------------
# Player-page parser
# ---------------------------------------------------------------------------

def test_player_page_tebow_career_arc(tebow_html):
    rows = sr.parse_player_page(tebow_html, "tim-tebow-1")
    by_season = {r["season"]: r for r in rows}
    assert set(by_season.keys()) == {2006, 2007, 2008, 2009}

    # Sanity checks against the spec's spot-values.
    # 2007 = Heisman year: ~210 pass att... wait, spec says 210; let's
    # check what SR actually has. The PFR mark from the scope doc was
    # rough numbers \u2014 use SR's own values as ground truth here.
    t07 = by_season[2007]
    assert t07["team_name_abbr"] == "Florida"
    assert t07["pos"] == "QB"
    assert int(t07["pass_yds"]) == 3286
    assert int(t07["pass_td"]) == 32
    assert int(t07["rush_yds"]) == 895
    assert int(t07["rush_td"]) == 23


def test_player_page_tebow_schema_conversion(tebow_html):
    rows = sr.parse_player_page(tebow_html, "tim-tebow-1")
    out_2007 = None
    for r in rows:
        norm = sr.row_to_cfb_schema(r)
        if norm and norm["season"] == 2007:
            out_2007 = norm
            break
    assert out_2007 is not None, "Tebow 2007 didn't convert"

    # Verify schema parity with the existing 2014+ corpus.
    expected_keys = set(sr.CFB_SCHEMA_KEYS)
    assert set(out_2007.keys()) == expected_keys

    assert out_2007["cfb_player_id"] == "sr_tim-tebow-1"
    assert out_2007["name"] == "Tim Tebow"
    assert out_2007["team"] == "Florida"
    assert out_2007["conference"] == "SEC"
    assert out_2007["conference_tier"] == "P5"
    assert out_2007["class_year"] == "SO"
    assert out_2007["position"] == "QB"
    assert out_2007["games"] == 13
    assert out_2007["pass_att"] == 350
    assert out_2007["pass_comp"] == 234
    assert out_2007["pass_yds"] == 3286
    assert out_2007["pass_td"] == 32
    assert out_2007["int_thrown"] == 6
    assert out_2007["rush_att"] == 210
    assert out_2007["rush_yds"] == 895
    assert out_2007["rush_td"] == 23
    # Targets are pre-2014-ish unreliable; Tebow as a QB has no rec
    # rows so rec/rec_yds/rec_td are all 0. targets stays None when SR
    # didn't emit the field.
    assert out_2007["targets"] is None


def test_schema_matches_existing_cfbfastr_corpus():
    """The PR-1 output schema MUST match ``season_2024.json`` keys exactly.

    Loads one existing record (so we never copy keys from a stale
    constant) and compares against CFB_SCHEMA_KEYS.
    """
    existing = json.loads(
        (ROOT / "data" / "historical_ncaa_football" / "season_2024.json")
        .read_text(encoding="utf-8")
    )
    assert existing, "season_2024.json is empty?"
    existing_keys = set(existing[0].keys())
    assert existing_keys == set(sr.CFB_SCHEMA_KEYS), (
        f"\nin existing not in schema: {existing_keys - set(sr.CFB_SCHEMA_KEYS)}"
        f"\nin schema not in existing: {set(sr.CFB_SCHEMA_KEYS) - existing_keys}"
    )


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

def test_fetch_player_page_rejects_bogus_slug():
    with pytest.raises(ValueError):
        sr.fetch_player_page("not_a_valid_slug")
    with pytest.raises(ValueError):
        sr.fetch_player_page("Tim Tebow")
    with pytest.raises(ValueError):
        sr.fetch_player_page("../etc/passwd")


# ---------------------------------------------------------------------------
# Orchestration script helpers
# ---------------------------------------------------------------------------

def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_pre2014_cfb_corpus",
        ROOT / "scripts" / "build_pre2014_cfb_corpus.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_qualifies_thresholds():
    b = _load_builder()
    # Rushing gate
    assert b.qualifies({"rush_att": 60})
    assert not b.qualifies({"rush_att": 30, "rec_yds": 50, "pass_yds": 100})
    # Receiving yards gate
    assert b.qualifies({"rec_yds": 250})
    # Reception count gate
    assert b.qualifies({"rec": 25})
    # Passing yards gate
    assert b.qualifies({"pass_yds": 600})
    # Below all thresholds
    assert not b.qualifies({"rush_att": 5, "rec": 2, "pass_yds": 100})
    # Empty dict
    assert not b.qualifies({})


def test_merge_leaderboard_rows_position_inference():
    b = _load_builder()
    # Synthetic input: same slug shows up in passing only -> QB
    rows_by_table = {
        "passing": [
            {
                "sr_slug": "test-qb-1", "season": 2010, "player_name": "Test QB",
                "team": "Florida", "conference": "SEC",
                "pass_att": "300", "pass_yds": "3000", "pass_td": "25",
            },
        ],
        "rushing": [
            {
                "sr_slug": "test-rb-1", "season": 2010, "player_name": "Test RB",
                "team": "Alabama", "conference": "SEC",
                "rush_att": "200", "rush_yds": "1200", "rush_td": "12",
                "rec": "10", "rec_yds": "80",
            },
        ],
        "receiving": [
            {
                "sr_slug": "test-wr-1", "season": 2010, "player_name": "Test WR",
                "team": "USC", "conference": "Pac-10",
                "rec": "60", "rec_yds": "900", "rec_td": "8",
            },
        ],
        "scoring": [],
    }
    merged = b.merge_leaderboard_rows(rows_by_table)
    qb = merged[("test-qb-1", "Florida")]
    rb = merged[("test-rb-1", "Alabama")]
    wr = merged[("test-wr-1", "USC")]
    assert qb["_inferred_position"] == "QB"
    assert rb["_inferred_position"] == "RB"
    assert wr["_inferred_position"] == "WR"


def test_normalize_name_strips_suffixes():
    b = _load_builder()
    assert b._normalize_name("Tim Tebow") == "tim tebow"
    assert b._normalize_name("Tim Tebow Jr.") == "tim tebow"
    assert b._normalize_name("Robert Griffin III") == "robert griffin"
    assert b._normalize_name("D'Onta Foreman") == "donta foreman"
    assert b._normalize_name("Ka'imi Fairbairn") == "kaimi fairbairn"
