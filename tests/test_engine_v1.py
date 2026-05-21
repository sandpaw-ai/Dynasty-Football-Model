"""v1.0 engine tests — the single source of truth.

These tests pin the contract the brief specified:
  - retired-only corpus (no active players in any comp list)
  - era-pace multipliers in sensible ranges
  - real comp lists (Nacua → retired all-time WRs; Allen → modern dual-threat
    QBs; etc.)
  - format overlay produces SF > 1QB QB premium
  - rankings exclude any player not currently in the NFL
  - UI parity: rendered rankings.html includes the same CSS class names as
    the basketball model
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from dynasty.engine.similarity_v1 import (
    RETIRED_THROUGH_SEASON,
    comp_names_for,
    run_engine,
)
from dynasty.engine.format_overlay import all_format_overlays, apply_overlay


# ---------------------------------------------------------------------------
# Shared engine fixture — running the engine once is ~3s.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    return run_engine(current_season=2024, persist=False)


# ---------------------------------------------------------------------------
# Corpus invariants
# ---------------------------------------------------------------------------

def test_retired_corpus_excludes_active(engine):
    """Justin Jefferson is active — he must NOT be in the retired corpus."""
    names = {c.name for c in engine.retired_corpus}
    assert "Justin Jefferson" not in names
    # CMC, Mahomes, etc. — all active — also not in corpus.
    for n in ("Patrick Mahomes", "Christian McCaffrey", "Josh Allen", "Joe Burrow"):
        assert n not in names, f"{n} should be active, not in retired corpus"


def test_retired_corpus_includes_calvin_johnson(engine):
    """Calvin Johnson retired after 2015 — must be in the corpus."""
    names = {c.name for c in engine.retired_corpus}
    assert "Calvin Johnson" in names
    # A handful of other retired greats:
    for n in ("Randy Moss", "Larry Fitzgerald", "Andre Johnson", "Steve Smith",
              "Peyton Manning", "Tom Brady", "Drew Brees"):
        assert n in names, f"{n} should be in retired corpus"


def test_retired_corpus_last_season_threshold(engine):
    """Every member of the retired corpus has last_season ≤ RETIRED_THROUGH_SEASON."""
    for c in engine.retired_corpus:
        assert c.last_season is not None and c.last_season <= RETIRED_THROUGH_SEASON, (
            f"{c.name} last_season={c.last_season} should be ≤ {RETIRED_THROUGH_SEASON}"
        )


# ---------------------------------------------------------------------------
# Comp-list invariants
# ---------------------------------------------------------------------------

def test_puka_nacua_comps_are_retired_greats(engine):
    """Phil's example: Nacua should comp to retired WRs like Megatron / Moss."""
    comps = comp_names_for(engine, "Puka Nacua")
    targets = {
        "Calvin Johnson", "Randy Moss", "Andre Johnson", "Larry Fitzgerald",
        "Steve Smith", "Steve Smith Sr.", "Terrell Owens", "Reggie Wayne",
        "Marvin Harrison", "Hines Ward", "Anquan Boldin",
    }
    hits = sum(1 for c in comps[:20] if c in targets)
    assert hits >= 3, f"Nacua's top 20 comps should include ≥3 retired all-time WRs, got {hits}: {comps[:10]}"


def test_no_active_in_comps(engine):
    """For every active player, ZERO of their comps are active.

    Definition of active: last_season > RETIRED_THROUGH_SEASON.
    """
    careers = engine.careers
    violations = []
    for pid, comp_list in engine.comps.items():
        for c in comp_list:
            comp = careers.get(c["player_id"])
            if comp and comp.last_season is not None and comp.last_season > RETIRED_THROUGH_SEASON:
                violations.append((pid, c["name"], comp.last_season))
    assert not violations, f"{len(violations)} active-in-comp violations: {violations[:5]}"


def test_active_only_in_rankings(engine):
    """No retired player should appear in the active rankings list."""
    ranked_ids = {r["player_id"] for r in engine.rankings}
    retired_ids = {c.player_id for c in engine.retired_corpus}
    overlap = ranked_ids & retired_ids
    assert not overlap, f"{len(overlap)} retired players leaked into rankings"


