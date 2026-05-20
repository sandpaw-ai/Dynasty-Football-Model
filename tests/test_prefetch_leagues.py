"""Test the prefetch_leagues script with mock leagues.json + fixture clients.

Verifies the script:
  - Reads leagues.json
  - Writes per-league JSON files
  - Writes index.json manifest
  - Captures errors per-entry without crashing
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///./test_prefetch.db"
if os.path.exists("./test_prefetch.db"):
    os.remove("./test_prefetch.db")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Player, CompositeScore
import prefetch_leagues


def _seed_db():
    init_db()
    gen_at = datetime(2026, 5, 20, 12, 0, 0)
    with get_session() as session:
        p = Player(sleeper_id="S1", full_name="Test Player", position="WR")
        session.add(p)
        session.flush()
        session.add(CompositeScore(
            player_id=p.id, league_format="sf_ppr",
            score=80.0, overall_rank=10, position_rank=3, tier=2,
            generated_at=gen_at, model_version="0.3.0",
            breakdown_json="{}",
        ))


def test_empty_config_writes_empty_manifest():
    """If leagues.json has no entries, we still write index.json."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "leagues"
        with patch.object(prefetch_leagues, "_load_config", return_value=[]):
            summary = prefetch_leagues.prefetch_all(output_dir=out)

        assert summary["leagues"] == []
        assert summary["errors"] == []
        assert (out / "index.json").exists()
        with open(out / "index.json") as f:
            idx = json.load(f)
        assert idx["leagues"] == []


def test_unknown_platform_caught_as_error():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "leagues"
        config = [{"platform": "yahoo", "league_id": "999"}]
        with patch.object(prefetch_leagues, "_load_config", return_value=config):
            summary = prefetch_leagues.prefetch_all(output_dir=out)
        assert summary["leagues"] == []
        assert len(summary["errors"]) == 1
        assert "unknown platform" in summary["errors"][0]["error"]


def test_missing_league_id_caught():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "leagues"
        config = [{"platform": "sleeper"}]
        with patch.object(prefetch_leagues, "_load_config", return_value=config):
            summary = prefetch_leagues.prefetch_all(output_dir=out)
        assert len(summary["errors"]) == 1
        assert "missing league_id" in summary["errors"][0]["error"]


def test_sleeper_prefetch_writes_files():
    """Mock the league + manager pipeline; verify the file gets written."""
    _seed_db()

    fake_team_report = type("R", (), {"to_dict": lambda self: {"name": "Mock Sleeper", "teams": [], "power_rankings": []}})()
    fake_manager_report = {"platform": "sleeper", "n_picks": 5, "n_trades": 1, "managers": []}

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "leagues"
        config = [{"platform": "sleeper", "league_id": "999"}]
        with patch.object(prefetch_leagues, "_load_config", return_value=config), \
             patch("dynasty.league.evaluate_sleeper_league", return_value=fake_team_report), \
             patch("dynasty.manager.manager_report_sleeper", return_value=fake_manager_report):
            summary = prefetch_leagues.prefetch_all(output_dir=out)

        assert len(summary["leagues"]) == 1
        L = summary["leagues"][0]
        assert L["platform"] == "sleeper"
        assert L["league_id"] == "999"
        assert L["name"] == "Mock Sleeper"
        assert L["slug"] == "sleeper-999"

        payload_path = out / "sleeper-999.json"
        assert payload_path.exists()
        with open(payload_path) as f:
            payload = json.load(f)
        assert payload["platform"] == "sleeper"
        assert payload["team_report"]["name"] == "Mock Sleeper"
        assert payload["manager_report"]["n_picks"] == 5

        idx_path = out / "index.json"
        with open(idx_path) as f:
            idx = json.load(f)
        assert len(idx["leagues"]) == 1
        assert idx["leagues"][0]["slug"] == "sleeper-999"


def main():
    test_empty_config_writes_empty_manifest(); print("1. empty config -> empty manifest: ✓")
    test_unknown_platform_caught_as_error();   print("2. unknown platform error: ✓")
    test_missing_league_id_caught();           print("3. missing league_id error: ✓")
    test_sleeper_prefetch_writes_files();      print("4. sleeper prefetch writes files: ✓")
    print("\nAll prefetch_leagues tests passed.")


if __name__ == "__main__":
    main()
