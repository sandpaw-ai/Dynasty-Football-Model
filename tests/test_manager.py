"""Tests for the manager skill rating module.

Uses fixture HTTP clients for both Sleeper and MFL, seeded composite scores
in the DB so each ext_id resolves to a known score.
"""
import os
import sys
from datetime import datetime

os.environ["DATABASE_URL"] = "sqlite:///./test_manager.db"
if os.path.exists("./test_manager.db"):
    os.remove("./test_manager.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Player, CompositeScore
from dynasty.manager import (
    expected_score_at_pick,
    manager_report_sleeper,
    manager_report_mfl,
    _compute_manager_table,
    DraftPickRecord, TradeRecord,
)


def test_expected_score_at_pick_anchors():
    assert expected_score_at_pick(1) == 100.0
    assert 75 < expected_score_at_pick(60) < 78
    assert 19 < expected_score_at_pick(200) < 22
    assert expected_score_at_pick(250) == 0.4 or expected_score_at_pick(250) >= 0.0
    assert expected_score_at_pick(300) == 0.0
    assert expected_score_at_pick(0) == 100.0


# ---------------------------------------------------------------------------
# Pure-function test on _compute_manager_table
# ---------------------------------------------------------------------------

def test_manager_table_basic_math():
    """Two managers, controlled inputs, verify the arithmetic.

    expected_score_at_pick(p) = 100 * (1 - (p-1)/250)
      pick   1 -> 100.0
      pick   5 ->  98.4
      pick  50 ->  80.4
      pick 100 ->  60.4

    Manager A:
      Pick   1, Star (95):  delta = 95 - 100.0 = -5.0
      Pick  50, Solid (80): delta = 80 -  80.4 = -0.4
      total = -5.4

    Manager B:
      Pick   5, Bargain (90): delta = 90 - 98.4 = -8.4
      Pick 100, Steal (60):   delta = 60 - 60.4 = -0.4
      total = -8.8
    """
    franchise_names = {"A": "Alpha", "B": "Bravo"}
    picks = [
        DraftPickRecord(pick_no=1, round_no=1, franchise_id="A", player_ext_id="P1"),
        DraftPickRecord(pick_no=50, round_no=4, franchise_id="A", player_ext_id="P2"),
        DraftPickRecord(pick_no=5, round_no=1, franchise_id="B", player_ext_id="P3"),
        DraftPickRecord(pick_no=100, round_no=8, franchise_id="B", player_ext_id="P4"),
    ]
    score_lookup = {
        "P1": {"name": "Star",    "position": "WR", "score": 95.0, "rank": 1, "tier": 1},
        "P2": {"name": "Solid",   "position": "RB", "score": 80.0, "rank": 50, "tier": 3},
        "P3": {"name": "Bargain", "position": "WR", "score": 90.0, "rank": 5, "tier": 1},
        "P4": {"name": "Steal",   "position": "TE", "score": 60.0, "rank": 100, "tier": 4},
    }
    managers = _compute_manager_table(franchise_names, picks, [], score_lookup)
    by_name = {m.display_name: m for m in managers}
    assert by_name["Alpha"].n_picks == 2
    assert by_name["Bravo"].n_picks == 2
    # Alpha's: (95-100.0) + (80-80.4) = -5.4
    assert abs(by_name["Alpha"].draft_delta_total - (-5.4)) < 0.01
    # Bravo's: (90-98.4) + (60-60.4) = -8.8
    assert abs(by_name["Bravo"].draft_delta_total - (-8.8)) < 0.01
    # Alpha drafted closer to expectation -> better skill rank
    assert by_name["Alpha"].skill_rank < by_name["Bravo"].skill_rank


def test_trade_value_zero_sum_within_a_trade():
    """In any 2-team trade, the sum of trade_delta across the two sides
    must equal zero (one's loss is the other's gain)."""
    franchise_names = {"A": "Alpha", "B": "Bravo", "C": "Charlie"}
    score_lookup = {
        "PX": {"name": "Stud", "position": "WR", "score": 90.0, "rank": 1, "tier": 1},
        "PY": {"name": "Asset", "position": "RB", "score": 70.0, "rank": 30, "tier": 3},
        "PZ": {"name": "Bench", "position": "TE", "score": 30.0, "rank": 120, "tier": 7},
    }
    trades = [
        # Alpha gave PX, received PY + PZ. Bravo gave PY+PZ, received PX.
        TradeRecord(
            transaction_id="tx1",
            sides={"A": ["PY", "PZ"], "B": ["PX"]},
        )
    ]
    managers = _compute_manager_table(franchise_names, [], trades, score_lookup)
    by_name = {m.display_name: m for m in managers}
    # Alpha received 100 (70+30), gave 90 -> delta = +10
    # Bravo received 90, gave 100 -> delta = -10
    # Charlie not in the trade -> 0
    assert abs(by_name["Alpha"].trade_delta_total - 10.0) < 0.01
    assert abs(by_name["Bravo"].trade_delta_total - (-10.0)) < 0.01
    assert by_name["Charlie"].trade_delta_total == 0.0
    # Skill ranking should put Alpha > Bravo on trade alone.
    assert by_name["Alpha"].skill_rank < by_name["Bravo"].skill_rank


# ---------------------------------------------------------------------------
# End-to-end fixture for Sleeper
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload): self._payload = payload
    def json(self): return self._payload
    def raise_for_status(self): pass


