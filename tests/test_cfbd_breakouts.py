"""Unit tests for the CFBD Breakouts (Breakout Age + Dominator) adapter."""
import os
import sys
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///./test_cfbd.db"
if os.path.exists("./test_cfbd.db"):
    os.remove("./test_cfbd.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Player, Ranking, Source
from dynasty.sources import REGISTRY, CFBDBreakouts
from dynasty.sources.cfbd_breakouts import composite_college_score
from dynasty.sync import sync_source


def test_registry():
    assert "cfbd_breakouts" in REGISTRY
    assert REGISTRY["cfbd_breakouts"] is CFBDBreakouts


def test_composite_score_extremes():
    # Best possible: early breakout (age 18) + max dominator
    best = composite_college_score(18.0, 1.0)
    assert 0.99 <= best <= 1.0

    # Worst possible: late breakout + zero dominator
    worst = composite_college_score(23.0, 0.0)
    assert 0.0 <= worst <= 0.01

    # Mid: breakout at 20 (= 0.6 ba_score), dominator 0.5
    mid = composite_college_score(20.0, 0.5)
    assert 0.5 <= mid <= 0.65

    # Both None -> None
    assert composite_college_score(None, None) is None

    # One value: returns sensible blend with neutral other side
    only_ba = composite_college_score(18.0, None)
    assert 0.55 <= only_ba <= 0.85
    only_dr = composite_college_score(None, 0.8)
    assert 0.55 <= only_dr <= 0.65


def _write_csv(tmpdir: str) -> str:
    path = os.path.join(tmpdir, "breakouts.csv")
    csv_text = (
        "name,pos,college,draft_year,breakout_age,best_dominator\n"
        "Early Bloom,WR,Alabama,2025,18,0.55\n"      # elite breakout + elite dom
        "Avg WR,WR,LSU,2025,20,0.30\n"
        "Late Bloom,WR,Texas,2025,22,0.20\n"
        "Top RB,RB,Georgia,2025,19,0.45\n"
        "Old WR,WR,Miami,2010,18,0.60\n"             # outside emit window
        "Junk,K,Florida,2025,18,0.60\n"              # filtered (K)
        "Missing,WR,Ohio State,2025,,\n"             # filtered (no features)
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(csv_text)
    return path


def test_parse_and_rank():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp)
        src = CFBDBreakouts(csv_path=csv_path, emit_years_back=3)
        records = list(src.fetch())

    by_name = {r.full_name: r for r in records}

    assert "Junk" not in by_name
    assert "Missing" not in by_name
    assert set(by_name) == {"Early Bloom", "Avg WR", "Late Bloom", "Top RB", "Old WR"}

    # 2025 WR ranking by composite (Early > Avg > Late)
    assert by_name["Early Bloom"].overall_rank == 1
    assert by_name["Avg WR"].overall_rank == 2
    assert by_name["Late Bloom"].overall_rank == 3

    # 2025 RB independent universe
    assert by_name["Top RB"].overall_rank == 1

    # Outside emit window
    assert by_name["Old WR"].overall_rank is None
    assert by_name["Old WR"].market_value is None

    # market_value is composite_score * 100; (18, 0.55) -> 0.6*1 + 0.4*0.55 = 82
    assert 80 <= by_name["Early Bloom"].market_value <= 84
    # Late breakout + low dominator -> low score
    assert by_name["Late Bloom"].market_value < 40


def test_missing_csv_returns_empty():
    src = CFBDBreakouts(csv_path="/no/such/file.csv")
    assert list(src.fetch()) == []


def test_end_to_end_sync():
    init_db()
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp)
        with get_session() as session:
            session.add(Player(
                sleeper_id="cfb-1",
                full_name="Early Bloom",
                position="WR",
            ))

        os.environ["DYNASTY_CFBD_CSV_PATH"] = csv_path
        try:
            n = sync_source("cfbd_breakouts")
        finally:
            del os.environ["DYNASTY_CFBD_CSV_PATH"]

    assert n >= 4

    with get_session() as session:
        p = session.query(Player).filter_by(full_name="Early Bloom").one()
        assert p.draft_year == 2025
        assert p.college == "Alabama"
        # Existing sleeper_id preserved
        assert p.sleeper_id == "cfb-1"

        src = session.query(Source).filter_by(slug="cfbd_breakouts").one()
        r = (
            session.query(Ranking)
            .filter_by(source_id=src.id, player_id=p.id)
            .one()
        )
        assert r.overall_rank == 1
        assert r.is_rookie_only is True


def main():
    test_registry();                  print("1. registry: ✓")
    test_composite_score_extremes();  print("2. composite_college_score math: ✓")
    test_parse_and_rank();            print("3. parse + per-year/pos ranking: ✓")
    test_missing_csv_returns_empty(); print("4. missing CSV yields nothing: ✓")
    test_end_to_end_sync();           print("5. end-to-end sync + enrichment: ✓")
    print("\nAll CFBD Breakouts tests passed.")


if __name__ == "__main__":
    main()
