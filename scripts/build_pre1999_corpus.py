#!/usr/bin/env python3
"""Build the v2.4 pre-1999 player-stats corpus (1980-1998).

Scrapes PFR via Wayback (cached on disk), normalizes to the same column
schema as ``data/nflverse/player_stats_season.csv.gz``, and writes
``data/nflverse/player_stats_season_pre1999.csv.gz``.

Run from the repo root::

    python3 scripts/build_pre1999_corpus.py

Idempotent: HTML cache lives at ``data/pfr_cache/`` so re-runs are
network-free.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd

# Allow running as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from dynasty.sources.pro_football_reference_seasonal import (  # noqa: E402
    fetch_season_table,
    parse_season_table,
)

log = logging.getLogger("build_pre1999_corpus")

YEARS = range(1980, 1999)
TABLES = ("fantasy", "passing", "rushing", "receiving")
OUTPUT_PATH = _REPO_ROOT / "data" / "nflverse" / "player_stats_season_pre1999.csv.gz"

# Canonical column order from data/nflverse/player_stats_season.csv.gz.
# Stats PFR doesn't expose pre-1999 stay NaN (or 0 for int columns where
# NaN would break downstream dtype assumptions).
NFLVERSE_COLUMNS = [
    "season", "season_type", "player_id", "player_name", "player_display_name",
    "position", "position_group", "headshot_url", "games", "recent_team",
    "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
    "sacks", "sack_yards", "sack_fumbles", "sack_fumbles_lost",
    "passing_air_yards", "passing_yards_after_catch", "passing_first_downs",
    "passing_epa", "passing_2pt_conversions", "pacr", "dakota",
    "carries", "rushing_yards", "rushing_tds", "rushing_fumbles",
    "rushing_fumbles_lost", "rushing_first_downs", "rushing_epa",
    "rushing_2pt_conversions",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "receiving_fumbles", "receiving_fumbles_lost", "receiving_air_yards",
    "receiving_yards_after_catch", "receiving_first_downs", "receiving_epa",
    "receiving_2pt_conversions", "racr", "target_share", "air_yards_share",
    "wopr", "special_teams_tds", "fantasy_points", "fantasy_points_ppr",
]

POSITION_MAP = {
    # RB family
    "RB": "RB", "HB": "RB", "FB": "RB",
    # WR family
    "WR": "WR", "FL": "WR", "SE": "WR",
    # TE / QB stable
    "TE": "TE", "QB": "QB",
}
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
POSITION_GROUP = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE"}

MULTI_TEAM_PATTERN = re.compile(r"^\d+TM$")  # "2TM", "3TM", "4TM"


def _to_int(s) -> int:
    """Parse to int; blank / non-numeric → 0."""
    if s is None:
        return 0
    s = str(s).strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _to_float(s):
    """Parse to float; blank → None (so pandas emits NaN)."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Per-season aggregation
# ---------------------------------------------------------------------------

def _collapse_multi_team(rows: list[dict]) -> list[dict]:
    """Collapse PFR multi-team rows.

    PFR emits, for a player who played for 2+ teams in one season:
      • one combined row with team="2TM" (the *correct* stat totals)
      • two or more per-team rows immediately after, with team="DAL", "MIA", etc.

    We keep the combined row and stash the *last* per-team abbreviation as
    ``recent_team``. For single-team seasons we keep the row as-is.

    Input rows must be from a single PFR table and already sorted as PFR
    emits them.
    """
    out: list[dict] = []
    # Group by (pfr_id) in order. PFR keeps the multi-team block contiguous.
    by_id: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        pid = r["pfr_id"]
        if pid not in by_id:
            by_id[pid] = []
            order.append(pid)
        by_id[pid].append(r)

    for pid in order:
        group = by_id[pid]
        if len(group) == 1:
            r = dict(group[0])
            r["recent_team"] = r.get("team", "")
            out.append(r)
            continue

        # Find the combined row.
        combined = next(
            (r for r in group if MULTI_TEAM_PATTERN.match(r.get("team", ""))),
            None,
        )
        if combined is None:
            # No combined row — odd; just take the first row and warn.
            log.warning("multi-row group for %s has no XTM combined row; using first row", pid)
            r = dict(group[0])
            r["recent_team"] = r.get("team", "")
            out.append(r)
            continue

        per_team = [r for r in group if not MULTI_TEAM_PATTERN.match(r.get("team", ""))]
        last_team = per_team[-1]["team"] if per_team else combined.get("team", "")
        merged = dict(combined)
        merged["recent_team"] = last_team
        out.append(merged)

    return out