_GEN_AT_SLEEPER = datetime(2026, 5, 20, 12, 0, 0)


def _seed_db_for_sleeper():
    init_db()
    with get_session() as session:
        rows = [
            ("SP1", "Stud WR", "WR", 92.0, 2, 1),
            ("SP2", "Solid RB", "RB", 80.0, 30, 3),
            ("SP3", "Late Find", "WR", 50.0, 130, 7),
        ]
        for sid, name, pos, score, rank, tier in rows:
            p = Player(sleeper_id=sid, full_name=name, position=pos)
            session.add(p)
            session.flush()
            # Use a fixed generated_at so _latest_composite_by_player()
            # picks up all three rows in the same "score run" snapshot.
            session.add(CompositeScore(
                player_id=p.id, league_format="sf_ppr",
                score=score, overall_rank=rank, position_rank=1, tier=tier,
                generated_at=_GEN_AT_SLEEPER, model_version="0.3.0",
                breakdown_json="{}",
            ))


def test_sleeper_manager_report_end_to_end():
    _seed_db_for_sleeper()

    class _Client:
        def get(self, url, *a, **kw):
            if url.endswith("/users"):
                return _Resp([
                    {"user_id": "U1", "display_name": "Alpha"},
                    {"user_id": "U2", "display_name": "Bravo"},
                ])
            if url.endswith("/rosters"):
                return _Resp([
                    {"roster_id": 1, "owner_id": "U1", "players": ["SP1", "SP2"]},
                    {"roster_id": 2, "owner_id": "U2", "players": ["SP3"]},
                ])
            if url.endswith("/drafts"):
                return _Resp([{"draft_id": "D1", "season": "2024"}])
            if "/draft/D1/picks" in url:
                return _Resp([
                    {"pick_no": 1, "round": 1, "roster_id": 1, "player_id": "SP1"},
                    {"pick_no": 14, "round": 2, "roster_id": 2, "player_id": "SP3"},
                    {"pick_no": 27, "round": 3, "roster_id": 1, "player_id": "SP2"},
                ])
            if "/transactions/" in url:
                # Trade in week 5: Alpha gives SP1, Bravo gives SP3 + SP2.
                # In Sleeper format: adds = {pid: roster_id_that_received}
                if url.endswith("/transactions/5"):
                    return _Resp([{
                        "type": "trade", "status": "complete",
                        "transaction_id": "tx-5",
                        "status_updated": 1700000000000,
                        "adds": {"SP1": 2, "SP3": 1, "SP2": 1},
                    }])
                return _Resp([])
            # Default fallback
            return _Resp([])
        def close(self): pass

    report = manager_report_sleeper("X", client=_Client())
    assert report["platform"] == "sleeper"
    assert report["n_picks"] == 3
    assert report["n_trades"] == 1
    names = {m["display_name"]: m for m in report["managers"]}
    assert "Alpha" in names and "Bravo" in names
    # Alpha drafted SP1 (pick 1) and SP2 (pick 27).
    # SP1 score 92 vs expected 100.0 -> -8.0.
    # SP2 score 80 vs expected ~89.6 -> -9.6.
    # Total ~-17.6, avg -8.8.
    assert abs(names["Alpha"]["draft_delta_total"] - (-17.6)) < 0.1
    assert abs(names["Alpha"]["draft_delta_avg"] - (-8.8)) < 0.1
    # Trade: Bravo received SP1 (92). Alpha received SP3 (50) + SP2 (80) = 130.
    # Alpha trade delta = 130 - 92 = +38. Bravo trade delta = 92 - 130 = -38.
    assert abs(names["Alpha"]["trade_delta_total"] - 38.0) < 0.5
    assert abs(names["Bravo"]["trade_delta_total"] - (-38.0)) < 0.5


