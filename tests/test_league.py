"""Tests for the league import / evaluation feature.

Uses synthetic in-memory HTTP responses (no network) for both Sleeper and
MFL paths, and a seeded DB with composite scores so that team evaluations
have something to lock onto.
"""
import os
import sys
from datetime import datetime

os.environ["DATABASE_URL"] = "sqlite:///./test_league.db"
if os.path.exists("./test_league.db"):
    os.remove("./test_league.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Player, CompositeScore
from dynasty.league import evaluate_sleeper_league, evaluate_mfl_league


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload): self._payload = payload
    def json(self): return self._payload
    def raise_for_status(self): pass


_SEEDED = False


def _seed_players_and_scores():
    """Idempotent seed — only inserts the player+score rows the first time."""
    global _SEEDED
    init_db()
    if _SEEDED:
        return
    with get_session() as session:
        # Eight players, 4 per team.
        rows = [
            # (sleeper_id, mfl_id, name, pos, model rank, tier, score)
            ("S1", "M1", "Star WR",       "WR", 1, 1, 99.0),
            ("S2", "M2", "Solid RB",      "RB", 12, 2, 85.0),
            ("S3", "M3", "Decent TE",     "TE", 24, 3, 70.0),
            ("S4", "M4", "Backup QB",     "QB", 80, 5, 40.0),
            ("S5", "M5", "Other WR",      "WR", 25, 3, 67.0),
            ("S6", "M6", "Other RB",      "RB", 45, 4, 55.0),
            ("S7", "M7", "Other TE",      "TE", 90, 6, 35.0),
            ("S8", "M8", "Other QB",      "QB", 6, 1, 92.0),
        ]
        players = []
        for sid, mid, name, pos, _r, _t, _s in rows:
            p = Player(sleeper_id=sid, mfl_id=mid, full_name=name, position=pos)
            session.add(p)
            players.append(p)
        session.flush()
        gen_at = datetime.utcnow()
        for p, (_, _, _, _, rk, tier, sc) in zip(players, rows):
            session.add(CompositeScore(
                player_id=p.id, league_format="sf_ppr",
                score=sc, overall_rank=rk, position_rank=1, tier=tier,
                generated_at=gen_at, model_version="0.3.0",
                breakdown_json="{}",
            ))
    _SEEDED = True


# Sleeper fixture: 2 teams, 4 players each
SLEEPER_LEAGUE_PAYLOAD = {"name": "Test Sleeper League"}
SLEEPER_USERS_PAYLOAD = [
    {"user_id": "U1", "display_name": "Alpha"},
    {"user_id": "U2", "display_name": "Bravo"},
]
SLEEPER_ROSTERS_PAYLOAD = [
    {"roster_id": 1, "owner_id": "U1", "players": ["S1", "S2", "S3", "S4"]},
    {"roster_id": 2, "owner_id": "U2", "players": ["S5", "S6", "S7", "S8"]},
]


class _SleeperClient:
    def get(self, url, *a, **kw):
        if url.endswith("/users"):
            return _Resp(SLEEPER_USERS_PAYLOAD)
        if url.endswith("/rosters"):
            return _Resp(SLEEPER_ROSTERS_PAYLOAD)
        return _Resp(SLEEPER_LEAGUE_PAYLOAD)
    def close(self): pass


def test_sleeper_league_report():
    _seed_players_and_scores()
    report = evaluate_sleeper_league(
        "TESTLEAGUE", league_format="sf_ppr",
        client=_SleeperClient(),
    )
    d = report.to_dict()
    assert d["platform"] == "sleeper"
    assert d["league_id"] == "TESTLEAGUE"
    assert d["name"] == "Test Sleeper League"
    assert len(d["teams"]) == 2

    alpha = next(t for t in d["teams"] if t["display_name"] == "Alpha")
    bravo = next(t for t in d["teams"] if t["display_name"] == "Bravo")

    # Alpha has Star WR + Solid RB + Decent TE + Backup QB
    expected_alpha = 99.0 + 85.0 + 70.0 + 40.0
    assert abs(alpha["total_score"] - expected_alpha) < 0.01
    assert alpha["players_evaluated"] == 4
    assert alpha["players_unrated"] == 0

    # Alpha's top asset is Star WR
    assert alpha["top_assets"][0]["name"] == "Star WR"

    # Alpha's QB is Backup QB (tier 5) → weakness flagged
    assert any("weak QB" in w for w in alpha["weaknesses"])

    # Power rankings — Bravo has Other QB (T1) plus a couple of mid pieces.
    # We don't hard-code the order, just confirm rankings exist & are sorted.
    pr = d["power_rankings"]
    assert pr[0]["total_score"] >= pr[1]["total_score"]
    assert pr[0]["rank"] == 1


