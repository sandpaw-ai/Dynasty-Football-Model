"""Unit tests for the FantasyFootballCalculator ADP adapter.

Uses an in-memory fake HTTP client (no network).
"""
import json
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///./test_ffc.db"
if os.path.exists("./test_ffc.db"):
    os.remove("./test_ffc.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.sources import REGISTRY, FFCAdp
from dynasty.sources.ffc_adp import FFCAdp as Cls


PAYLOAD_PPR = {
    "status": "OK",
    "meta": {"type": "PPR", "teams": 12, "rounds": 15, "total_drafts": 50},
    "players": [
        {"player_id": 1, "name": "Ja'Marr Chase",  "position": "WR", "team": "CIN", "adp": 1.4},
        {"player_id": 2, "name": "Bijan Robinson", "position": "RB", "team": "ATL", "adp": 2.1},
        {"player_id": 3, "name": "Justin Tucker",  "position": "K",  "team": "BAL", "adp": 180.0},
        {"player_id": 4, "name": "",               "position": "WR", "team": "FA",  "adp": 999.0},
    ],
}
PAYLOAD_ROOKIE = {
    "status": "OK",
    "meta": {"type": "Rookie", "teams": 12},
    "players": [
        {"player_id": 5670, "name": "Bijan Robinson", "position": "RB", "team": "ATL", "adp": 1.3},
    ],
}


class _Resp:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if url.endswith("/rookie"):
            return _Resp(PAYLOAD_ROOKIE)
        if url.endswith("/ppr"):
            return _Resp(PAYLOAD_PPR)
        # 2qb, dynasty -> empty payload to keep the test focused
        return _Resp({"players": []})

    def close(self): pass


def test_registry():
    assert "ffc_adp" in REGISTRY
    assert REGISTRY["ffc_adp"] is FFCAdp


def test_fetch_filters_and_labels():
    src = Cls(client=_Client(), year=2026)
    records = list(src.fetch())

    by_fmt = {}
    for r in records:
        by_fmt.setdefault((r.league_format, r.is_rookie_only), []).append(r)

    ppr_records = by_fmt.get(("1qb_ppr", False), [])
    assert len(ppr_records) == 2, f"expected 2 skill-pos rows in PPR, got {len(ppr_records)}"
    names = {r.full_name for r in ppr_records}
    assert names == {"Ja'Marr Chase", "Bijan Robinson"}
    # K filtered out, blank-name row filtered out

    chase = next(r for r in ppr_records if r.full_name == "Ja'Marr Chase")
    assert chase.overall_rank == 1
    assert chase.position == "WR"
    assert chase.nfl_team == "CIN"
    assert chase.market_value is not None and chase.market_value > 0
    assert chase.is_dynasty is False

    rookie_records = by_fmt.get(("sf_ppr", True), [])
    assert len(rookie_records) == 1
    assert rookie_records[0].full_name == "Bijan Robinson"
    assert rookie_records[0].is_dynasty is True


def test_does_not_raise_on_404():
    """If FFC 404s a format (common pre-season for niche flavors), skip it."""

    class _ErrClient:
        def get(self, *a, **k):
            class _R:
                def raise_for_status(self): raise RuntimeError("404")
                def json(self): return {}
            return _R()
        def close(self): pass

    src = Cls(client=_ErrClient(), year=2026)
    records = list(src.fetch())  # must not raise
    assert records == []


def main():
    test_registry();              print("1. registry: ✓")
    test_fetch_filters_and_labels(); print("2. parse + filter + label: ✓")
    test_does_not_raise_on_404();    print("3. graceful 404 handling: ✓")
    print("\nAll FFC ADP tests passed.")


if __name__ == "__main__":
    main()
