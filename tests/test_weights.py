"""Tests for the position-/years-pro-aware weighting hooks.

Two layers:
  - unit tests on the weights module (pure functions)
  - integration test driving compute_composite_scores against an in-memory
    DB with synthetic sources to verify the weighting actually flows through
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
    POSITION_MODIFIERS,
    position_modifier,
    years_pro_modifier,
    select_track_record_multiplier,
    corr_to_multiplier,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_position_modifier_lookups():
    assert position_modifier("ras", "WR") == 1.5
    assert position_modifier("ras", "QB") == 0.3
    assert position_modifier("ras", "K") == 1.0    # unknown pos → neutral
    assert position_modifier("ras", None) == 1.0
    assert position_modifier("unknown", "WR") == 1.0  # unknown source → neutral
    # cfbd at WR
    assert position_modifier("cfbd_breakouts", "WR") == 1.5
    assert position_modifier("cfbd_breakouts", "QB") == 0.4
    # nfl_draft_capital at QB
    assert position_modifier("nfl_draft_capital", "QB") == 1.2


def test_years_pro_decay_for_rookie_signal_sources():
    # nfl_draft_capital decays linearly
    assert years_pro_modifier("nfl_draft_capital", 0) == 1.0
    assert years_pro_modifier("nfl_draft_capital", 1) == 0.8
    assert years_pro_modifier("nfl_draft_capital", 2) == 0.6
    assert years_pro_modifier("nfl_draft_capital", 5) == 0.3   # floor
    assert years_pro_modifier("nfl_draft_capital", None) == 1.0  # unknown vet → neutral


def test_years_pro_inverse_for_market_sources():
    assert years_pro_modifier("fantasycalc", 0) == 0.6
    assert years_pro_modifier("fantasycalc", 1) == 0.8
    assert years_pro_modifier("fantasycalc", 2) == 1.0
    assert years_pro_modifier("ffc_adp", 0) == 0.6
    assert years_pro_modifier("ffc_adp", 3) == 1.0


def test_years_pro_neutral_for_unknown_sources():
    assert years_pro_modifier("anything_else", 0) == 1.0
    assert years_pro_modifier("anything_else", 5) == 1.0


def test_select_track_record_multiplier_prefers_position():
    by_pos = {None: 1.0, "WR": 1.6}
    assert select_track_record_multiplier(by_pos, "WR") == 1.6
    assert select_track_record_multiplier(by_pos, "RB") == 1.0  # falls back to overall
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
# Integration test
# ---------------------------------------------------------------------------

def test_position_modifier_flows_through_compute_composite_scores():
    """Two rookies, identical other inputs, different positions.

    RAS contributes 1.5x weight at WR but 0.3x at QB. The WR's composite
    should therefore tilt MORE toward RAS than the QB's does.
    """
    init_db()
    current_year = datetime.utcnow().year

    with get_session() as session:
        # Two rookies. They both get the SAME ranking from each source.
        wr = Player(full_name="WR Rookie", position="WR", draft_year=current_year)
        qb = Player(full_name="QB Rookie", position="QB", draft_year=current_year)
        session.add_all([wr, qb])
        session.flush()

        # RAS source with default_weight=0.8
        ras = Source(slug="ras", name="RAS",
                     category="model", default_weight=0.8,
                     update_frequency="event", tos_compliant=True)
        # Market source with no position tilt
        fcalc = Source(slug="fantasycalc", name="FantasyCalc",
                       category="market", default_weight=1.0,
                       update_frequency="daily", tos_compliant=True)
        session.add_all([ras, fcalc])
        session.flush()

        # Both players: RAS = top of class (rank 1 → high score),
        # FantasyCalc = much lower (rank 200 → near-zero score).
        # No market_value set so the rank-based normalization is used —
        # otherwise per-source value-normalization would saturate both
        # players to 100 (their rankings are the only data in the source).
        for p in (wr, qb):
            session.add(Ranking(
                source_id=ras.id, player_id=p.id,
                overall_rank=1,
                league_format="sf_ppr", is_dynasty=True, is_rookie_only=True,
            ))
            session.add(Ranking(
                source_id=fcalc.id, player_id=p.id,
                overall_rank=200,
                league_format="sf_ppr", is_dynasty=True,
            ))

    n = compute_composite_scores(league_format="sf_ppr", score_year=current_year)
    assert n == 2

    import json as _json
    with get_session() as session:
        rows = session.query(CompositeScore, Player).join(Player).all()
        snapshot = {
            p.full_name: {
                "score": cs.score,
                "breakdown": _json.loads(cs.breakdown_json),
            }
            for cs, p in rows
        }

    wr = snapshot["WR Rookie"]
    qb = snapshot["QB Rookie"]

    # The same RAS rank+value should produce a HIGHER weighted contribution
    # for the WR than for the QB (1.5x vs 0.3x).
    assert wr["breakdown"]["ras"]["weight"] > qb["breakdown"]["ras"]["weight"], (
        f"expected WR RAS weight > QB RAS weight, got "
        f"WR={wr['breakdown']['ras']['weight']} QB={qb['breakdown']['ras']['weight']}"
    )
    # WR's composite should be higher than QB's because RAS pulls it up more.
    assert wr["score"] > qb["score"]

    # FantasyCalc (market) gets the trailing-rookie discount: 0.6x at year 0
    # for both. WR/QB FC weights should be equal.
    assert wr["breakdown"]["fantasycalc"]["weight"] == qb["breakdown"]["fantasycalc"]["weight"]


def test_position_specific_track_record_beats_overall():
    """If a track-record row exists for (source, WR) with a stronger correlation
    than the overall (None) row, WR players should get the higher multiplier
    while non-WR players still see the overall multiplier.

    Reuses the existing DB; only adds new rows. (Re-initing the engine
    against a deleted-and-recreated SQLite file mid-process is unreliable.)
    """
    current_year = datetime.utcnow().year

    with get_session() as session:
        wr = Player(full_name="WR Specialist", position="WR", draft_year=current_year - 4)
        rb = Player(full_name="RB Generalist", position="RB", draft_year=current_year - 4)
        session.add_all([wr, rb])
        session.flush()

        src = Source(slug="mystery_source", name="Mystery",
                     category="expert", default_weight=1.0,
                     update_frequency="daily", tos_compliant=True)
        session.add(src)
        session.flush()

        # Overall track record: weak (0.20 → 1.0x multiplier)
        session.add(SourceTrackRecord(
            source_id=src.id, position=None, cohort_year=None,
            sample_size=100, spearman_corr=0.20,
            outcome_window_years=3,
        ))
        # WR-specific track record: strong (0.40 → 1.6x multiplier)
        session.add(SourceTrackRecord(
            source_id=src.id, position="WR", cohort_year=None,
            sample_size=50, spearman_corr=0.40,
            outcome_window_years=3,
        ))
        session.flush()

        for p in (wr, rb):
            session.add(Ranking(
                source_id=src.id, player_id=p.id,
                overall_rank=1, league_format="sf_ppr", is_dynasty=True,
            ))

    # Score everyone in the DB (the first integration test's rookies are
    # still in there). We only care about the two new players' weights.
    compute_composite_scores(league_format="sf_ppr", score_year=current_year)

    import json as _json
    with get_session() as session:
        rows = session.query(CompositeScore, Player).join(Player).all()
        # Take the LATEST composite per player (the score function appends
        # rather than overwrites, so multiple integration runs can co-exist).
        latest_by_name: dict[str, str] = {}
        for cs, p in rows:
            existing = latest_by_name.get(p.full_name)
            if existing is None or cs.generated_at >= existing[0]:
                latest_by_name[p.full_name] = (cs.generated_at, _json.loads(cs.breakdown_json))
        breakdowns = {n: tup[1] for n, tup in latest_by_name.items()}

    assert "mystery_source" in breakdowns["WR Specialist"]
    assert "mystery_source" in breakdowns["RB Generalist"]
    wr_weight = breakdowns["WR Specialist"]["mystery_source"]["weight"]
    rb_weight = breakdowns["RB Generalist"]["mystery_source"]["weight"]

    # WR should get 1.6x (default_weight 1.0 * 1.6 * pos_mod_default 1.0 *
    # years_pro_decay 1.0 = 1.6). RB should get 1.0 (overall multiplier).
    # The exact values depend on years_pro_modifier; with years_pro=4 and
    # source "mystery_source" (not in either ROOKIE_SIGNAL or
    # TRAILING_FOR_ROOKIES set), years_pro_modifier returns 1.0.
    assert wr_weight > rb_weight, (
        f"position-specific track record should beat overall for WR: "
        f"wr_weight={wr_weight} rb_weight={rb_weight}"
    )


def main():
    test_position_modifier_lookups();              print("1. position_modifier lookups: ✓")
    test_years_pro_decay_for_rookie_signal_sources(); print("2. years_pro decay (rookie signals): ✓")
    test_years_pro_inverse_for_market_sources();   print("3. years_pro inverse (market sources): ✓")
    test_years_pro_neutral_for_unknown_sources();  print("4. years_pro neutral default: ✓")
    test_select_track_record_multiplier_prefers_position(); print("5. select_track_record prefers position: ✓")
    test_corr_to_multiplier_cutoffs();             print("6. corr_to_multiplier cutoffs: ✓")
    test_position_modifier_flows_through_compute_composite_scores()
    print("7. position modifier flows through scoring: ✓")
    test_position_specific_track_record_beats_overall()
    print("8. position-specific track record beats overall: ✓")
    print("\nAll weights tests passed.")


if __name__ == "__main__":
    main()
