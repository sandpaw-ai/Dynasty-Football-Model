"""Tests for the v3.0 SOS scraper + parser.

Network-free: these run against checked-in Wayback HTML fixtures
(2010 and 2019 standings pages).
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty.sources.sports_reference_cfb_standings import (  # noqa: E402
    parse_standings,
    _normalize_conference,
    _conference_tier,
    _strip_division,
)


FIX_2010 = ROOT / "tests" / "fixtures" / "sr_cfb_standings_2010.html"
FIX_2019 = ROOT / "tests" / "fixtures" / "sr_cfb_standings_2019.html"


@pytest.fixture(scope="module")
def rows_2010():
    return parse_standings(FIX_2010.read_text(encoding="utf-8"), 2010)


@pytest.fixture(scope="module")
def rows_2019():
    return parse_standings(FIX_2019.read_text(encoding="utf-8"), 2019)


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------

def test_2010_has_many_teams(rows_2010):
    # FBS in 2010 was ~120 teams; should be at least 100.
    assert len(rows_2010) >= 100, f"too few rows: {len(rows_2010)}"


def test_2010_schema_complete(rows_2010):
    """Every row must carry the full v3.0 SOS schema."""
    required = {
        "year", "school", "school_canonical_slug",
        "conference", "conference_tier",
        "wins", "losses", "srs", "sos",
    }
    for r in rows_2010:
        missing = required - set(r.keys())
        assert not missing, f"row {r.get('school')} missing: {missing}"


def test_year_is_propagated(rows_2010, rows_2019):
    assert all(r["year"] == 2010 for r in rows_2010)
    assert all(r["year"] == 2019 for r in rows_2019)


# ---------------------------------------------------------------------------
# Spot checks: Phil-cited canonical cases
# ---------------------------------------------------------------------------

def _by_school(rows):
    return {r["school"]: r for r in rows}


def test_alabama_2010_spot_values(rows_2010):
    """Alabama 2010 was 10-3, SEC, +4.69 SOS, +18.31 SRS (verified vs SR)."""
    bama = _by_school(rows_2010)["Alabama"]
    assert bama["wins"] == 10
    assert bama["losses"] == 3
    assert bama["conference"] == "SEC"
    assert bama["conference_tier"] == "P5"
    assert bama["sos"] == pytest.approx(4.69, abs=0.01)
    assert bama["srs"] == pytest.approx(18.31, abs=0.01)
    assert bama["school_canonical_slug"] == "alabama"


def test_boise_state_2010_is_low_sos(rows_2010):
    """The Kellen Moore problem: Boise State 2010 SOS roughly 0 to -2.

    Verified from Wayback: SOS=-0.70. Their SRS is good (18.69) but
    schedule is among the weakest in FBS — exactly the asymmetry the
    SOS multiplier fixes downstream.
    """
    boise = _by_school(rows_2010)["Boise State"]
    assert boise["wins"] == 12
    assert boise["losses"] == 1
    assert boise["conference_tier"] == "G5"  # WAC in 2010
    assert -2.0 <= boise["sos"] <= 0.0


def test_alabama_2011_sos_vs_boise_2011():
    """Alabama 2011 must have substantially higher SOS than Boise 2011.

    Uses the 2019 fixture for Alabama/Boise to test the same pattern
    in a different year — both are SEC vs MWC matchups.
    """
    # We use 2019 since it's the second checked-in fixture.
    html = FIX_2019.read_text(encoding="utf-8")
    rows = parse_standings(html, 2019)
    by_school = _by_school(rows)
    bama = by_school["Alabama"]
    boise = by_school["Boise State"]
    assert bama["sos"] > boise["sos"], (
        f"Alabama 2019 SOS ({bama['sos']}) should exceed Boise 2019 "
        f"SOS ({boise['sos']}) by a clear margin"
    )
    # And the margin should be large (>= 3 SOS points typically).
    assert bama["sos"] - boise["sos"] >= 3.0


def test_lsu_2019_extreme_positive(rows_2019):
    """LSU's championship year: SOS high, SRS extreme positive."""
    lsu = _by_school(rows_2019)["LSU"]
    assert lsu["wins"] == 15
    assert lsu["losses"] == 0
    assert lsu["sos"] >= 5.0, f"LSU 2019 SOS surprisingly low: {lsu['sos']}"
    assert lsu["srs"] >= 20.0, f"LSU 2019 SRS surprisingly low: {lsu['srs']}"
    # LSU canonical slug is "louisiana-state" on sports-reference.
    assert lsu["school_canonical_slug"] == "louisiana-state"


def test_connecticut_2019_deep_negative(rows_2019):
    """UConn 2019 (terrible independent year): SOS moderate-negative,
    SRS deeply negative."""
    uconn = _by_school(rows_2019)["Connecticut"]
    assert uconn["losses"] >= 9
    assert uconn["srs"] <= -10.0, (
        f"UConn 2019 SRS should be deeply negative: {uconn['srs']}"
    )


