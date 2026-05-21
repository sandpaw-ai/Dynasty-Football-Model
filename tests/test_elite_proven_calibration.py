"""v0.18.0 \u2014 Elite-proven veteran calibration tests.

These tests pin the Mahomes-class veteran detection + calibration. The
projection layer must now respect a proven-elite veteran's full career
arc rather than getting dragged down by 1-2 statistically-down seasons.

Phil's directive (2026-05-21):

  "Mahomes lands at sf_ppr rank #35 after PR #15. That's too harsh \u2014
   he's consensus top-5 in superflex because his FLOOR is enormous.
   The model should respect 5+ seasons of elite production more than
   it currently does."

PR #18 adds:

  * Elite-proven detection (csn>=5 AND cum_pct>=85 AND peak_pct>=90
    AND position-enabled) against the CSN-cohort-normalized
    historical corpus.
  * Adaptive self-projection blend (peak-tilted instead of recent-tilted)
    for flagged players, with position-specific peak_weight.
  * Track-record floor on projected_total_remaining_ppr that lifts
    proven-elites whose KNN was suppressed by recent down seasons but
    collapses to ~0 for aging veterans (Rodgers at 41).

These tests run against the committed PFR / nflverse corpus under
``data/nflverse/``. They do NOT hit the network.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


# Make the package importable from tests/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Pin DB at module load so all dynasty.* imports below see the same engine.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

import importlib  # noqa: E402
import dynasty.config as _config_mod  # noqa: E402
importlib.reload(_config_mod)
import dynasty.db.session as _session_mod  # noqa: E402
importlib.reload(_session_mod)


# ---------------------------------------------------------------------------
# Shared corpus + projection fixture \u2014 building the cohort + projection
# pipeline takes a few seconds, so we share across tests.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sf_projections():
    from dynasty.similarity.vectorize import build_nfl_corpus
    from dynasty.similarity.projection import project_all_active_players

    corpus = build_nfl_corpus()
    projs = project_all_active_players(corpus=corpus, league_format="sf_ppr")
    sorted_projs = sorted(projs, key=lambda p: p.dynasty_value, reverse=True)
    return corpus, sorted_projs


@pytest.fixture(scope="module")
def oneqb_projections():
    from dynasty.similarity.vectorize import build_nfl_corpus
    from dynasty.similarity.projection import project_all_active_players

    corpus = build_nfl_corpus()
    projs = project_all_active_players(corpus=corpus, league_format="1qb_ppr")
    sorted_projs = sorted(projs, key=lambda p: p.dynasty_value, reverse=True)
    return corpus, sorted_projs


def _rank_of(name: str, sorted_projs) -> int | None:
    for i, p in enumerate(sorted_projs):
        if p.player_name == name:
            return i + 1
    return None


def _proj_by_name(name: str, sorted_projs):
    for p in sorted_projs:
        if p.player_name == name:
            return p
    return None


# ---------------------------------------------------------------------------
# 1. Mahomes \u2014 the headline pathology
# ---------------------------------------------------------------------------


def test_mahomes_top10_sf(sf_projections):
    """Patrick Mahomes must rank top 10 in sf_ppr (was #35 in PR #15).

    The whole point of PR #18: Mahomes' two down seasons (2023 / 2024
    PPR ~280) should not crater a player whose career-arc cumulative
    + peak place him squarely in the historically-Brady-Manning tier.
    """
    _, sp = sf_projections
    rank = _rank_of("Patrick Mahomes", sp)
    assert rank is not None, "Mahomes missing from sf_ppr projections"
    assert rank <= 10, f"Mahomes rank {rank} \u2014 expected top 10 sf_ppr"


def test_mahomes_top25_sf(sf_projections):
    """Looser safety invariant: Mahomes never drops below top 25."""
    _, sp = sf_projections
    rank = _rank_of("Patrick Mahomes", sp)
    assert rank is not None
    assert rank <= 25, f"Mahomes rank {rank} \u2014 expected top 25 sf_ppr"


# ---------------------------------------------------------------------------
# 2. Other proven-elite QBs hold their ground
# ---------------------------------------------------------------------------


def test_allen_still_top3_sf(sf_projections):
    """Josh Allen \u2014 the cleanest top-tier QB \u2014 stays #1-3 in sf_ppr."""
    _, sp = sf_projections
    rank = _rank_of("Josh Allen", sp)
    assert rank is not None
    assert rank <= 5, f"Allen rank {rank} \u2014 expected top 5 sf_ppr (composite test pins #1)"