def test_sleeper_unrated_player_counted():
    """A roster slot referring to an unknown player ID counts as unrated."""
    _seed_players_and_scores()

    class _Client:
        def get(self, url, *a, **kw):
            if url.endswith("/users"):
                return _Resp([{"user_id": "U1", "display_name": "Alpha"}])
            if url.endswith("/rosters"):
                return _Resp([{
                    "roster_id": 1, "owner_id": "U1",
                    "players": ["S1", "DOESNOTEXIST", "S3"],
                }])
            return _Resp({"name": "Tiny"})
        def close(self): pass

    rep = evaluate_sleeper_league("X", client=_Client())
    alpha = rep.teams[0]
    assert alpha.players_evaluated == 2
    assert alpha.players_unrated == 1


def test_mfl_league_report():
    _seed_players_and_scores()

    mfl_league_payload = {
        "league": {
            "name": "Test MFL League",
            "franchises": {"franchise": [
                {"id": "0001", "name": "Charlie"},
                {"id": "0002", "name": "Delta"},
            ]},
        },
    }
    mfl_rosters_payload = {
        "rosters": {"franchise": [
            {"id": "0001", "player": [
                {"id": "M1"}, {"id": "M2"}, {"id": "M3"}, {"id": "M4"},
            ]},
            {"id": "0002", "player": [
                {"id": "M5"}, {"id": "M6"}, {"id": "M7"}, {"id": "M8"},
            ]},
        ]},
    }

    class _MFLClient:
        def get(self, url, *a, **kw):
            if "TYPE=league" in url:
                return _Resp(mfl_league_payload)
            return _Resp(mfl_rosters_payload)
        def close(self): pass

    rep = evaluate_mfl_league("12345", year=2026, client=_MFLClient())
    d = rep.to_dict()
    assert d["platform"] == "mfl"
    assert d["name"] == "Test MFL League"
    assert len(d["teams"]) == 2
    charlie = next(t for t in d["teams"] if t["display_name"] == "Charlie")
    assert charlie["players_evaluated"] == 4
    # Same lineup as Alpha (M1..M4 mirror S1..S4)
    expected = 99.0 + 85.0 + 70.0 + 40.0
    assert abs(charlie["total_score"] - expected) < 0.01


def test_no_composite_scores_returns_empty_evals():
    """If the model hasn't been scored at a given league_format, league import
    still works but everyone is unrated."""
    _seed_players_and_scores()
    # Use a league_format that has no composite scores in the seeded DB.

    class _Client:
        def get(self, url, *a, **kw):
            if url.endswith("/users"):
                return _Resp([{"user_id": "U1", "display_name": "Solo"}])
            if url.endswith("/rosters"):
                return _Resp([{"roster_id": 1, "owner_id": "U1", "players": ["S1"]}])
            return _Resp({"name": "Empty"})
        def close(self): pass

    rep = evaluate_sleeper_league("X", league_format="nonexistent_format", client=_Client())
    assert rep.teams[0].players_evaluated == 0
    assert rep.teams[0].players_unrated == 1


def main():
    test_sleeper_league_report();        print("1. Sleeper league report: ✓")
    test_sleeper_unrated_player_counted(); print("2. unrated player handling: ✓")
    test_mfl_league_report();             print("3. MFL league report: ✓")
    test_no_composite_scores_returns_empty_evals(); print("4. no composites → all unrated: ✓")
    print("\nAll league tests passed.")


if __name__ == "__main__":
    main()
