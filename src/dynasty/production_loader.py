"""Load actual NFL fantasy production into the `production` table.

This is the *outcome* data that makes backtesting honest. Without it, every
"accuracy score" is theatre.

Recommended source: `nfl_data_py`, a maintained Python wrapper around nflfastR
that exposes weekly + seasonal fantasy stats going back to 1999. It's the same
data backend that powers most of the analytics community.

    pip install nfl_data_py

Usage:
    from dynasty.production_loader import load_seasons
    load_seasons([2020, 2021, 2022, 2023, 2024])

This module is in a separate file (not requirements.txt by default) because
nfl_data_py pulls a heavy pandas/pyarrow dependency tree. Install it when you're
ready to wire up real outcomes.
"""
from __future__ import annotations
from typing import Iterable
from sqlalchemy import select

from .db.session import get_session
from .db.models import Player, Production


def load_seasons(seasons: Iterable[int]) -> int:
    """Load season-totals production rows for the given NFL seasons.

    Strategy:
      1. Pull weekly stats from nfl_data_py for each season.
      2. Aggregate to season totals.
      3. Match to existing players by (a) sleeper_id when available via the
         nfl_data_py player ID crosswalk, (b) name + position fallback.
      4. Upsert into production (player_id, season, week=NULL).

    Returns the number of rows upserted.
    """
    try:
        import nfl_data_py as nfl
    except ImportError:
        raise ImportError(
            "nfl_data_py is required for production loading. "
            "Install with: pip install nfl_data_py"
        )

    import pandas as pd

    seasons = list(seasons)
    weekly = nfl.import_weekly_data(seasons)

    # Compute PPR / half-PPR / standard fantasy points from raw stats.
    # nfl_data_py already provides `fantasy_points` and `fantasy_points_ppr`
    # in recent versions; we'll use those if present.
    cols_needed = ["player_id", "player_display_name", "position", "season",
                   "recent_team", "fantasy_points", "fantasy_points_ppr"]
    weekly = weekly[[c for c in cols_needed if c in weekly.columns]].copy()

    # Half-PPR isn't always provided; derive a usable approximation:
    if "fantasy_points_half_ppr" not in weekly.columns and "fantasy_points" in weekly.columns and "fantasy_points_ppr" in weekly.columns:
        weekly["fantasy_points_half_ppr"] = (weekly["fantasy_points"] + weekly["fantasy_points_ppr"]) / 2

    # Aggregate to season totals
    agg_cols = {
        c: "sum"
        for c in ["fantasy_points", "fantasy_points_ppr", "fantasy_points_half_ppr"]
        if c in weekly.columns
    }
    season_totals = (
        weekly.groupby(["player_id", "player_display_name", "position", "season"], dropna=False)
        .agg({**agg_cols, "recent_team": "last"})
        .reset_index()
    )
    # Games played = count of weekly rows for the player/season
    games = weekly.groupby(["player_id", "season"]).size().rename("games_played").reset_index()
    season_totals = season_totals.merge(games, on=["player_id", "season"], how="left")

    # Build a player ID crosswalk for resolution
    try:
        ids = nfl.import_ids()
        # ids has columns like gsis_id, sleeper_id, espn_id, name, position
        gsis_to_sleeper = dict(zip(ids["gsis_id"], ids["sleeper_id"]))
    except Exception:
        gsis_to_sleeper = {}

    count = 0
    with get_session() as session:
        for _, row in season_totals.iterrows():
            sleeper_id = gsis_to_sleeper.get(row["player_id"])
            player = None
            if sleeper_id:
                player = session.execute(
                    select(Player).where(Player.sleeper_id == sleeper_id)
                ).scalar_one_or_none()
            if player is None:
                # Fallback: name + position
                player = session.execute(
                    select(Player)
                    .where(Player.full_name == row["player_display_name"])
                    .where(Player.position == row["position"])
                ).scalars().first()
            if player is None:
                # Skip — we won't auto-create here; better to align players first
                continue

            existing = session.execute(
                select(Production)
                .where(Production.player_id == player.id)
                .where(Production.season == int(row["season"]))
                .where(Production.week.is_(None))
            ).scalar_one_or_none()

            pts_std = float(row["fantasy_points"]) if pd.notna(row.get("fantasy_points")) else None
            pts_ppr = float(row["fantasy_points_ppr"]) if pd.notna(row.get("fantasy_points_ppr")) else None
            pts_half = float(row["fantasy_points_half_ppr"]) if pd.notna(row.get("fantasy_points_half_ppr")) else None
            games_played = int(row["games_played"]) if pd.notna(row.get("games_played")) else None
            team = row.get("recent_team") if pd.notna(row.get("recent_team")) else None

            if existing:
                existing.fantasy_points_std = pts_std
                existing.fantasy_points_ppr = pts_ppr
                existing.fantasy_points_half_ppr = pts_half
                existing.games_played = games_played
                existing.nfl_team = team
                existing.source = "nfl_data_py"
            else:
                session.add(Production(
                    player_id=player.id,
                    season=int(row["season"]),
                    week=None,
                    nfl_team=team,
                    games_played=games_played,
                    fantasy_points_std=pts_std,
                    fantasy_points_ppr=pts_ppr,
                    fantasy_points_half_ppr=pts_half,
                    source="nfl_data_py",
                ))
            count += 1
    return count
