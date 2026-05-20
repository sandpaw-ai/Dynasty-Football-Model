"""Smoke test — verifies schema, sync, scoring, backtesting all work end-to-end.

Run with: python tests/smoke_test.py
No network calls — uses in-memory test sources.
"""
import os
import sys
from datetime import datetime

os.environ["DATABASE_URL"] = "sqlite:///./test_dynasty.db"
if os.path.exists("./test_dynasty.db"):
    os.remove("./test_dynasty.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Source, Player, Ranking, Production, CompositeScore
from dynasty.sources.base import BaseSource, RankingRecord
from dynasty.sources import REGISTRY
from dynasty.sync import sync_source
from dynasty.scoring import compute_composite_scores
from dynasty.backtest import backtest_source


# 6 synthetic players, indexed by sleeper_id
PLAYERS = [
    ("100", "Player A", "WR", 250),  # name, position, true 2022 PPR pts
    ("200", "Player B", "RB", 400),
    ("300", "Player C", "WR", 50),
    ("400", "Player D", "TE", 180),
    ("500", "Player E", "QB", 320),
    ("600", "Player F", "WR", 120),
]


class _FakeMarket(BaseSource):
    slug = "fake_market"
    name = "Fake market source"
    category = "market"
    default_weight = 1.0
    homepage = "http://example.com"

    def fetch(self):
        # Market ordering: B, A, E, D, F, C
        order = ["200", "100", "500", "400", "600", "300"]
        for rank, sid in enumerate(order, start=1):
            sid_, name, pos, _ = next(p for p in PLAYERS if p[0] == sid)
            yield RankingRecord(source_slug=self.slug, sleeper_id=sid_,
                                full_name=name, position=pos,
                                overall_rank=rank, market_value=10000 - rank * 800)


class _FakeExpert(BaseSource):
    slug = "fake_expert"
    name = "Fake expert source"
    category = "expert"
    default_weight = 1.0
    homepage = "http://example.com"

    def fetch(self):
        # Expert ordering — slightly different
        order = ["200", "500", "100", "300", "400", "600"]
        for rank, sid in enumerate(order, start=1):
            sid_, name, pos, _ = next(p for p in PLAYERS if p[0] == sid)
            yield RankingRecord(source_slug=self.slug, sleeper_id=sid_,
                                full_name=name, position=pos, overall_rank=rank)


REGISTRY["fake_market"] = _FakeMarket
REGISTRY["fake_expert"] = _FakeExpert


def main():
    print("1. init_db..."); init_db()

    print("2. sync fake_market...")
    assert sync_source("fake_market") == 6

    print("3. sync fake_expert...")
    assert sync_source("fake_expert") == 6

    print("4. compute composite scores...")
    n = compute_composite_scores(league_format="sf_ppr")
    assert n == 6, f"expected 6, got {n}"

    print("5. composite top:")
    with get_session() as session:
        scores = session.query(CompositeScore, Player).join(Player).order_by(CompositeScore.overall_rank).all()
        for cs, p in scores:
            print(f"   #{cs.overall_rank}  {p.full_name:10}  pos={p.position}  score={cs.score:.2f}  tier={cs.tier}")

    print("\n6. backtest with synthetic production...")
    with get_session() as session:
        # mark all as 2022 rookies
        for p in session.query(Player).all():
            p.draft_year = 2022
        # season totals for 2022 + 2023 + 2024 (window=3)
        for sid, _, _, pts2022 in PLAYERS:
            player = session.query(Player).filter_by(sleeper_id=sid).one()
            for season, mult in [(2022, 1.0), (2023, 1.1), (2024, 0.9)]:
                session.add(Production(
                    player_id=player.id, season=season, week=None,
                    fantasy_points_ppr=pts2022 * mult,
                ))
        # Add pre-draft rankings for fake_expert (captured_at < April 2022)
        expert = session.query(Source).filter_by(slug="fake_expert").one()
        order = ["200", "500", "100", "300", "400", "600"]
        for rank, sid in enumerate(order, start=1):
            player = session.query(Player).filter_by(sleeper_id=sid).one()
            session.add(Ranking(
                source_id=expert.id, player_id=player.id, overall_rank=rank,
                league_format="sf_ppr", is_dynasty=True,
                captured_at=datetime(2022, 3, 15),
            ))

    result = backtest_source("fake_expert", [2022], window_years=3)
    assert result is not None, "backtest returned None"
    print("   Backtest result:")
    for k, v in result.items():
        print(f"     {k}: {v}")
    assert result["spearman_corr"] is not None
    # fake_expert ordering loosely matches actual production → strong negative corr
    assert result["spearman_corr"] < -0.3, f"expected strong neg corr, got {result['spearman_corr']}"

    print("\n7. re-score after backtest — weight multiplier should now apply")
    n = compute_composite_scores(league_format="sf_ppr")
    assert n == 6
    with get_session() as session:
        latest = session.query(CompositeScore).order_by(CompositeScore.generated_at.desc()).first()
        print(f"   Latest model_version={latest.model_version}, generated_at={latest.generated_at}")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