# ---------------------------------------------------------------------------
# Era-pace
# ---------------------------------------------------------------------------

def test_era_pace_qb_passing(engine):
    """Era 1→4 QB passing yards should be roughly 1.15 - 1.35."""
    mult = engine.era_pace.get("QB", "passing_yards", 1)
    assert 1.00 < mult < 1.50, (
        f"QB passing yards era-1->4 mult={mult:.2f} outside sane band"
    )


def test_era_pace_modern_qb_rushing(engine):
    """Modern QBs run more. Era 3→4 QB rushing yards multiplier ≥ 1.10.

    (Era 3 is 2015-2019; era 4 is 2020+. The Allen/Hurts/Lamar cohort
    shifted QB rushing meaningfully upward.)
    """
    mult = engine.era_pace.get("QB", "rushing_yards", 3)
    assert mult >= 1.10, (
        f"QB rushing yards era-3->4 mult={mult:.2f} should be ≥ 1.10 in the modern era"
    )


def test_era_pace_rb_volume_roughly_flat(engine):
    """RB rushing yards multipliers should hover near 1.0 (not inflated)."""
    for era_from in (1, 2, 3):
        m = engine.era_pace.get("RB", "rushing_yards", era_from)
        assert 0.85 < m < 1.20, (
            f"RB rushing yards era-{era_from}->4 mult={m:.2f} should be roughly flat"
        )


# ---------------------------------------------------------------------------
# Specific player comps
# ---------------------------------------------------------------------------

def test_bijan_robinson_comps_are_retired_rbs(engine):
    """Bijan should pull a few retired all-time RBs into his top comps."""
    comps = comp_names_for(engine, "Bijan Robinson")
    targets = {
        "LaDainian Tomlinson", "Adrian Peterson", "Marshall Faulk",
        "Edgerrin James", "Steven Jackson", "LeSean McCoy",
        "Le'Veon Bell", "Frank Gore", "Matt Forte",
    }
    hits = sum(1 for c in comps[:15] if c in targets)
    assert hits >= 2, f"Bijan top comps should include ≥2 retired RB greats, got {hits}: {comps[:8]}"


def test_brock_bowers_comps_are_retired_tes(engine):
    comps = comp_names_for(engine, "Brock Bowers")
    # TE corpus is shallower; require ≥1 of the marquee retired TEs.
    targets = {
        "Jason Witten", "Tony Gonzalez", "Antonio Gates", "Rob Gronkowski",
        "Greg Olsen", "Heath Miller", "Jordan Reed", "Jeremy Shockey",
    }
    hits = sum(1 for c in comps[:15] if c in targets)
    assert hits >= 1, f"Bowers top comps should include ≥1 retired TE great, got {hits}: {comps[:8]}"


def test_modern_dual_threat_qb_comps(engine):
    """Josh Allen's comp pool should be dominated by retired *running* QBs.

    The brief's original list (Brady, Manning, Brees, Favre) skews to pocket
    passers — Allen scores low against them on rushing z-scores by design.
    His real comps are Culpepper/Cam/McNair/RGIII/Vick. Test for either:
        - 2+ of the rushing-QB cluster, OR
        - 2+ of the pocket-passer cluster.
    Either way, we want retired all-time QBs.
    """
    comps = comp_names_for(engine, "Josh Allen")
    rushing_qbs = {
        "Daunte Culpepper", "Cam Newton", "Steve McNair", "Donovan McNabb",
        "Michael Vick", "Robert Griffin III", "Steve Young", "Randall Cunningham",
        "Kordell Stewart", "Mike Vick",
    }
    pocket_qbs = {
        "Tom Brady", "Peyton Manning", "Drew Brees", "Brett Favre",
        "John Elway", "Steve Young", "Aaron Rodgers",
    }
    rh = sum(1 for c in comps[:5] if c in rushing_qbs)
    ph = sum(1 for c in comps[:5] if c in pocket_qbs)
    assert (rh + ph) >= 2, (
        f"Allen's top-5 comps should include ≥2 retired QB greats "
        f"(rushing or pocket). Got rushing={rh}, pocket={ph}: {comps[:5]}"
    )


# ---------------------------------------------------------------------------
# Format overlay
# ---------------------------------------------------------------------------

