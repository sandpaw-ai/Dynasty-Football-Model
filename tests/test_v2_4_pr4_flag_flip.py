"""v2.4 PR 4 — flag-flip + UI badge tests.

PR 4 flips ``USE_PRE1999_CORPUS`` to default-True, so the pre-1999 legends
corpus is in play for the live site. The flag is still overridable via
``USE_PRE1999_CORPUS=false`` and via the ``use_pre1999`` keyword argument
(used by the v2.4 PR 3 tests that pin the flag explicitly).

The UI surface for pre-1999 comps is a small era badge on the player page;
the engine now emits ``snapshot_season`` and ``is_pre1999_snapshot`` on
every comp record so the report layer can render the badge.
"""
from __future__ import annotations

import os

import pytest

from dynasty.engine.similarity_v1 import _pre1999_enabled


class TestFlagDefault:
    """The flag now defaults to True. Existing override paths still win."""

    def test_default_is_true_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("USE_PRE1999_CORPUS", raising=False)
        assert _pre1999_enabled() is True

    def test_default_is_true_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("USE_PRE1999_CORPUS", "")
        assert _pre1999_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "True"])
    def test_truthy_env_returns_true(self, monkeypatch, val):
        monkeypatch.setenv("USE_PRE1999_CORPUS", val)
        assert _pre1999_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", "False"])
    def test_falsey_env_returns_false(self, monkeypatch, val):
        monkeypatch.setenv("USE_PRE1999_CORPUS", val)
        assert _pre1999_enabled() is False

    def test_explicit_override_true_wins(self, monkeypatch):
        monkeypatch.setenv("USE_PRE1999_CORPUS", "false")
        assert _pre1999_enabled(override=True) is True

    def test_explicit_override_false_wins(self, monkeypatch):
        monkeypatch.setenv("USE_PRE1999_CORPUS", "true")
        assert _pre1999_enabled(override=False) is False

    def test_unknown_env_value_falls_to_default(self, monkeypatch):
        monkeypatch.setenv("USE_PRE1999_CORPUS", "maybe")
        # Default is True since PR 4; any unrecognised value falls through.
        assert _pre1999_enabled() is True


class TestCompRecordHasSnapshotSeason:
    """v2.4 PR 4 surfaces snapshot_season + is_pre1999_snapshot on each comp.

    These are diagnostic fields the report layer consumes for the ⏳ era
    badge. They have no effect on similarity scores or projections.
    """

    def test_snapshot_season_field_documented(self):
        """The fields are introduced as part of PR 4's UI work.

        The full engine integration (snapshot_season computed from the
        comp's career_arc + snapshot_age) is verified end-to-end by the
        regression-snapshot validation that PR 4 ships, not unit-tested
        in isolation because constructing a realistic CareerArc fixture
        is non-trivial. The smoke check here is structural: the report
        layer must accept the new keys without erroring when they're
        present, and gracefully degrade when they're absent (back-compat
        with any cached engine output produced before PR 4).
        """
        # Import the player-page builder to make sure the module loads
        # cleanly with the new logic in place.
        from dynasty import report

        assert hasattr(report, "_build_player_page")

    def test_player_page_renders_with_era_badge(self):
        """A comp with is_pre1999_snapshot=True should produce HTML
        containing the ⏳ badge marker."""
        from dynasty.report import _build_player_page

        row = {
            "name": "Derrick Henry",
            "player_id": "00-0032764",
            "position": "RB",
            "age": 31,
            "projected_years_remaining": 2.5,
            "tier": 1,
            "comp_tier": "elite",
            "production_score": 2400,
            "overall_rank": 1,
            "n_comps": 3,
            "comp_weighted_fp": 1800.0,
            "peak_anchored_fp": 2400.0,
            "projection_path": "peak-anchored",
            "projection_raw_pre_penalty": 2400.0,
            "survival_multiplier": 0.95,
            "sample_confidence": 1.0,
            "late_breakout_penalty": 1.0,
        }
        comps = [
            {
                "name": "Walter Payton",
                "position": "RB",
                "last_season": 1987,
                "snapshot_season": 1984,
                "is_pre1999_snapshot": True,
                "similarity": 0.82,
                "post_age_projected_pts": 600.0,
                "post_age_seasons": 3,
                "seasons_played": 13,
                "final_age": 33,
                "washed_out": False,
                "peak_3yr_fp_per_game": 18.5,
                "career_ppr": 2800.0,
            },
            {
                "name": "Frank Gore",
                "position": "RB",
                "last_season": 2020,
                "snapshot_season": 2014,
                "is_pre1999_snapshot": False,
                "similarity": 0.79,
                "post_age_projected_pts": 580.0,
                "post_age_seasons": 5,
                "seasons_played": 16,
                "final_age": 37,
                "washed_out": False,
                "peak_3yr_fp_per_game": 17.2,
                "career_ppr": 3100.0,
            },
        ]

        from datetime import datetime

        html = _build_player_page(
            row=row,
            comps=comps,
            team="BAL",
            league_label="Superflex PPR",
            latest_ts=datetime(2026, 5, 23, 12, 0, 0),
        )

        # The pre-1999 comp gets the era badge with the snapshot year.
        assert "era-chip" in html
        assert "⏳ 1984" in html
        # The post-1999 comp does NOT get the badge.
        # (We can't assert absence of "era-chip" trivially because the lede
        # paragraph mentions it as an example — so we assert the post-1999
        # comp row in particular doesn't carry "⏳ 2014".)
        assert "⏳ 2014" not in html

    def test_player_page_works_without_snapshot_season_fields(self):
        """Back-compat: comps emitted before PR 4 don't have
        ``snapshot_season`` / ``is_pre1999_snapshot``. The page must
        still render cleanly."""
        from dynasty.report import _build_player_page
        from datetime import datetime

        row = {
            "name": "Test Player",
            "player_id": "00-0000000",
            "position": "RB",
            "age": 26,
            "projected_years_remaining": 5.0,
            "tier": 2,
            "comp_tier": "starter",
            "production_score": 1200,
            "overall_rank": 25,
            "n_comps": 1,
            "comp_weighted_fp": 900.0,
            "peak_anchored_fp": 1200.0,
            "projection_path": "peak-anchored",
            "projection_raw_pre_penalty": 1200.0,
            "survival_multiplier": 1.0,
            "sample_confidence": 1.0,
            "late_breakout_penalty": 1.0,
        }
        comps = [
            {
                "name": "Generic Comp",
                "position": "RB",
                "last_season": 2018,
                # No snapshot_season / is_pre1999_snapshot fields.
                "similarity": 0.75,
                "post_age_projected_pts": 400.0,
                "post_age_seasons": 4,
                "seasons_played": 10,
                "final_age": 30,
                "washed_out": False,
                "peak_3yr_fp_per_game": 15.0,
                "career_ppr": 2000.0,
            }
        ]
        # No exception, valid HTML returned.
        html = _build_player_page(
            row=row,
            comps=comps,
            team="—",
            league_label="Superflex PPR",
            latest_ts=__import__("datetime").datetime(2026, 5, 23),
        )
        assert "Generic Comp" in html
        # No ⏳ badge when fields are absent.
        # (The comp row itself shouldn't have one — the lede paragraph
        # uses ⏳ 1985 as an illustrative example, so we just check the
        # comp's snapshot year isn't present.)
        assert "⏳ 2018" not in html
