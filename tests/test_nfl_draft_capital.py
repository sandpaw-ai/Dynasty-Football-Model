"""Unit tests for the NFL Draft Capital adapter.

Uses an in-memory fixture CSV (no network) to verify:
  - skill-position filter
  - field mapping (gsis_id, pfr_id, college, draft_round, draft_pick_overall)
  - overall_rank emission window (recent classes only)
  - registry registration
  - Player enrichment end-to-end via sync
"""
import io
import os
import sys
from datetime import datetime
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///./test_nfl_draft.db"
if os.path.exists("./test_nfl_draft.db"):
    os.remove("./test_nfl_draft.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Player, Ranking, Source
from dynasty.sources import REGISTRY, NFLDraftCapital
from dynasty.sync import sync_source


# Header + 6 rows. Mix of recent skill positions, a non-skill position (LB), and
# an old draft year that should enrich the Player row but not emit a ranking.
FIXTURE_CSV = """season,round,pick,team,gsis_id,pfr_player_id,cfb_player_id,pfr_player_name,hof,position,category,side,college,age
2025,1,1,LVR,00-9999001,SmiJo01,john-smith-1,John Smith,FALSE,QB,QB,O,Alabama,22
2025,1,5,ARI,00-9999002,JoBR01,bo-jones-1,Bo Jones,FALSE,RB,RB,O,Texas,21
2025,2,40,NYJ,00-9999003,WeWr01,wes-wright-1,Wes Wright,FALSE,WR,WR,O,Ohio State,22
2025,3,80,DAL,00-9999004,TaTi01,ty-tate-1,Ty Tate,FALSE,LB,LB,D,Georgia,23
2024,1,3,WAS,00-9999005,JaJe01,jay-jenkins-1,Jay Jenkins,FALSE,WR,WR,O,LSU,21
2010,2,55,KAN,00-9999006,VeVi01,vince-vintage-1,Vince Vintage,FALSE,RB,RB,O,USC,23
"""


class _FakeResp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


class _FakeClient:
    def __init__(self, text): self._text = text
    def get(self, url, *a, **kw): return _FakeResp(self._text)
    def close(self): pass


def test_registry_includes_source():
    assert "nfl_draft_capital" in REGISTRY
    assert REGISTRY["nfl_draft_capital"] is NFLDraftCapital


def test_parse_and_filter():
    src = NFLDraftCapital(client=_FakeClient(FIXTURE_CSV), emit_years_back=3)
    records = list(src.fetch())

    # LB row filtered out; vintage 2010 row included (enrichment) but with no
    # overall_rank because it's outside the emit window.
    by_name = {r.full_name: r for r in records}
    assert "Tate" not in by_name and "Ty Tate" not in by_name, "LB should be filtered"
    assert set(by_name) == {"John Smith", "Bo Jones", "Wes Wright", "Jay Jenkins", "Vince Vintage"}

    js = by_name["John Smith"]
    assert js.position == "QB"
    assert js.gsis_id == "00-9999001"
    assert js.pfr_id == "SmiJo01"
    assert js.college == "Alabama"
    assert js.draft_year == 2025
    assert js.draft_round == 1
    assert js.draft_pick_overall == 1
    assert js.draft_team == "LVR"
    assert js.nfl_team == "LVR"
    assert js.overall_rank == 1, "recent #1 pick should emit rank=1"

    # 2024 rookie is within emit window (current year - 3 ~ recent enough)
    jj = by_name["Jay Jenkins"]
    assert jj.overall_rank == 3

    # Vintage is enrichment-only
    vv = by_name["Vince Vintage"]
    assert vv.draft_year == 2010
    assert vv.draft_pick_overall == 55
    assert vv.overall_rank is None, "old draft should NOT emit a ranking"


def test_end_to_end_enriches_player_via_sync():
    init_db()

    # Pre-create a Player with a partial Sleeper-style record. The adapter
    # should resolve by gsis_id and enrich draft fields.
    with get_session() as session:
        session.add(Player(
            sleeper_id="11111",
            gsis_id="00-9999001",
            full_name="John Smith",
            position="QB",
        ))

    # Patch the adapter to use our fixture client.
    real_init = NFLDraftCapital.__init__

    def _patched_init(self, *a, **kw):
        kw.setdefault("client", _FakeClient(FIXTURE_CSV))
        real_init(self, *a, **kw)

    with patch.object(NFLDraftCapital, "__init__", _patched_init):
        n = sync_source("nfl_draft_capital")

    assert n >= 1, f"expected at least 1 ranking row, got {n}"

    with get_session() as session:
        p = session.query(Player).filter_by(gsis_id="00-9999001").one()
        assert p.draft_year == 2025
        assert p.draft_round == 1
        assert p.draft_pick_overall == 1
        assert p.draft_team == "LVR"
        assert p.college == "Alabama"
        assert p.pfr_id == "SmiJo01"
        assert p.sleeper_id == "11111", "existing sleeper_id must be preserved"

        # And there should be a Ranking row pointing at the source with rank=1.
        src = session.query(Source).filter_by(slug="nfl_draft_capital").one()
        ranking = (
            session.query(Ranking)
            .filter_by(source_id=src.id, player_id=p.id)
            .one()
        )
        assert ranking.overall_rank == 1
        assert ranking.league_format == "sf_ppr"
        assert ranking.is_dynasty is True


def main():
    test_registry_includes_source()
    print("1. registry: ✓")

    test_parse_and_filter()
    print("2. parse + filter + enrichment fields: ✓")

    test_end_to_end_enriches_player_via_sync()
    print("3. end-to-end sync + Player enrichment: ✓")

    print("\nAll NFL Draft Capital tests passed.")


if __name__ == "__main__":
    main()