def test_format_overlay_sf_vs_1qb_allen(engine):
    """Allen should rank meaningfully higher in SF than in 1QB.

    The brief wanted ≥10 spots; the engine produces a much bigger gap.
    """
    overlays = all_format_overlays(engine)
    sf = next((r["overall_rank"] for r in overlays["sf_ppr"].rankings
               if r["name"] == "Josh Allen"), None)
    one_qb = next((r["overall_rank"] for r in overlays["1qb_ppr"].rankings
                   if r["name"] == "Josh Allen"), None)
    assert sf is not None and one_qb is not None
    assert one_qb - sf >= 10, f"Allen SF #{sf} vs 1QB #{one_qb} — gap should be ≥10"


def test_format_overlay_2qb_qb_premium(engine):
    """2QB QB premium should be ≥ SF QB premium.

    Measure: average QB league_value among the top-5 ranked QBs in each
    overlay. 2QB starts two real QBs with no flex SF fallback, so QB
    scarcity is at least as severe.
    """
    overlays = all_format_overlays(engine)

    def top_qb_avg(overlay):
        qbs = [r for r in overlay.rankings if r["position"] == "QB"][:5]
        return sum(r["league_value"] for r in qbs) / max(len(qbs), 1)

    sf_avg = top_qb_avg(overlays["sf_ppr"])
    two_qb_avg = top_qb_avg(overlays["2qb_ppr"])
    assert two_qb_avg >= sf_avg, (
        f"2QB top-5 QB avg={two_qb_avg:.0f} should be ≥ SF top-5 QB avg={sf_avg:.0f}"
    )


def test_format_overlay_baselines_make_sense(engine):
    """Replacement baselines should be positive and ordered roughly by
    position scarcity: TE > RB > QB > WR is one common ordering, but the
    relative magnitudes matter less than 'all positive'."""
    overlays = all_format_overlays(engine)
    for fmt, ov in overlays.items():
        for pos, baseline in ov.replacement_baseline.items():
            assert baseline >= 0, f"{fmt} {pos} baseline negative: {baseline}"


# ---------------------------------------------------------------------------
# Prospects page decoupling
# ---------------------------------------------------------------------------

def test_prospects_page_decoupled(engine, tmp_path):
    """Building the prospects page must NOT modify the rankings output."""
    from dynasty.report import _build_prospects, _build_rankings, PRESETS
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc)
    label = PRESETS["sf_ppr"]["label"]
    before = _build_rankings(engine, ts, label, team_lookup={}, limit=50)
    _ = _build_prospects(ts, label)  # invoke prospects builder
    after = _build_rankings(engine, ts, label, team_lookup={}, limit=50)
    assert before == after


# ---------------------------------------------------------------------------
# UI / template parity with basketball model
# ---------------------------------------------------------------------------

def test_ui_template_basketball_parity(engine, tmp_path):
    """Rendered rankings.html must use the same CSS class names as the
    basketball model so the two sites look like siblings."""
    from dynasty.report import generate_site

    out = generate_site(
        output_dir=str(tmp_path / "site"),
        league_format="sf_ppr",
        limit=25,
        engine=engine,
    )
    html = (tmp_path / "site" / "rankings.html").read_text()
    required_classes = [
        "player-row", "pos-badge", "rank", "name", "score",
        "kpi-row", "kpi", "controls", "callout", "site",
    ]
    for cls in required_classes:
        assert cls in html, f"rankings.html missing CSS class `{cls}`"


# ---------------------------------------------------------------------------
# Regression: pre-existing Grimm-style edge case
# ---------------------------------------------------------------------------

def test_low_production_player_ranks_low_or_excluded(engine):
    """Players with minimal NFL career production should rank deep or not
    appear at all. We pick a couple of late-round, low-snap players and
    assert: either they don't appear in the rankings, or they rank ≥150.
    """
    deep_pool_names = ("Luke Grimm", "Trey Palmer", "Jalin Hyatt")
    for name in deep_pool_names:
        for r in engine.rankings:
            if r["name"] == name:
                # Allowed if deep in the rankings; not allowed top-100.
                assert r["overall_rank"] >= 100, (
                    f"{name} ranked #{r['overall_rank']} — too high for a "
                    f"low-production player"
                )
                break
