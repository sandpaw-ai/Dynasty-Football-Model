#!/usr/bin/env python3
"""Refresh nflverse data sources used by the engine.

Two files live under ``data/nflverse/``:
  * ``player_stats_season.csv.gz`` \u2014 per-season stat lines for every
    player back to 1999 (~52 columns after schema mapping).
  * ``players.csv.gz`` \u2014 player metadata (birth_date, position,
    rookie/last season, draft info).

Both are pulled from the public ``nflverse-data`` GitHub release tags.

Daily-refresh mode (default): just re-pulls the current NFL season's
stats and the players metadata file. Older completed seasons are static
and don't need to be re-fetched.

Full mode (``--full``): re-pulls every season from 1999 to the current
season, rebuilding the unified stats file from scratch. Use this when
the file is missing or after a schema change.

Phil 2026-05-22 directive: \"I want everything to pull from every source
on a daily basis ... lets make sure that the scrapes from all of the
sources runs every day as well. build that into the code.\" The launcher
(``dynasty.launcher_headless``) calls ``refresh()`` from this module on
every build, so the daily site rebuild always sees fresh nflverse data.

Usage:
    python scripts/refresh_nflverse_corpus.py            # daily mode
    python scripts/refresh_nflverse_corpus.py --full     # rebuild all
    python scripts/refresh_nflverse_corpus.py --since-year 2024
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
import tempfile
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
NFLVERSE_DIR = REPO_ROOT / "data" / "nflverse"
STATS_PATH = NFLVERSE_DIR / "player_stats_season.csv.gz"
PLAYERS_PATH = NFLVERSE_DIR / "players.csv.gz"

STATS_URL_TEMPLATE = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_reg_{year}.csv.gz"
)
PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "players/players.csv.gz"
)
USER_AGENT = (
    "dynasty-football-model refresh script "
    "(+https://github.com/pstiehl/Dynasty-Football-Model)"
)

START_SEASON = 1999

# Column-name renames from the new nflverse schema (113 cols) to the
# old schema (52 cols) the v1/v2 engine reads.
COL_RENAME = {
    "passing_interceptions": "interceptions",
    "sacks_suffered": "sacks",
    "sack_yards_lost": "sack_yards",
}

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


# ---------------------------------------------------------------------------
# Season detection
# ---------------------------------------------------------------------------

def current_nfl_season(today: date | None = None) -> int:
    """Return the most recent COMPLETED NFL regular season as of ``today``.

    The NFL regular season ends in early January. We treat any date in
    March-onward as having the prior calendar year as the most recent
    completed season; January-February still belong to the prior season
    (e.g. Feb 2026 -> 2025 season). This is conservative \u2014 if a season
    file isn't yet published, ``fetch_year`` will gracefully skip it.
    """
    today = today or date.today()
    # Sept-Dec: the in-progress regular season is ``today.year``.
    # Jan-Aug: the most recent published season is ``today.year - 1``.
    return today.year if today.month >= 9 else today.year - 1


# ---------------------------------------------------------------------------
# stats_player_reg_<year>.csv.gz \u2014 the season stats file
# ---------------------------------------------------------------------------

def _fetch_year(year: int) -> list[dict]:
    """Fetch one regular-season nflverse file and return rows mapped to\n    the old (52-column) schema. Raises on network or parse failure.\n    """
    url = STATS_URL_TEMPLATE.format(year=year)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    with gzip.open(io.BytesIO(raw), "rt") as fh:
        reader = csv.DictReader(fh)
        out: list[dict] = []
        for row in reader:
            for new_name, old_name in COL_RENAME.items():
                if new_name in row and old_name not in row:
                    row[old_name] = row.pop(new_name)
            out.append({col: row.get(col, "") for col in OLD_COLUMNS})
    return out


def _read_existing_stats() -> dict[int, list[dict]]:
    """Load the existing stats file into a {year: [rows]} dict so we can\n    selectively replace a year without re-fetching everything.\n    Returns an empty dict if the file is missing or unreadable.\n    """
    if not STATS_PATH.exists():
        return {}
    by_year: dict[int, list[dict]] = {}
    try:
        with gzip.open(STATS_PATH, "rt") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    season = int(row.get("season") or 0)
                except ValueError:
                    continue
                if season <= 0:
                    continue
                by_year.setdefault(season, []).append(
                    {col: row.get(col, "") for col in OLD_COLUMNS}
                )
    except (OSError, gzip.BadGzipFile):
        return {}
    return by_year


def _write_unified_stats(by_year: dict[int, list[dict]]) -> int:
    """Write the merged {year: [rows]} dict back to the canonical\n    ``player_stats_season.csv.gz`` location. Atomic via tempfile + rename\n    so a failure mid-write can't corrupt the cached file.\n    """
    NFLVERSE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", dir=NFLVERSE_DIR, delete=False, suffix=".tmp"
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    total = 0
    try:
        with gzip.open(tmp_path, "wt", newline="") as gz:
            writer = csv.DictWriter(gz, fieldnames=OLD_COLUMNS)
            writer.writeheader()
            for year in sorted(by_year.keys()):
                writer.writerows(by_year[year])
                total += len(by_year[year])
        tmp_path.replace(STATS_PATH)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return total


