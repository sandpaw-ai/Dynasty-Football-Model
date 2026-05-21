#!/usr/bin/env python3
"""
Refresh the nflverse player_stats_season.csv.gz corpus through the
latest available season.

The cached file in data/nflverse/ was last refreshed during the v1.0
build and stops at 2024. The 2025 NFL season has been played and is
now published on nflverse-data/releases/tag/stats_player as
stats_player_reg_<year>.csv.gz files. This script pulls all available
regular-season files (1999-present) and merges them into a single
gzipped CSV that matches the schema the v1/v2 engine expects.

Notes on schema drift:
  - The 2024-vintage cached file uses 52 columns.
  - The 2025-published nflverse files use 113 columns (added kicker
    and defensive stats among others).
  - This script maps the new schema down to the old schema by:
      * passing_interceptions  -> interceptions
      * sacks_suffered         -> sacks
      * sack_yards_lost        -> sack_yards
    The engine's existing column references then keep working.

Usage:
    python scripts/refresh_nflverse_corpus.py
"""
from __future__ import annotations

import csv
import gzip
import io
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "nflverse" / "player_stats_season.csv.gz"
RELEASES_BASE = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_reg_{year}.csv.gz"
)

# Range of seasons we want in the unified file. nflverse goes back to
# 1999. Stop year is inclusive.
START_SEASON = 1999
STOP_SEASON = 2025

# Column-name renames from the new nflverse schema (113 cols) to the
# old schema (52 cols) the v1/v2 engine reads.
COL_RENAME = {
    "passing_interceptions": "interceptions",
    "sacks_suffered": "sacks",
    "sack_yards_lost": "sack_yards",
}

# Old schema column order (preserved verbatim from the prior cache file).
OLD_COLUMNS = [
    "season",
    "season_type",
    "player_id",
    "player_name",
    "player_display_name",
    "position",
    "position_group",
    "headshot_url",
    "games",
    "recent_team",
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "interceptions",
    "sacks",
    "sack_yards",
    "sack_fumbles",
    "sack_fumbles_lost",
    "passing_air_yards",
    "passing_yards_after_catch",
    "passing_first_downs",
    "passing_epa",
    "passing_2pt_conversions",
    "pacr",
    "dakota",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "rushing_fumbles",
    "rushing_fumbles_lost",
    "rushing_first_downs",
    "rushing_epa",
    "rushing_2pt_conversions",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
    "receiving_fumbles",
    "receiving_fumbles_lost",
    "receiving_air_yards",
    "receiving_yards_after_catch",
    "receiving_first_downs",
    "receiving_epa",
    "receiving_2pt_conversions",
    "racr",
    "target_share",
    "air_yards_share",
    "wopr",
    "special_teams_tds",
    "fantasy_points",
    "fantasy_points_ppr",
]


def fetch_year(year: int) -> list[dict]:
    """Fetch one regular-season nflverse file and return rows in the
    old schema layout."""
    url = RELEASES_BASE.format(year=year)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "dynasty-football-model refresh script"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    with gzip.open(io.BytesIO(raw), "rt") as fh:
        reader = csv.DictReader(fh)
        out: list[dict] = []
        for row in reader:
            # Rename new-schema columns down to old-schema names.
            for new_name, old_name in COL_RENAME.items():
                if new_name in row and old_name not in row:
                    row[old_name] = row.pop(new_name)
            # Project onto the old schema; missing columns become "".
            out.append({col: row.get(col, "") for col in OLD_COLUMNS})
    return out


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    with gzip.open(OUT_PATH, "wt", newline="") as gz:
        writer = csv.DictWriter(gz, fieldnames=OLD_COLUMNS)
        writer.writeheader()
        for year in range(START_SEASON, STOP_SEASON + 1):
            try:
                rows = fetch_year(year)
            except Exception as exc:  # noqa: BLE001 - one-shot script
                print(f"  {year}: SKIP ({exc})", file=sys.stderr)
                continue
            writer.writerows(rows)
            total_rows += len(rows)
            print(f"  {year}: {len(rows):>6} rows")
    print(f"\nWrote {total_rows:,} rows to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
