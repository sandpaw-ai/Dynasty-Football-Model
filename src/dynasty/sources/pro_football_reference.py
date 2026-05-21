"""Pro Football Reference (via nflverse) — historical player-season corpus.

This is NOT a ranking adapter — it does not emit ``RankingRecord``s and is
intentionally NOT registered in ``sources/__init__.py:REGISTRY``. Instead, it
exposes ``load_pfr_seasons()`` and ``load_pfr_players()`` which the
similarity engine and DARKO-style NFL-impact source consume.

Why nflverse instead of scraping pro-football-reference.com directly?

  * PFR's robots.txt and rate-limiting (3s+ per page) make a 45-year crawl
    a multi-hour job that's fragile under CI and would need to be excluded
    from CI entirely.
  * The nflverse project (https://github.com/nflverse/nflverse-data)
    publishes pre-aggregated PFR-derived player-season totals as CSV
    releases on GitHub — same underlying data source, no scraping
    required, MIT licensed, and stable.
  * Schema is richer than what we'd get from the PFR fantasy page alone:
    EPA, target_share, WOPR, RACR are all included.

Cache layout (committed to repo):

  data/nflverse/player_stats_season.csv.gz   — player-season stats 1999-2024
  data/nflverse/players.csv.gz               — player bio table (birth_date,
                                                positions, draft info,
                                                pfr_id ↔ gsis_id crosswalk)

Live refresh is gated behind ``DYNASTY_FB_PFR_LIVE=1``. CI must NOT hit
the external endpoint.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
from datetime import date
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Cache paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "data" / "nflverse"
PLAYER_STATS_GZ = CACHE_DIR / "player_stats_season.csv.gz"
PLAYERS_GZ = CACHE_DIR / "players.csv.gz"

# Upstream URLs (only hit when DYNASTY_FB_PFR_LIVE=1)
NFLVERSE_PLAYER_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_season.csv"
)
NFLVERSE_PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "players/players.csv"
)

LIVE_ENV_VAR = "DYNASTY_FB_PFR_LIVE"

# Politeness identity for any direct network fetch
USER_AGENT = (
    "Dynasty Football Model research bot - contact: gregory@stiehl.com"
)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _read_csv_gz(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_pfr_seasons(min_season: int = 1999) -> list[dict]:
    """Return the cached player-season corpus (list of dicts).

    Columns include: season, player_id (gsis_id), player_display_name,
    position, position_group, games, recent_team, passing_*, rushing_*,
    receiving_*, target_share, wopr, fantasy_points, fantasy_points_ppr,
    etc.

    nflverse publishes both ``REG`` (regular season only) and ``REG+POST``
    (regular + playoffs) rows for the same player-season. We keep only
    ``REG`` so the corpus is deduplicated and comparable across players
    who never made the playoffs.
    """
    rows = _read_csv_gz(PLAYER_STATS_GZ)
    out = []
    for r in rows:
        if not _safe_int(r.get("season")) or int(r["season"]) < min_season:
            continue
        st = (r.get("season_type") or "").strip()
        if st and st != "REG":
            continue
        out.append(r)
    return out


def load_pfr_players() -> list[dict]:
    """Return the cached player bio table.

    Useful columns: gsis_id, pfr_id, display_name, position_group, position,
    birth_date (YYYY-MM-DD), draft_year, draft_round, draft_pick, draft_team,
    college_name, rookie_season, last_season, status.
    """
    return _read_csv_gz(PLAYERS_GZ)


def player_age_in_season(birth_date: Optional[str], season: int) -> Optional[float]:
    """Approximate fantasy-football age: years between Sept 1 of the season
    and the player's birth date (matches PFR convention).
    """
    if not birth_date:
        return None
    try:
        y, m, d = birth_date.split("-")
        bd = date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None
    season_anchor = date(season, 9, 1)
    delta = season_anchor - bd
    return round(delta.days / 365.25, 2)


def _safe_int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "", "NA") else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Live refresh — only when DYNASTY_FB_PFR_LIVE=1
# ---------------------------------------------------------------------------


def refresh_cache() -> dict:
    """Re-pull nflverse data and rewrite the cache. Gated by env var.

    Returns a small summary. No-op (with warning) if the env var is unset.
    """
    if os.environ.get(LIVE_ENV_VAR) != "1":
        return {
            "ok": False,
            "reason": f"{LIVE_ENV_VAR} not set; refusing to hit network",
        }

    import httpx

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}

    def _pull(url: str, dst: Path) -> int:
        with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as c:
            resp = c.get(url)
            resp.raise_for_status()
            data = resp.content
        # Re-gzip for committed cache
        with gzip.open(dst, "wb") as gz:
            gz.write(data)
        return len(data)

    n_stats = _pull(NFLVERSE_PLAYER_STATS_URL, PLAYER_STATS_GZ)
    n_players = _pull(NFLVERSE_PLAYERS_URL, PLAYERS_GZ)
    return {
        "ok": True,
        "player_stats_bytes": n_stats,
        "players_bytes": n_players,
    }


# ---------------------------------------------------------------------------
# Cache health check — used by tests to confirm the committed cache is sane
# ---------------------------------------------------------------------------


def cache_summary() -> dict:
    seasons = load_pfr_seasons()
    players = load_pfr_players()
    season_years = sorted({int(r["season"]) for r in seasons if _safe_int(r.get("season"))})
    return {
        "n_player_seasons": len(seasons),
        "n_players": len(players),
        "min_season": season_years[0] if season_years else None,
        "max_season": season_years[-1] if season_years else None,
    }