def _index_by_id(rows: list[dict]) -> dict[str, dict]:
    """Index rows by pfr_id. Caller must have already collapsed multi-team rows."""
    return {r["pfr_id"]: r for r in rows}


def _normalize_position(raw_pos: str) -> str | None:
    """Normalize PFR position tokens. Returns None for non-skill positions.

    PFR sometimes emits multi-position strings like "RB/FB" — take the
    first token.
    """
    if not raw_pos:
        return None
    head = re.split(r"[/,\-\s]", raw_pos.strip())[0].upper()
    return POSITION_MAP.get(head)


def _qualifies(carries: int, targets: int | None, receptions: int, attempts: int) -> bool:
    """Universe threshold from V2.4-PRE1999-LEGENDS.md §2:
    carries ≥ 50 OR targets ≥ 20 OR pass attempts ≥ 100.

    Pre-1992 ``targets`` is missing entirely — we fall back to
    ``receptions ≥ 15``, which is roughly equivalent (~75% catch rate
    was the era norm). Without this fallback we'd lose the entire 1980s
    WR / TE universe.
    """
    if carries >= 50:
        return True
    if targets is not None and targets >= 20:
        return True
    if receptions >= 15:
        return True
    if attempts >= 100:
        return True
    return False


def build_season_rows(year: int) -> list[dict]:
    """Build normalized nflverse-schema rows for one PFR season.

    Pulls all four PFR tables, joins them on pfr_id, applies position
    + universe filters, returns one dict per qualifying player.
    """
    log.info("processing %d", year)
    raw: dict[str, list[dict]] = {}
    for tbl in TABLES:
        html = fetch_season_table(year, tbl)
        rows = parse_season_table(html, tbl, year)
        rows = _collapse_multi_team(rows)
        raw[tbl] = rows

    fantasy = _index_by_id(raw["fantasy"])
    passing = _index_by_id(raw["passing"])
    rushing = _index_by_id(raw["rushing"])
    receiving = _index_by_id(raw["receiving"])

    out_rows: list[dict] = []

    # The fantasy table is the universe — it includes anyone with any
    # offensive production. Players missing from it (pure ST / D) we
    # don't want.
    for pfr_id, f in fantasy.items():
        pos = _normalize_position(f.get("fantasy_pos", ""))
        if pos not in SKILL_POSITIONS:
            continue

        p = passing.get(pfr_id, {})
        ru = rushing.get(pfr_id, {})
        rec = receiving.get(pfr_id, {})

        # Universe threshold.
        carries = _to_int(f.get("rush_att"))
        attempts = _to_int(f.get("pass_att"))
        receptions = _to_int(f.get("rec"))
        # ``targets`` only exists 1992+ on the receiving table.
        raw_targets = rec.get("targets")
        targets_int = _to_int(raw_targets) if raw_targets else None
        if not _qualifies(carries, targets_int, receptions, attempts):
            continue

        # ``recent_team`` resolution: the fantasy table doesn't emit
        # per-team duplicate rows for multi-team seasons (only the
        # combined row, with team="2TM"), so when the fantasy row has
        # an XTM team we must look up the *actual* last team from
        # whichever stat-specific table tracks per-team rows. Pick by
        # position: rushing for RBs, receiving for WR/TE, passing for
        # QBs. Each of those tables ran through _collapse_multi_team
        # which set ``recent_team`` to the last per-team abbreviation.
        recent_team = f.get("recent_team") or f.get("team", "")
        if MULTI_TEAM_PATTERN.match(recent_team):
            stat_table = {
                "RB": ru,
                "WR": rec,
                "TE": rec,
                "QB": p,
            }[pos]
            stat_team = stat_table.get("recent_team") or ""
            if stat_team and not MULTI_TEAM_PATTERN.match(stat_team):
                recent_team = stat_team

        row = {
            "season": year,
            "season_type": "REG",
            "player_id": f"pfr_{pfr_id}",
            "player_name": f.get("player_name", ""),
            "player_display_name": f.get("player_name", ""),
            "position": pos,
            "position_group": POSITION_GROUP[pos],
            "headshot_url": "",
            "games": _to_int(f.get("g")),
            "recent_team": recent_team,
            # Passing stats — fantasy table has cmp/att/yds/td/int but no
            # sacks; the dedicated passing table fills those in.
            "completions": _to_int(f.get("pass_cmp")),
            "attempts": _to_int(f.get("pass_att")),
            "passing_yards": _to_int(f.get("pass_yds")),
            "passing_tds": _to_int(f.get("pass_td")),
            "interceptions": _to_int(f.get("pass_int")),
            "sacks": _to_int(p.get("pass_sacked")),
            "sack_yards": _to_int(p.get("pass_sacked_yds")),
            "sack_fumbles": 0,
            "sack_fumbles_lost": 0,
            "passing_air_yards": 0,
            "passing_yards_after_catch": 0,
            "passing_first_downs": 0,
            "passing_epa": None,
            "passing_2pt_conversions": _to_int(f.get("two_pt_pass")),
            "pacr": None,
            "dakota": None,
            # Rushing stats.
            "carries": carries,
            "rushing_yards": _to_int(f.get("rush_yds")),
            "rushing_tds": _to_int(f.get("rush_td")),
            "rushing_fumbles": 0,
            "rushing_fumbles_lost": _to_int(f.get("fumbles_lost")),
            "rushing_first_downs": 0,
            "rushing_epa": None,
            "rushing_2pt_conversions": 0,
            # Receiving.
            "receptions": _to_int(f.get("rec")),
            "targets": targets_int if targets_int is not None else 0,
            "receiving_yards": _to_int(f.get("rec_yds")),
            "receiving_tds": _to_int(f.get("rec_td")),
            "receiving_fumbles": 0,
            "receiving_fumbles_lost": 0,
            "receiving_air_yards": 0,
            "receiving_yards_after_catch": 0,
            "receiving_first_downs": 0,
            "receiving_epa": None,
            "receiving_2pt_conversions": 0,
            "racr": None,
            "target_share": None,
            "air_yards_share": 0,
            "wopr": None,
            "special_teams_tds": 0,
            "fantasy_points": _to_float(f.get("fantasy_points")) or 0.0,
            "fantasy_points_ppr": _to_float(f.get("fantasy_points_ppr")) or 0.0,
        }
        out_rows.append(row)

    log.info("  %d qualifying skill players in %d", len(out_rows), year)
    return out_rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    all_rows: list[dict] = []
    for year in YEARS:
        all_rows.extend(build_season_rows(year))

    df = pd.DataFrame(all_rows, columns=NFLVERSE_COLUMNS)

    # Sanity: drop any row that ended up with no useful production.
    has_production = (
        (df["carries"] > 0) | (df["attempts"] > 0) | (df["receptions"] > 0)
    )
    df = df[has_production].reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, compression="gzip")

    log.info("wrote %d rows → %s", len(df), OUTPUT_PATH)
    log.info("  unique players: %d", df["player_id"].nunique())
    log.info("  seasons: %s..%s", df["season"].min(), df["season"].max())
    by_pos = df.groupby("position").size().to_dict()
    log.info("  by position: %s", by_pos)


if __name__ == "__main__":
    main()