# ---------------------------------------------------------------------------
# players.csv.gz \u2014 the player metadata file
# ---------------------------------------------------------------------------

def _refresh_players_metadata() -> int:
    """Re-pull ``players.csv.gz`` and replace the cached copy atomically.\n    Returns the file size in bytes for logging.\n    """
    NFLVERSE_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(PLAYERS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    if not raw or len(raw) < 1024:
        raise RuntimeError(
            f"players.csv.gz fetch returned {len(raw)} bytes - looks empty"
        )
    # Sanity check: must parse as gzip with a header row containing gsis_id.
    with gzip.open(io.BytesIO(raw), "rt") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or "gsis_id" not in header:
            raise RuntimeError(
                "players.csv.gz fetch missing gsis_id column - schema drift?"
            )
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", dir=NFLVERSE_DIR, delete=False, suffix=".tmp"
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(raw)
        tmp.close()
        tmp_path.replace(PLAYERS_PATH)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return len(raw)


# ---------------------------------------------------------------------------
# Public refresh API used by the launcher
# ---------------------------------------------------------------------------

def refresh(
    *,
    full: bool = False,
    since_year: int | None = None,
    verbose: bool = True,
) -> dict:
    """Refresh the nflverse caches.\n\n    Daily mode (default): pull the current NFL season plus ``players.csv.gz``.\n    Older seasons are static so we skip them unless the unified stats\n    file is missing.\n\n    Args:\n      full: re-pull every season from ``START_SEASON`` to the current.\n      since_year: re-pull every season from ``since_year`` to current.\n\n    Returns a summary dict with row counts and bytes fetched.\n    """
    current = current_nfl_season()
    if full:
        years_to_fetch = list(range(START_SEASON, current + 1))
    elif since_year is not None:
        years_to_fetch = list(range(max(since_year, START_SEASON), current + 1))
    else:
        years_to_fetch = [current]

    existing = _read_existing_stats()
    if not existing:
        # No cached file \u2014 must do a full rebuild regardless of mode.
        if not full:
            if verbose:
                print(
                    "  [stats] no cached file detected - forcing full rebuild"
                )
            years_to_fetch = list(range(START_SEASON, current + 1))

    fetched_rows = 0
    for year in years_to_fetch:
        try:
            rows = _fetch_year(year)
        except Exception as exc:  # noqa: BLE001 - per-year non-fatal
            if verbose:
                print(f"  [stats] {year}: SKIP ({exc})")
            continue
        existing[year] = rows
        fetched_rows += len(rows)
        if verbose:
            print(f"  [stats] {year}: {len(rows):,} rows")

    total_rows = _write_unified_stats(existing)
    if verbose:
        print(f"  [stats] wrote {total_rows:,} rows to {STATS_PATH}")

    try:
        players_bytes = _refresh_players_metadata()
        if verbose:
            print(f"  [players] {players_bytes:,} bytes -> {PLAYERS_PATH}")
    except Exception as exc:  # noqa: BLE001 - non-fatal at top level
        if verbose:
            print(f"  [players] WARN: refresh failed: {exc}")
        players_bytes = 0

    return {
        "current_season": current,
        "years_fetched": years_to_fetch,
        "rows_fetched": fetched_rows,
        "rows_total": total_rows,
        "players_bytes": players_bytes,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--full",
        action="store_true",
        help="Re-pull every season back to 1999 (full rebuild).",
    )
    ap.add_argument(
        "--since-year",
        type=int,
        default=None,
        help="Re-pull every season from this year forward.",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-year logging.",
    )
    args = ap.parse_args()
    try:
        summary = refresh(
            full=args.full,
            since_year=args.since_year,
            verbose=not args.quiet,
        )
    except Exception as exc:  # noqa: BLE001 - top-level failure surfaces
        print(f"refresh_nflverse_corpus FAIL: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(
            f"OK: current_season={summary['current_season']} "
            f"years_fetched={len(summary['years_fetched'])} "
            f"total_rows={summary['rows_total']:,}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
