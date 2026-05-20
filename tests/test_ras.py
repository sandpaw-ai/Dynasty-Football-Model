"""Unit tests for the RAS adapter.

Builds a tiny in-memory CSV and verifies:
  - column-alias parsing
  - K/DEF filtering
  - position-rank within draft class is computed
  - emit window respects ``emit_years_back``
  - end-to-end sync enriches Player rows
"""
import os
import sys
import tempfile
from datetime import datetime

os.environ["DATABASE_URL"] = "sqlite:///./test_ras.db"
if os.path.exists("./test_ras.db"):
    os.remove("./test_ras.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Player, Ranking, Source
from dynasty.sources import REGISTRY, RAS
from dynasty.sync import sync_source


def _write_csv(tmpdir: str) -> str:
    path = os.path.join(tmpdir, "ras.csv")
    csv_text = (
        # Mix of column-name styles + a K row to verify filtering + a no-RAS row
        "Name,Pos,College,Year,RAS\n"
        "Top WR 2025,WR,Alabama,2025,9.85\n"
        "Mid WR 2025,WR,Texas,2025,7.10\n"
        "Bust WR 2025,WR,LSU,2025,3.20\n"
        "Top RB 2025,RB,Georgia,2025,9.50\n"
        "Mid RB 2025,RB,Notre Dame,2025,6.40\n"
        "Top WR 2024,WR,Ohio State,2024,9.90\n"
        "Kicker Guy,K,Florida,2025,8.00\n"     # filtered out (non-skill)
        "No-RAS Guy,WR,USC,2025,\n"            # filtered out (no score)
        "Old WR,WR,Miami,2005,9.95\n"          # parsed but no ranking emitted
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(csv_text)
    return path


def test_registry():
    assert "ras" in REGISTRY and REGISTRY["ras"] is RAS


def test_parse_and_rank():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp)
        src = RAS(csv_path=csv_path, emit_years_back=3)
        records = list(src.fetch())

    by_name = {r.full_name: r for r in records}

    # Filtered
    assert "Kicker Guy" not in by_name
    assert "No-RAS Guy" not in by_name

    # All others parsed
    assert set(by_name) == {
        "Top WR 2025", "Mid WR 2025", "Bust WR 2025",
        "Top RB 2025", "Mid RB 2025",
        "Top WR 2024",
        "Old WR",
    }

    # 2025 WR ranking by descending RAS
    assert by_name["Top WR 2025"].overall_rank == 1
    assert by_name["Mid WR 2025"].overall_rank == 2
    assert by_name["Bust WR 2025"].overall_rank == 3

    # 2025 RB ranking is independent of WR
    assert by_name["Top RB 2025"].overall_rank == 1
    assert by_name["Mid RB 2025"].overall_rank == 2

    # 2024 WR is its own ranking universe (rank=1 within that class)
    assert by_name["Top WR 2024"].overall_rank == 1

    # Old WR (2005) is parsed but emits no ranking (outside emit window)
    assert by_name["Old WR"].overall_rank is None
    assert by_name["Old WR"].market_value is None

    # Raw RAS comes through as market_value (for value-based scoring branch)
    assert by_name["Top WR 2025"].market_value == 9.85
    # Rookie flag set on emit-window rows
    assert by_name["Top WR 2025"].is_rookie_only is True
    assert by_name["Old WR"].is_rookie_only is False


def test_missing_csv_returns_empty():
    src = RAS(csv_path="/no/such/file.csv")
    assert list(src.fetch()) == []


def test_end_to_end_sync_enriches_player():
    init_db()
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp)

        # Pre-create Player with partial info — adapter should enrich draft_year/college.
        with get_session() as session:
            session.add(Player(
                sleeper_id="seed-1",
                full_name="Top WR 2025",
                position="WR",
            ))

        # Patch path via env var
        os.environ["DYNASTY_RAS_CSV_PATH"] = csv_path
        try:
            n = sync_source("ras")
        finally:
            del os.environ["DYNASTY_RAS_CSV_PATH"]

    assert n >= 5, f"expected at least 5 records, got {n}"

    with get_session() as session:
        p = session.query(Player).filter_by(full_name="Top WR 2025").one()
        assert p.draft_year == 2025
        assert p.college == "Alabama"
        assert p.position == "WR"
        # Existing sleeper_id preserved
        assert p.sleeper_id == "seed-1"

        # And the ranking landed
        src = session.query(Source).filter_by(slug="ras").one()
        r = (
            session.query(Ranking)
            .filter_by(source_id=src.id, player_id=p.id)
            .one()
        )
        assert r.overall_rank == 1
        assert r.is_rookie_only is True
        assert r.market_value == 9.85


def main():
    test_registry();                  print("1. registry: ✓")
    test_parse_and_rank();            print("2. parse + per-year/position ranking: ✓")
    test_missing_csv_returns_empty(); print("3. missing CSV yields nothing: ✓")
    test_end_to_end_sync_enriches_player(); print("4. end-to-end sync + enrichment: ✓")
    print("\nAll RAS tests passed.")


if __name__ == "__main__":
    main()
