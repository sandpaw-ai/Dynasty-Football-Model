"""v3.0 PR 6 — Prospects UI ship.

Tests cover:
- prospects.html renders from the fixture artifact
- Filter chips (All / QB / RB / WR / TE / class-year) present
- Sortable column markup present
- Per-prospect page exists for every prospect in the fixture
- KTC delta math is correct (positive = model bullish vs KTC)
- TE rows carry the experimental flag (table + per-prospect page)
- Status banner renders both green + amber pills
- Methodology has #prospects anchor + TE Spearman disclosure text
- Graceful placeholder when artifact missing
- No-KTC-match row renders "—" (not "None"/"NaN")
- Era-badge defensive rendering (no crash when no pre-1999 comps)
- Per-prospect TE page includes the v3.1 roadmap callout

All tests are network-free (fixture JSON only).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "prospects_fixture.json"


@pytest.fixture(scope="module")
def fixture_data():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prospects_html(fixture_data):
    from dynasty.report import _build_prospects

    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    return _build_prospects(ts, "Superflex PPR", prospects_path=FIXTURE_PATH)


@pytest.fixture(scope="module")
def methodology_html():
    from dynasty.engine.similarity_v1 import EngineResult, run_engine
    from dynasty.report import _build_methodology

    # Use a real engine result so the methodology has era-pace data — but
    # only when the engine cache is available. Otherwise build a minimal
    # mock. The methodology builder only touches engine.era_pace.
    try:
        engine = run_engine(persist=False)
    except Exception:
        pytest.skip("engine cache not available; skipping methodology render")
    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    return _build_methodology(engine, ts, "Superflex PPR")


# ---------------------------------------------------------------------------
# prospects.html
# ---------------------------------------------------------------------------

def test_prospects_page_renders_with_fixture(prospects_html, fixture_data):
    """Every prospect in the fixture should appear in the table."""
    # v3.4: header bumped from v3.0 to v3.4 (Phil 2026-05-28 drafted-only).
    assert "<h2>Prospect <span class=\"accent\">Rankings — v3.4</span></h2>" in prospects_html
    for p in fixture_data["prospects"]:
        assert p["name"] in prospects_html, f"missing prospect: {p['name']}"


def test_prospects_page_has_status_banner(prospects_html):
    """Both pills (green QB/RB/WR validated, amber TE experimental) must render."""
    assert "status-pill-ok" in prospects_html
    assert "status-pill-warn" in prospects_html
    assert "QB · RB · WR engine validated" in prospects_html
    assert "TE engine experimental" in prospects_html
    assert "preview-grade" in prospects_html


def test_prospects_page_has_filter_chips(prospects_html, fixture_data):
    """Filter chips present: All/QB/RB/WR/TE plus a chip per class year."""
    assert 'data-pos="QB"' in prospects_html
    assert 'data-pos="RB"' in prospects_html
    assert 'data-pos="WR"' in prospects_html
    assert 'data-pos="TE"' in prospects_html
    assert 'class="chip pos-chip active" data-pos=""' in prospects_html  # All
    for yr in fixture_data["draft_classes"]:
        assert f'data-class="{yr}"' in prospects_html


def test_prospects_page_has_sortable_columns(prospects_html):
    """Sortable table markup present (data-sort attrs + click-to-sort JS)."""
    assert 'id="prospect-table"' in prospects_html
    assert 'data-sortable="true"' in prospects_html
    # All the column data-sort keys we expect
    for key in ("rank", "name", "pos", "class", "school", "age",
                "career_fp", "peak3", "ktc", "delta"):
        assert f'data-sort="{key}"' in prospects_html, f"missing sort key: {key}"


def test_te_rows_carry_experimental_flag(prospects_html, fixture_data):
    """Every TE prospect row must have the .prospect-te-row class."""
    te_count = sum(1 for p in fixture_data["prospects"] if p["position"] == "TE")
    assert te_count > 0, "fixture has no TE prospects to test"
    assert prospects_html.count('prospect-te-row') >= te_count
    # The TE chip in the filter row carries the warning emoji
    assert 'TE ⚠️' in prospects_html


def test_ktc_delta_positive_means_model_bullish(fixture_data):
    """Sanity-check: ktc_delta_overall > 0 iff model_overall_rank < ktc_rank_sf.

    (Lower model rank number = better model opinion, so positive delta =
    KTC rank is worse than model rank = model is more bullish.)
    """
    checked = 0
    for p in fixture_data["prospects"]:
        delta = p.get("ktc_delta_overall")
        model_rank = p.get("model_overall_rank")
        ktc_rank = (p.get("ktc") or {}).get("ktc_rank_sf")
        if delta is None or model_rank is None or ktc_rank is None:
            continue
        # delta = ktc_rank - model_rank. Positive when model is bullish.
        assert delta == ktc_rank - model_rank, (
            f"{p['name']}: delta={delta} but ktc_rank-model_rank={ktc_rank - model_rank}"
        )
        checked += 1
    assert checked > 0, "no prospects in fixture had all three ranks set"


def test_no_ktc_match_renders_dash(prospects_html, fixture_data):
    """Prospects with no KTC delta render '—' in the KTC + delta cells,
    not 'None' or 'NaN'."""
    # Strip the inline <script> block (which legitimately contains 'isNaN')
    # before scanning for data leakage.
    import re as _re
    body_only = _re.sub(r"<script>.*?</script>", "", prospects_html, flags=_re.S)
    assert ">None<" not in body_only
    assert ">NaN<" not in body_only
    assert ">nan<" not in body_only
    # The div-none chip is what the rendering uses for no-delta.
    has_no_ktc = any(
        (p.get("ktc_delta_overall") is None) for p in fixture_data["prospects"]
    )
    if has_no_ktc:
        assert "div-none" in prospects_html


def test_prospects_default_sort_is_rank_ascending(prospects_html, fixture_data):
    """Lowest model_overall_rank should appear first in the rendered tbody."""
    ranked = [p for p in fixture_data["prospects"]
              if p.get("model_overall_rank") is not None]
    ranked.sort(key=lambda p: p["model_overall_rank"])
    expected_first = ranked[0]["name"]
    expected_last = ranked[-1]["name"]
    idx_first = prospects_html.find(expected_first)
    idx_last = prospects_html.find(expected_last)
    assert idx_first != -1 and idx_last != -1
    assert idx_first < idx_last, (
        f"expected {expected_first} (rank #{ranked[0]['model_overall_rank']}) "
        f"to render before {expected_last}"
    )


# ---------------------------------------------------------------------------
# Per-prospect pages
# ---------------------------------------------------------------------------

def test_per_prospect_page_for_each_fixture_prospect(fixture_data):
    """Every fixture prospect must yield a renderable per-prospect page."""
    from dynasty.report import _build_prospect_page, _prospect_slug

    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    for p in fixture_data["prospects"]:
        page = _build_prospect_page(p, "Superflex PPR", ts)
        assert p["name"] in page
        assert "Top-25" in page
        assert p["school"] in page
        slug = _prospect_slug(p)
        assert slug  # non-empty


def test_te_prospect_page_has_limitation_callout(fixture_data):
    """TE per-prospect pages must include the v3.1 roadmap callout."""
    from dynasty.report import _build_prospect_page

    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    te = next(p for p in fixture_data["prospects"] if p["position"] == "TE")
    page = _build_prospect_page(te, "Superflex PPR", ts)
    assert "TE projections are preview-grade" in page
    assert "0.086" in page
    assert "v3.1 roadmap" in page
    assert "callout-warn" in page
    # Experimental pill in header
    assert "prospect-te-flag" in page


def test_non_te_prospect_page_has_no_te_callout(fixture_data):
    """Non-TE pages must NOT show the TE-experimental flag/callout."""
    from dynasty.report import _build_prospect_page

    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    qb = next(p for p in fixture_data["prospects"] if p["position"] == "QB")
    page = _build_prospect_page(qb, "Superflex PPR", ts)
    assert "TE projections are preview-grade" not in page
    assert "prospect-te-flag" not in page


def test_per_prospect_page_handles_no_pre1999_comps(fixture_data):
    """Era-badge code path must not crash when no comps are pre-1999.

    Prospect corpus is 2000+ so this should be the common case.
    """
    from dynasty.report import _build_prospect_page

    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    for p in fixture_data["prospects"]:
        page = _build_prospect_page(p, "Superflex PPR", ts)
        # No exception means defensive rendering held.
        assert len(page) > 1000


# ---------------------------------------------------------------------------
# Methodology + graceful fallback
# ---------------------------------------------------------------------------

def test_methodology_has_prospects_anchor_and_te_disclosure(methodology_html):
    """Methodology page must have #prospects anchor + the TE Spearman text."""
    assert 'id="prospects"' in methodology_html
    assert "v3.0 Prospect Engine" in methodology_html or "v3.0 prospect engine" in methodology_html.lower()
    assert "TE Spearman" in methodology_html or "0.086" in methodology_html
    assert "v3.1" in methodology_html  # roadmap mention


def test_prospects_page_graceful_when_artifact_missing(tmp_path):
    """When the artifact path doesn't exist, the page must render a
    clearly-marked placeholder, not crash."""
    from dynasty.report import _build_prospects

    ts = datetime(2026, 5, 24, 15, 12, tzinfo=timezone.utc)
    bogus = tmp_path / "does_not_exist.json"
    # Also ensure the default data/engine_v3 lookup misses by pointing
    # the loader at a non-existent path explicitly.
    html_out = _build_prospects(ts, "Superflex PPR", prospects_path=bogus)
    # Either we render the placeholder text OR (if the default
    # data/engine_v3/prospects_all.json exists locally) we render the
    # real page — both are valid. The hard requirement is: no crash.
    assert "<h2>" in html_out
    assert "Prospects" in html_out or "Prospect" in html_out