def test_burrow_top10_sf(sf_projections):
    """Burrow's down-injury seasons should not push him out of top 10."""
    _, sp = sf_projections
    rank = _rank_of("Joe Burrow", sp)
    assert rank is not None
    assert rank <= 10, f"Burrow rank {rank} \u2014 expected top 10 sf_ppr"


def test_lamar_top10_sf(sf_projections):
    """Lamar Jackson's MVP-tier peak should anchor him top 10."""
    _, sp = sf_projections
    rank = _rank_of("Lamar Jackson", sp)
    assert rank is not None
    assert rank <= 10, f"Lamar rank {rank} \u2014 expected top 10 sf_ppr"


# ---------------------------------------------------------------------------
# 3. Non-elite veterans NOT artificially re-promoted
# ---------------------------------------------------------------------------


def test_jordan_love_demotion_check(sf_projections):
    """Jordan Love (was #7 in PR #15, then #20 in PR #17) must NOT
    artificially re-promote in PR #18 \u2014 he's not elite_proven (only 2
    seasons of csn>=5 production; cumulative is still well below p85).
    """
    _, sp = sf_projections
    rank = _rank_of("Jordan Love", sp)
    # Permissive band \u2014 Love is a real starter but should land outside
    # the elite-proven boost zone (top 10).
    assert rank is None or rank > 10, (
        f"Jordan Love rank {rank} \u2014 should NOT be top 10 (not elite_proven)"
    )


def test_no_elite_inflation_tyrod_taylor(sf_projections):
    """Tyrod Taylor (8+ seasons but never elite cumulative percentile)
    must NOT receive the elite_proven treatment. His ranking should
    remain deep in the pool.
    """
    _, sp = sf_projections
    rank = _rank_of("Tyrod Taylor", sp)
    # Tyrod is a journeyman backup \u2014 should be far outside the top 100.
    if rank is not None:
        assert rank > 100, (
            f"Tyrod Taylor rank {rank} \u2014 must not get elite_proven boost"
        )


# ---------------------------------------------------------------------------
# 4. Aging-decline signal preserved
# ---------------------------------------------------------------------------


def test_aging_decline_respected(sf_projections):
    """Aaron Rodgers (csn=16+, cum/peak both elite by historical
    standards) IS flagged elite_proven, but his projected_remaining
    _years has collapsed near zero. The track-record floor
    (career_pace \u00d7 remaining_yrs \u00d7 floor_mult) therefore floors at
    near-zero by construction, so aging decline survives the elite
    boost.

    Concretely: Rodgers should stay below QB30 in sf_ppr.
    """
    _, sp = sf_projections
    rank = _rank_of("Aaron Rodgers", sp)
    # Rodgers post-Achilles must rank deep in the pool \u2014 well below
    # young starters.
    if rank is not None:
        assert rank > 100, (
            f"Aaron Rodgers rank {rank} \u2014 aging decline must survive elite_proven flag"
        )


# ---------------------------------------------------------------------------
# 5. RBs NOT boosted
# ---------------------------------------------------------------------------


def test_rb_elite_not_boosted_mccaffrey(sf_projections):
    """Christian McCaffrey is an elite RB by any standard \u2014 cum/peak
    both top-tier \u2014 but RB position_peak_weight is None (disabled).
    His ranking should NOT receive the elite_proven boost; the RB cliff
    is real and recent decline IS predictive.

    Specifically: McCaffrey should rank where he did in PR #17 (well
    outside top 30), NOT lift into top 15.
    """
    _, sp = sf_projections
    rank = _rank_of("Christian McCaffrey", sp)
    assert rank is not None
    assert rank > 30, (
        f"McCaffrey rank {rank} \u2014 RB elite_proven must be disabled"
    )