# ---------------------------------------------------------------------------
# Aggregate / corpus-level sanity
# ---------------------------------------------------------------------------

def test_sec_average_sos_exceeds_lower_tiers(rows_2019):
    """SEC team SOS averages should exceed lower-tier conference averages."""
    sec = [r["sos"] for r in rows_2019
           if r["conference"] == "SEC" and r["sos"] is not None]
    mac = [r["sos"] for r in rows_2019
           if r["conference"] == "Mid-American" and r["sos"] is not None]
    assert sec and mac, "expected both SEC and MAC rows in 2019"
    assert statistics.mean(sec) > statistics.mean(mac), (
        f"SEC mean SOS ({statistics.mean(sec):.2f}) should exceed "
        f"MAC mean SOS ({statistics.mean(mac):.2f})"
    )


def test_sos_values_in_plausible_range(rows_2010, rows_2019):
    """SOS values are SRS-derived; magnitudes should sit roughly in
    [-15, +15] for any FBS-or-near team in a non-pandemic year."""
    for rows in (rows_2010, rows_2019):
        for r in rows:
            if r["sos"] is None:
                continue
            assert -15.0 <= r["sos"] <= 15.0, (
                f"{r['year']} {r['school']} SOS out of range: {r['sos']}"
            )


def test_empty_sos_returns_none():
    """A team row that's missing the SOS cell parses to ``None``, not
    a stringly-typed empty value."""
    # Minimal synthetic standings table with one row missing SOS/SRS.
    minimal = """
    <table id="standings">
      <tbody>
        <tr>
          <th data-stat="ranker">1</th>
          <td data-stat="school_name"><a href="/cfb/schools/test-school/2020.html">Test School</a></td>
          <td data-stat="conf_abbr">SEC</td>
          <td data-stat="wins">10</td>
          <td data-stat="losses">2</td>
          <td data-stat="srs"></td>
          <td data-stat="sos"></td>
        </tr>
      </tbody>
    </table>
    """
    rows = parse_standings(minimal, 2020)
    assert len(rows) == 1
    r = rows[0]
    assert r["school"] == "Test School"
    assert r["wins"] == 10
    assert r["sos"] is None
    assert r["srs"] is None
    assert r["school_canonical_slug"] == "test-school"


# ---------------------------------------------------------------------------
# School + conference normalization
# ---------------------------------------------------------------------------

def test_lsu_canonical_slug_is_louisiana_state(rows_2019):
    """The Phil-cited 'LSU is Louisiana State on SR' canonicalization
    works automatically: we pull the slug from the table's <a href>."""
    lsu = _by_school(rows_2019)["LSU"]
    assert lsu["school_canonical_slug"] == "louisiana-state"


def test_usc_canonical_slug_is_southern_california(rows_2019):
    """USC canonicalizes to slug ``southern-california`` even when the
    display name on sports-reference is the short form ``USC``. The
    slug is the join key for the rest of the v3.0 pipeline."""
    by = _by_school(rows_2019)
    usc = by.get("USC")
    assert usc is not None, "USC row missing in 2019"
    assert usc["school_canonical_slug"] == "southern-california"


def test_strip_division_handles_parens():
    assert _strip_division("ACC(Atlantic)") == "ACC"
    assert _strip_division("SEC(West)") == "SEC"
    assert _strip_division("Big 12(South)") == "Big 12"
    assert _strip_division("American") == "American"
    assert _strip_division("") == ""


def test_normalize_conference_maps_sr_abbrevs():
    assert _normalize_conference("ACC(Atlantic)") == "ACC"
    assert _normalize_conference("CUSA(East)") == "Conference USA"
    assert _normalize_conference("MWC(Mountain)") == "Mountain West"
    assert _normalize_conference("Ind") == "FBS Independents"
    assert _normalize_conference("Pac-10") == "Pac-10"
    assert _normalize_conference("Pac-12(South)") == "Pac-12"
    # Unknown passes through (defensive — surfaces drift instead of
    # silently corrupting).
    assert _normalize_conference("MadeUp") == "MadeUp"


def test_conference_tier_matches_cfbfastr_scheme():
    assert _conference_tier("SEC") == "P5"
    assert _conference_tier("ACC") == "P5"
    assert _conference_tier("Big Ten") == "P5"
    assert _conference_tier("Pac-12") == "P5"
    assert _conference_tier("Pac-10") == "P5"
    assert _conference_tier("Big East") == "P5"  # FBS-era Big East
    assert _conference_tier("American Athletic") == "G5_top"
    assert _conference_tier("Mountain West") == "G5_top"
    assert _conference_tier("Sun Belt") == "G5_top"
    assert _conference_tier("Conference USA") == "G5"
    assert _conference_tier("Mid-American") == "G5"
    assert _conference_tier("WAC") == "G5"
    assert _conference_tier("MVFC") == "FCS"
    assert _conference_tier("SWAC") == "FCS"
    assert _conference_tier("Unknown") is None  # surfaces drift
