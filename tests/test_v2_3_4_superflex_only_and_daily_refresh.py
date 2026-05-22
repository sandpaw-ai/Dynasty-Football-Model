"""Tests for v2.3.4 (Phil 2026-05-22):

  1. Dynasty Rankings tab is Superflex-only - drop the 1QB PPR toggle.
  2. Player-name cells link to /players/<slug>.html (the similarity
     score page) for EVERY consensus row, not just the top 300.
  3. The headless launcher refreshes every external data source
     (nflverse stats + players, KTC consensus, dynastyprocess
     crosswalk, Sleeper, MFL) on every invocation, so the daily
     rebuild always sees fresh inputs.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def site(tmp_path_factory):
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
    return out_dir, engine


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Part 1: Dynasty Rankings is Superflex-only
# ---------------------------------------------------------------------------

def test_no_format_toggle_buttons(site):
    """Phil 2026-05-22: 'On Dynasty Rankings tab it should only be
    Superflex PPR. Let's get rid of the 1QB PPR format button.'
    """
    out_dir, _ = site
    html = _read(out_dir / "league.html")
    if "const CONSENSUS" not in html:
        pytest.skip("legacy overlay fallback rendered (no KTC snapshot)")
    # No btn-<fmt> ids should exist on the page anymore.
    assert re.search(r'id="btn-[a-z0-9_]+"', html) is None, (
        "Dynasty Rankings should not have format toggle buttons"
    )
    # Static Superflex PPR label remains.
    assert "Superflex PPR" in html


def test_payload_only_has_sf_ppr(site):
    """The CONSENSUS JSON payload should only carry the sf_ppr key."""
    out_dir, _ = site
    html = _read(out_dir / "league.html")
    m = re.search(r'const CONSENSUS = (\{.*?\});\s*\n', html, re.DOTALL)
    if m is None:
        pytest.skip("legacy overlay rendered")
    payload = json.loads(m.group(1))
    assert list(payload.keys()) == ["sf_ppr"], (
        f"unexpected format keys in CONSENSUS payload: {list(payload.keys())}"
    )


# ---------------------------------------------------------------------------
# Part 2: every consensus row links to a real player page
# ---------------------------------------------------------------------------

def test_every_consensus_row_has_a_player_page(site):
    """Phil 2026-05-22: 'when you click into a player in the dynasty
    rankings tab this should link to the player's similarity score.'

    Every row's slug field must reference a file under players/ on
    disk. Pre-v2.3.4 most slugs were null because the engine row
    didn't carry one; v2.3.4 computes it from (name, player_id) and
    also generates a player page for EVERY ranked player so the
    deep-tail rows aren't orphaned.
    """
    out_dir, _ = site
    html = _read(out_dir / "league.html")
    m = re.search(r'const CONSENSUS = (\{.*?\});\s*\n', html, re.DOTALL)
    if m is None:
        pytest.skip("legacy overlay rendered")
    rows = json.loads(m.group(1))["sf_ppr"]["rows"]
    existing = set(os.listdir(out_dir / "players"))
    missing = []
    for r in rows:
        slug = r.get("slug")
        if not slug:
            missing.append((r["model_rank"], r["name"], "<null slug>"))
        elif f"{slug}.html" not in existing:
            missing.append((r["model_rank"], r["name"], slug))
    assert not missing, (
        f"{len(missing)} consensus rows have broken player-page links; "
        f"first 5: {missing[:5]}"
    )


def test_anchor_template_in_render_js(site):
    """The render() JS that draws each row must contain the anchor
    template ``<a href="players/...">``. The actual anchor tags are
    DOM-rendered at runtime, so the source HTML carries the template
    not the rendered output.
    """
    out_dir, _ = site
    html = _read(out_dir / "league.html")
    if "const CONSENSUS" not in html:
        pytest.skip("legacy overlay rendered")
    assert "'<a href=\"players/'" in html, (
        "render() JS must wrap player names in /players/<slug>.html anchors"
    )


# ---------------------------------------------------------------------------
# Part 3: daily refresh of all external data sources
# ---------------------------------------------------------------------------

def test_launcher_runs_nflverse_refresh():
    """The headless launcher must call `refresh_nflverse_corpus.refresh`
    on every invocation so the engine sees fresh stats. Source-level
    test - we don't run the full launcher in CI (it'd hit GitHub).
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "dynasty" / "launcher_headless.py"
    ).read_text(encoding="utf-8")
    assert "refresh_nflverse_corpus" in src, (
        "launcher_headless must import refresh_nflverse_corpus"
    )
    assert "refresh_nflverse_corpus.refresh(" in src, (
        "launcher_headless must invoke refresh_nflverse_corpus.refresh()"
    )


def test_launcher_runs_ktc_refresh():
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "dynasty" / "launcher_headless.py"
    ).read_text(encoding="utf-8")
    assert "refresh_ktc_consensus" in src
    assert "refresh_ktc_consensus.refresh(" in src


def test_launcher_runs_sleeper_and_mfl_sync():
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "dynasty" / "launcher_headless.py"
    ).read_text(encoding="utf-8")
    assert "sync_sleeper_players" in src
    assert "sync_mfl_players" in src


def test_nflverse_refresh_runs_before_engine():
    """Ordering invariant: the nflverse refresh must run BEFORE the
    engine. The engine reads from `data/nflverse/` so a stale cache
    would silently produce a stale ranking.
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "dynasty" / "launcher_headless.py"
    ).read_text(encoding="utf-8")
    nflverse_idx = src.find("refresh_nflverse_corpus.refresh(")
    engine_idx = src.find("run_engine(")
    assert nflverse_idx > 0, "refresh call missing"
    assert engine_idx > 0, "engine call missing"
    assert nflverse_idx < engine_idx, (
        "refresh_nflverse_corpus must run BEFORE run_engine"
    )


# ---------------------------------------------------------------------------
# Part 4: refresh_nflverse_corpus.current_nfl_season heuristic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("today,expected", [
    (date(2026, 5, 22), 2025),   # offseason after 2025 ended
    (date(2026, 9, 15), 2026),   # 2026 season in progress
    (date(2026, 1, 5), 2025),    # postseason of 2025 - file is 2025
    (date(2027, 2, 3), 2026),    # Feb after 2026 ended
    (date(2025, 12, 1), 2025),   # mid-2025 regular season
    (date(2026, 8, 31), 2025),   # Aug, just before kickoff
    (date(2026, 9, 1), 2026),    # Sept 1 - new season window opens
])
def test_current_nfl_season_heuristic(today, expected):
    """Pin the season-detection rule so the daily refresh always asks
    nflverse for the right year.
    """
    import sys
    scripts_dir = (
        Path(__file__).resolve().parent.parent / "scripts"
    )
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from refresh_nflverse_corpus import current_nfl_season
    assert current_nfl_season(today) == expected, (
        f"current_nfl_season({today}) returned "
        f"{current_nfl_season(today)} but expected {expected}"
    )