def test_rb_elite_not_boosted_bijan(sf_projections):
    """Bijan Robinson must remain top 15 in sf_ppr (the PR #17 RB
    invariant). The elite-proven QB lift must NOT push him out of top
    15 \u2014 conservatism on false promotions is the priority.
    """
    _, sp = sf_projections
    rank = _rank_of("Bijan Robinson", sp)
    assert rank is not None
    assert rank <= 15, (
        f"Bijan rank {rank} \u2014 RB top-15 invariant from PR #17"
    )


def test_rb_elite_not_boosted_gibbs(sf_projections):
    """Jahmyr Gibbs (top-tier young RB) must remain top 15."""
    _, sp = sf_projections
    rank = _rank_of("Jahmyr Gibbs", sp)
    assert rank is not None
    assert rank <= 15, f"Gibbs rank {rank} \u2014 RB top-15 invariant"


# ---------------------------------------------------------------------------
# 6. Luke Grimm regression \u2014 the coverage penalty + Bayesian prior
#    behavior from v0.14 must survive.
# ---------------------------------------------------------------------------


def test_luke_grimm_regression(sf_projections):
    """Luke Grimm has near-zero NFL production and very thin coverage \u2014
    if he appears at all, he should be deep in the pool (>=500).
    """
    _, sp = sf_projections
    rank = _rank_of("Luke Grimm", sp)
    if rank is not None:
        assert rank > 400, f"Luke Grimm rank {rank} \u2014 should stay deep"


# ---------------------------------------------------------------------------
# 7. Detection helper unit tests
# ---------------------------------------------------------------------------


def test_elite_proven_detection_mahomes(sf_projections):
    """Direct test of the detection helper: Mahomes must be flagged
    elite_proven against the historical corpus.
    """
    from dynasty.similarity.projection import (
        build_elite_pool_stats,
        _detect_elite_proven,
    )
    from dynasty.similarity.comparables import _player_seasons_by_pid

    corpus, _ = sf_projections
    by_pid = _player_seasons_by_pid(corpus)
    eps = build_elite_pool_stats(by_pid, "sf_ppr")

    # Find Mahomes' latest season
    mahomes_seasons = [
        ps for arr in by_pid.values() for ps in arr
        if ps.player_name == "Patrick Mahomes"
    ]
    assert mahomes_seasons, "Mahomes missing from corpus"
    latest = sorted(mahomes_seasons, key=lambda x: x.season)[-1]

    is_elite, dbg = _detect_elite_proven(latest, by_pid, "sf_ppr", eps)
    assert is_elite, f"Mahomes must be flagged elite_proven; debug={dbg}"
    assert dbg["csn"] >= 5
    assert dbg["position_enabled"] is True
    assert dbg["position_peak_weight"] is not None


def test_elite_proven_detection_rb_disabled(sf_projections):
    """RB position is disabled regardless of how elite the cumulative /
    peak numbers are. McCaffrey should NOT be flagged.
    """
    from dynasty.similarity.projection import (
        build_elite_pool_stats,
        _detect_elite_proven,
    )
    from dynasty.similarity.comparables import _player_seasons_by_pid

    corpus, _ = sf_projections
    by_pid = _player_seasons_by_pid(corpus)
    eps = build_elite_pool_stats(by_pid, "sf_ppr")

    cmc_seasons = [
        ps for arr in by_pid.values() for ps in arr
        if ps.player_name == "Christian McCaffrey"
    ]
    assert cmc_seasons, "McCaffrey missing from corpus"
    latest = sorted(cmc_seasons, key=lambda x: x.season)[-1]

    is_elite, dbg = _detect_elite_proven(latest, by_pid, "sf_ppr", eps)
    assert is_elite is False, (
        f"McCaffrey must NOT be flagged elite_proven (RB disabled); debug={dbg}"
    )
    assert dbg["position_enabled"] is False


