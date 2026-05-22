"""Tests for the displayed-age (current-age) computation.

The model's player rankings include an ``age`` field that must match the
player's CURRENT age — the same number Pro-Football-Reference shows on
the player profile (whole years between birth_date and today).

Prior to this fix the field reported ``last_season - birth_year``, which
under-counts whenever the player's birthday for the current calendar
year has already passed.

Anchor case (the bug Phil flagged on 2026-05-22):
    Tetairoa McMillan — born 2003-04-05, last completed NFL season
    2025. PFR showed 23-047d; the model showed 22. With the new
    ``PlayerCareer.current_age`` the model agrees with PFR (23).
"""
from datetime import date

from dynasty.engine.similarity_v1 import PlayerCareer, PlayerSeason


def _make_career(
    player_id: str,
    birth_date,
    last_season: int,
    *,
    birth_year=None,
) -> PlayerCareer:
    season_age = last_season - (birth_year or (birth_date.year if birth_date else 0))
    seasons = [
        PlayerSeason(
            player_id=player_id,
            season=last_season,
            age=season_age,
            position="WR",
            games=17,
            stats={"games": 17.0},
            fantasy_points_ppr=0.0,
        )
    ]
    return PlayerCareer(
        player_id=player_id,
        name=player_id,
        position="WR",
        birth_year=birth_year if birth_year is not None else (birth_date.year if birth_date else None),
        rookie_season=last_season,
        last_season=last_season,
        seasons=seasons,
        birth_date=birth_date,
    )


def test_mcmillan_current_age_matches_pfr():
    """Tetairoa McMillan (born 2003-04-05) is 23 as of 2026-05-22."""
    pc = _make_career("Tetairoa McMillan", date(2003, 4, 5), last_season=2025)
    # As of the day Phil reported the bug:
    assert pc.current_age(date(2026, 5, 22)) == 23
    # At the START of the 2025 season he was 22 — engine internals still use this.
    assert pc.seasons[-1].age == 22


def test_current_age_before_and_after_birthday():
    """Birthday-not-yet-reached must subtract one full year."""
    pc = _make_career("X", date(1999, 6, 16), last_season=2025)
    # Before birthday in calendar year.
    assert pc.current_age(date(2026, 5, 22)) == 26
    # On the birthday.
    assert pc.current_age(date(2026, 6, 16)) == 27
    # After the birthday.
    assert pc.current_age(date(2026, 6, 17)) == 27
    # Same calendar year, before birthday → 26.
    assert pc.current_age(date(2026, 6, 15)) == 26


def test_current_age_falls_back_to_birth_year():
    """When only birth_year is known (no birth_date), use year-grained math."""
    pc = _make_career("Old", birth_date=None, last_season=2015, birth_year=1990)
    assert pc.birth_date is None
    # 2026 - 1990 = 36 regardless of month/day.
    assert pc.current_age(date(2026, 5, 22)) == 36
    assert pc.current_age(date(2026, 12, 31)) == 36


def test_current_age_returns_none_without_metadata():
    """No birth_date AND no birth_year → cannot compute."""
    pc = PlayerCareer(
        player_id="?",
        name="?",
        position="WR",
        birth_year=None,
        rookie_season=None,
        last_season=None,
        seasons=[],
        birth_date=None,
    )
    assert pc.current_age(date(2026, 5, 22)) is None


def test_with_completed_seasons_only_preserves_birth_date():
    """The trimmed copy used for the long-arc corpus must keep birth_date so
    current_age still works for active veterans."""
    pc = _make_career("Veteran", date(1990, 8, 1), last_season=2025)
    trimmed = pc.with_completed_seasons_only(2024)
    assert trimmed.birth_date == date(1990, 8, 1)
    # Trimmed career has no 2025 row, but current_age still resolves.
    assert trimmed.current_age(date(2026, 5, 22)) == 35


def test_rankings_age_column_uses_current_age(monkeypatch):
    """End-to-end: the rankings ``age`` field reflects today's age, not
    ``last_season - birth_year``.

    Anchored to Tetairoa McMillan because that's the case Phil flagged
    and because the fix is wholly contained in the rankings emit path.
    """
    from dynasty.engine import similarity_v1

    # Freeze "today" so this test is stable across calendar days. Use a
    # date well after McMillan's birthday so we get 23.
    class _FrozenDate(date):
        @classmethod
        def today(cls):  # type: ignore[override]
            return cls(2026, 5, 22)

    monkeypatch.setattr(similarity_v1, "date", _FrozenDate)

    res = similarity_v1.run_engine(persist=False)
    by_name = {r["name"]: r for r in res.rankings}
    mcm = by_name.get("Tetairoa McMillan")
    assert mcm is not None, "McMillan should be in the active rankings"
    assert mcm["age"] == 23, (
        f"Expected current age 23 (PFR-aligned) but got {mcm['age']}. "
        "If this fails, the engine reverted to last_season - birth_year semantics."
    )
