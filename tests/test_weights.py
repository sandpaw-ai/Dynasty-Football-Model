"""Tests for the v0.10 deterministic weighting model.

Phil's requirement (2026-05-20):
  - Weights are per-source, not per-player.
  - The only allowed per-player variation is when a backtest produced a
    position-specific SourceTrackRecord row.
  - Hand-coded position modifiers (RAS=1.5x at WR) and years-pro decay
    are gone.
"""
import os
import sys
from datetime import datetime

os.environ["DATABASE_URL"] = "sqlite:///./test_weights.db"
if os.path.exists("./test_weights.db"):
    os.remove("./test_weights.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.db.session import init_db, get_session
from dynasty.db.models import Source, Player, Ranking, CompositeScore, SourceTrackRecord
from dynasty.scoring import compute_composite_scores
from dynasty.weights import (
    ROOKIE_SIGNAL_SOURCES,
    select_track_record_multiplier,
    corr_to_multiplier,
)


# ---------------------------------------------------------------------------
# Unit tests on what's left of weights.py
# ---------------------------------------------------------------------------

def test_rookie_signal_sources_set():
    # These should be flagged as rookie-signal sources (used by scoring.py to
    # filter out retired/no-roster players whose ONLY rankings come from them).
    assert "nfl_draft_capital" in ROOKIE_SIGNAL_SOURCES
    assert "ras" in ROOKIE_SIGNAL_SOURCES
    assert "cfbd_breakouts" in ROOKIE_SIGNAL_SOURCES


def test_select_track_record_multiplier_prefers_position():
    by_pos = {None: 1.0, "WR": 1.6}
    assert select_track_record_multiplier(by_pos, "WR") == 1.6
    assert select_track_record_multiplier(by_pos, "RB") == 1.0
    assert select_track_record_multiplier(by_pos, None) == 1.0
    assert select_track_record_multiplier({}, "WR") == 1.0


def test_corr_to_multiplier_cutoffs():
    assert corr_to_multiplier(None) == 1.0
    assert corr_to_multiplier(0.05) == 0.5
    assert corr_to_multiplier(0.20) == 1.0
    assert corr_to_multiplier(0.30) == 1.3
    assert corr_to_multiplier(0.40) == 1.6
    # negative correlations are absolute-valued
    assert corr_to_multiplier(-0.40) == 1.6


# ---------------------------------------------------------------------------
# Integration tests — weights are consistent across players
# ---------------------------------------------------------------------------

def test_same_source_same_weight_for_two_players_of_same_position():
    """Two WR rookies, same source, same rank. Their weighted contribution
    from that source must be identical (no per-player adjustment).
    """
    init_db()
    current_year = datetime.utcnow().year

    with get_session() as session:
        wr1 = Player(full_name="WR Alpha", position="WR", draft_year=current_year)
        wr2 = Player(full_name="WR Bravo", position="WR", draft_year=current_year)
        # And one veteran WR — same source weight should still apply.
        wr_vet = Player(full_name="WR Vet", position="WR", draft_year=current_year - 7)
        session.add_all([wr1, wr2, wr_vet])
        session.flush()

        ras = Source(
            slug="ras", name="RAS",
            category="model", default_weight=0.8,
            update_frequency="event", tos_compliant=True,
        )
        session.add(ras)
        session.flush()

        for p in (wr1, wr2, wr_vet):
            session.add(Ranking(
                source_id=ras.id, player_id=p.id,
                overall_rank=1,
                league_format="sf_ppr", is_dynasty=True,
            ))

        # Add a corroborating source so the rookie-signal-only filter doesn't
        # drop these players.
        market = Source(
            slug="fantasycalc", name="FantasyCalc",
            category="market", default_weight=1.0,
            update_frequency="daily", tos_compliant=True,
        )
        session.add(market)
        session.flush()
        for p in (wr1, wr2, wr_vet):
            session.add(Ranking(
                source_id=market.id, player_id=p.id,
                overall_rank=10,
                league_format="sf_ppr", is_dynasty=True,
            ))

    compute_composite_scores(league_format="sf_ppr", score_year=current_year)

    import json as _json
    with get_session() as session:
        rows = session.query(CompositeScore, Player).join(Player).all()
        snapshot = {p.full_name: _json.loads(cs.breakdown_json) for cs, p in rows}

    ras_weights = {name: snapshot[name]["ras"]["weight"] for name in
                   ("WR Alpha", "WR Bravo", "WR Vet")}

    # All three WR rows must have the SAME RAS weight regardless of years-pro.
    assert len(set(ras_weights.values())) == 1, (
        f"expected identical RAS weight for all three WRs, got {ras_weights}"
    )

    fc_weights = {name: snapshot[name]["fantasycalc"]["weight"] for name in
                  ("WR Alpha", "WR Bravo", "WR Vet")}
    assert len(set(fc_weights.values())) == 1, (
        f"expected identical FantasyCalc weight (no rookie-decay), got {fc_weights}"
    )


def test_position_specific_track_record_overrides_overall():
    """If a track-record row exists for (source, WR) with a stronger
    correlation than the overall row, WR players see the WR multiplier;
    other positions see the overall multiplier.

    Reuses the existing DB (fantasycalc Source from the first test is
    still around; we add a NEW Source for this test).
    """
    from sqlalchemy import select as _select
    current_year = datetime.utcnow().year

    with get_session() as session:
        wr = Player(full_name="WR Specialist", position="WR", draft_year=current_year - 2)
        rb = Player(full_name="RB Generalist", position="RB", draft_year=current_year - 2)
        session.add_all([wr, rb])
        session.flush()

        src = Source(
            slug="mystery_source", name="Mystery",
            category="expert", default_weight=1.0,
            update_frequency="daily", tos_compliant=True,
        )
        session.add(src)
        session.flush()

        # Overall: weak (0.20 -> 1.0x)
        session.add(SourceTrackRecord(
            source_id=src.id, position=None, cohort_year=None,
            sample_size=100, spearman_corr=0.20,
            outcome_window_years=3,
        ))
        # WR-specific: strong (0.40 -> 1.6x)
        session.add(SourceTrackRecord(
            source_id=src.id, position="WR", cohort_year=None,
            sample_size=50, spearman_corr=0.40,
            outcome_window_years=3,
        ))
        session.flush()

        # Reuse the fantasycalc Source from the prior test (or create it).
        mkt = session.execute(
            _select(Source).where(Source.slug == "fantasycalc")
        ).scalar_one_or_none()
        if mkt is None:
            mkt = Source(
                slug="fantasycalc", name="FantasyCalc",
                category="market", default_weight=1.0,
                update_frequency="daily", tos_compliant=True,
            )
            session.add(mkt)
            session.flush()

        for p in (wr, rb):
            session.add(Ranking(
                source_id=src.id, player_id=p.id, overall_rank=1,
                league_format="sf_ppr", is_dynasty=True,
            ))
            session.add(Ranking(
                source_id=mkt.id, player_id=p.id, overall_rank=20,
                league_format="sf_ppr", is_dynasty=True,
            ))

    compute_composite_scores(league_format="sf_ppr", score_year=current_year)

    import json as _json
    with get_session() as session:
        rows = session.query(CompositeScore, Player).join(Player).all()
        latest = {}
        for cs, p in rows:
            existing = latest.get(p.full_name)
            if existing is None or cs.generated_at >= existing[0]:
                latest[p.full_name] = (cs.generated_at, _json.loads(cs.breakdown_json))
        breakdowns = {n: tup[1] for n, tup in latest.items()}

    wr_weight = breakdowns["WR Specialist"]["mystery_source"]["weight"]
    rb_weight = breakdowns["RB Generalist"]["mystery_source"]["weight"]
    assert wr_weight > rb_weight, (
        f"position-specific track record should beat overall for WR: "
        f"wr_weight={wr_weight} rb_weight={rb_weight}"
    )
    # Specifically: 1.0 * 1.6 = 1.6 for WR, 1.0 * 1.0 = 1.0 for RB.
    assert wr_weight == 1.6
    assert rb_weight == 1.0


def main():
    test_rookie_signal_sources_set();                    print("1. ROOKIE_SIGNAL_SOURCES set: ✓")
    test_select_track_record_multiplier_prefers_position(); print("2. position-specific track record selector: ✓")
    test_corr_to_multiplier_cutoffs();                   print("3. corr_to_multiplier cutoffs: ✓")
    test_same_source_same_weight_for_two_players_of_same_position()
    print("4. same source -> same weight across players: ✓")
    test_position_specific_track_record_overrides_overall()
    print("5. position-specific track record overrides overall: ✓")
    print("\nAll weights tests passed.")


if __name__ == "__main__":
    main()