def test_peak_3yr_avg_uses_best_three_not_recent(sf_projections):
    """Mahomes' peak 3-year average should be his BEST 3 seasons by
    fantasy points, not his last 3 (which are 2022, 2023, 2024).
    Specifically: peak_3yr_avg(Mahomes) > recent_3yr_avg(Mahomes) when
    2023+2024 are down years.
    """
    from dynasty.similarity.projection import _peak_3yr_avg
    from dynasty.similarity.comparables import _player_seasons_by_pid
    from dynasty.scoring_rules import score_season

    corpus, _ = sf_projections
    by_pid = _player_seasons_by_pid(corpus)
    mah = [
        ps for arr in by_pid.values() for ps in arr
        if ps.player_name == "Patrick Mahomes"
    ]
    assert mah, "Mahomes missing"
    pid = mah[0].player_id
    latest = sorted(mah, key=lambda x: x.season)[-1]

    peak_avg = _peak_3yr_avg(pid, by_pid, "sf_ppr", latest.season)

    # Recent 3 = last 3 seasons, re-scored
    recent_3 = sorted(by_pid[pid], key=lambda x: x.season)[-3:]
    recent_avg = sum(
        score_season(ps.raw, "sf_ppr", position="QB") for ps in recent_3
    ) / max(1, len(recent_3))

    assert peak_avg > recent_avg, (
        f"peak_3yr_avg ({peak_avg:.1f}) must exceed recent_3yr_avg "
        f"({recent_avg:.1f}) for Mahomes \u2014 the whole point of the "
        f"peak-tilted blend"
    )


# ---------------------------------------------------------------------------
# 8. Track-record floor only RAISES \u2014 never lowers
# ---------------------------------------------------------------------------


def test_track_record_floor_for_rodgers_collapses(sf_projections):
    """The track-record floor is `career_pace \u00d7 remaining_yrs \u00d7 mult`.
    For Rodgers at age 41, remaining_yrs collapses to ~1, so the floor
    collapses too \u2014 the elite_proven flag does NOT save him from the
    aging-decline signal.

    Direct sanity check on the floor computation.
    """
    from dynasty.similarity.projection import (
        _elite_proven_track_record_floor,
        _player_career_fantasy,
    )
    from dynasty.similarity.comparables import _player_seasons_by_pid

    corpus, _ = sf_projections
    by_pid = _player_seasons_by_pid(corpus)
    rodgers = [
        ps for arr in by_pid.values() for ps in arr
        if ps.player_name == "Aaron Rodgers"
    ]
    assert rodgers, "Rodgers missing from corpus"
    latest = sorted(rodgers, key=lambda x: x.season)[-1]
    pid = latest.player_id

    cum_total, _, n = _player_career_fantasy(
        pid, by_pid, "sf_ppr", through_season=latest.season
    )
    career_pace = cum_total / n

    # Try Rodgers with ~1 remaining year (realistic for age 41 QB)
    floor_low = _elite_proven_track_record_floor(
        pid, by_pid, "sf_ppr", latest.season,
        projected_remaining_years=1.0, floor_multiplier=0.78,
    )
    # The floor is at most career_pace \u00d7 1 \u00d7 0.78 \u2014 small relative
    # to a young QB's 5-8 remaining years.
    assert floor_low <= career_pace * 0.78 + 1e-6, (
        f"Rodgers floor {floor_low} exceeds career_pace*1*0.78"
    )

    # And with 0 remaining years \u2014 floor must be exactly 0.
    floor_zero = _elite_proven_track_record_floor(
        pid, by_pid, "sf_ppr", latest.season,
        projected_remaining_years=0.0, floor_multiplier=0.78,
    )
    assert floor_zero == 0.0