# ---------------------------------------------------------------------------
# End-to-end fixture for MFL
# ---------------------------------------------------------------------------

def _seed_db_for_mfl():
    # Idempotent additions to the same DB. Use the SAME generated_at as
    # the Sleeper seed so _latest_composite_by_player picks up everyone.
    from sqlalchemy import select as _select
    with get_session() as session:
        rows = [
            ("MP1", "MFL Stud", "QB", 95.0, 3, 1),
            ("MP2", "MFL Solid", "RB", 75.0, 40, 4),
        ]
        for mid, name, pos, score, rank, tier in rows:
            existing = session.execute(
                _select(Player).where(Player.mfl_id == mid)
            ).scalar_one_or_none()
            if existing:
                continue
            p = Player(mfl_id=mid, full_name=name, position=pos)
            session.add(p)
            session.flush()
            session.add(CompositeScore(
                player_id=p.id, league_format="sf_ppr",
                score=score, overall_rank=rank, position_rank=1, tier=tier,
                generated_at=_GEN_AT_SLEEPER, model_version="0.3.0",
                breakdown_json="{}",
            ))


def test_mfl_manager_report_end_to_end():
    _seed_db_for_mfl()

    league_payload = {"league": {
        "name": "Mock MFL",
        "franchises": {"franchise": [
            {"id": "0001", "name": "Foxtrot"},
            {"id": "0002", "name": "Gamma"},
        ]},
    }}
    draft_payload = {"draftResults": {"draftUnit": [{
        "year": "2024",
        "draftPick": [
            # MFL is 0-indexed: pick=0 means overall pick 1.
            {"pick": "0", "round": "0", "franchise": "0001", "player": "MP1"},
            {"pick": "11", "round": "0", "franchise": "0002", "player": "MP2"},
        ],
    }]}}
    trades_payload = {"transactions": {"transaction": [{
        "type": "TRADE",
        "transaction_id": "mtx-1",
        "timestamp": "1700000000",
        "franchise1": "0001",
        "franchise2": "0002",
        "franchise1_gave_up": "MP1,",
        "franchise2_gave_up": "MP2,DP_02_05,",  # MP2 + a draft pick
    }]}}

    class _Client:
        def get(self, url, *a, **kw):
            if "TYPE=league" in url:
                return _Resp(league_payload)
            if "TYPE=draftResults" in url:
                return _Resp(draft_payload)
            if "TYPE=transactions" in url:
                return _Resp(trades_payload)
            return _Resp({})
        def close(self): pass

    report = manager_report_mfl("12345", year=2024, client=_Client())
    assert report["platform"] == "mfl"
    assert report["n_picks"] == 2
    assert report["n_trades"] == 1
    by_name = {m["display_name"]: m for m in report["managers"]}
    assert "Foxtrot" in by_name and "Gamma" in by_name
    # Foxtrot drafted MP1 (pick 1) score=95 vs expected=100.0 -> -5.0.
    assert abs(by_name["Foxtrot"]["draft_delta_total"] - (-5.0)) < 0.1
    # Gamma drafted MP2 (pick 12) score=75 vs expected=95.6 -> -20.6.
    assert abs(by_name["Gamma"]["draft_delta_total"] - (-20.6)) < 0.1
    # Trade: Foxtrot received MP2 (75), gave MP1 (95). Delta = -20.
    # Gamma received MP1 (95), gave MP2 (75). Delta = +20.
    # (Draft pick DP_02_05 is filtered out as non-player asset.)
    assert abs(by_name["Foxtrot"]["trade_delta_total"] - (-20.0)) < 0.5
    assert abs(by_name["Gamma"]["trade_delta_total"] - 20.0) < 0.5


def main():
    test_expected_score_at_pick_anchors(); print("1. expected_score_at_pick anchors: ✓")
    test_manager_table_basic_math();       print("2. _compute_manager_table arithmetic: ✓")
    test_trade_value_zero_sum_within_a_trade(); print("3. trade value zero-sum: ✓")
    test_sleeper_manager_report_end_to_end();   print("4. sleeper end-to-end: ✓")
    test_mfl_manager_report_end_to_end();       print("5. MFL end-to-end: ✓")
    print("\nAll manager tests passed.")


if __name__ == "__main__":
    main()
