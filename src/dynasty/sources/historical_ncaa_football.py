"""Historical NCAA FBS player-season corpus.

This is NOT a ranking adapter — it does not emit ``RankingRecord``s and is
intentionally NOT registered in ``sources/__init__.py:REGISTRY``. Instead, it
exposes :func:`load_ncaa_seasons` and :func:`load_ncaa_rosters` which the
:mod:`dynasty.similarity` college-football engine consumes.

Why ``sportsdataverse/cfbfastR-data`` instead of CollegeFootballData.com or
sports-reference.com/cfb?

* **CFBData API** requires an API key and rate-limits aggressively. The
  ``cfbd_breakouts`` source already reads a *local CSV* for that reason.
* **sports-reference.com/cfb** sits behind Cloudflare's bot challenge and
  blocks CI runs (HTTP 403).
* The ``cfbfastR-data`` project publishes pre-aggregated play-by-play and
  roster CSVs as GitHub release assets (MIT licensed). PBP rows include
  per-player event IDs (passer, rusher, receiver, sack-taken, target,
  touchdown) which we stream-aggregate into season totals here. No
  scraping required.

Cache layout (committed to repo):

  data/historical_ncaa_football/season_<YYYY>.json   — per-season aggregated
                                                       player totals
  data/historical_ncaa_football/roster_<YYYY>.csv.gz — per-season roster
                                                       (position, class year)

The cache is built once via ``DYNASTY_FB_NCAA_LIVE=1`` and committed. CI must
NOT hit the external endpoint. cfbfastR-data only goes back to **2014** (11
seasons), not the 25 originally hoped-for. That window is documented in the
PR description as a follow-up gap (PR #17).

Output schema (one row per cfb_player_id-season):

    cfb_player_id        str    ESPN athlete_id from cfbfastR
    season               int    Calendar year of the college season
    name                 str
    team                 str    School name (cfbfastR convention)
    conference           str    FBS conference name (or empty for FCS pulls)
    conference_tier      str    "P5" / "G5_top" / "G5" / "FCS"
    class_year           str    "FR" / "SO" / "JR" / "SR" / ""
    position             str    QB / RB / WR / TE / "" (best-guess from
                                roster crosswalk + PBP role inference)
    games                int    Distinct game_ids the player appeared in
    pass_att / pass_comp / pass_yds / pass_td / int_thrown / sacks_taken
    rush_att / rush_yds / rush_td
    rec / targets / rec_yds / rec_td
    scrimmage_yds / scrimmage_td
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Optional


# ---------------------------------------------------------------------------
# Cache layout + upstream
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "data" / "historical_ncaa_football"

# Upstream raw PBP + roster CSVs (only hit when DYNASTY_FB_NCAA_LIVE=1).
PBP_CSV_URL_TPL = (
    "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/"
    "main/player_stats/csv/player_stats_{year}.csv"
)
ROSTER_CSV_URL_TPL = (
    "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/"
    "main/rosters/csv/cfb_rosters_{year}.csv"
)

LIVE_ENV_VAR = "DYNASTY_FB_NCAA_LIVE"
USER_AGENT = (
    "Dynasty Football Model research bot - contact: gregory@stiehl.com"
)

# cfbfastR-data publishes player_stats from 2014 onwards. Rosters go back to
# 2004 but without per-player stat aggregation they're not useful for the
# similarity engine.
MIN_NCAA_SEASON = 2014
DEFAULT_MAX_SEASON = 2024

# Per-season top-N skill players to retain after aggregation. Bound the
# corpus so the cache stays well under 25MB.
TOP_N_PER_SEASON = 2000
MIN_GAMES = 4
# A meaningful skill-position season: any one of these triggers retention.
MIN_TOUCHES_RB = 30           # carries or receptions for RB-shaped players
MIN_REC_WR_TE = 15            # catches for WR/TE-shaped players
MIN_PASS_ATT_QB = 60          # attempts for QB-shaped players

# Conference strength tiers (cfbfastR uses the marketing conference names).
P5_CONFERENCES = {
    "ACC", "Big 12", "Big Ten", "Pac-12", "SEC",
    "FBS Independents",   # Notre Dame era
}
G5_TOP_CONFERENCES = {"American Athletic", "Mountain West", "Sun Belt"}
G5_OTHER_CONFERENCES = {
    "Conference USA", "Mid-American", "FBS Independents (FCS)",
}
# Anything else inferred as FCS / lower division.

CONFERENCE_MULTIPLIER = {
    "P5": 1.00,
    "G5_top": 0.85,
    "G5": 0.75,
    "FCS": 0.65,
}

# US class-year token → standard abbreviation.
CLASS_YEAR_TO_ABBR = {
    "1": "FR", "2": "SO", "3": "JR", "4": "SR", "5": "SR",
    "FR": "FR", "SO": "SO", "JR": "JR", "SR": "SR",
    "Freshman": "FR", "Sophomore": "SO", "Junior": "JR", "Senior": "SR",
    "Redshirt Freshman": "FR", "Redshirt Sophomore": "SO",
    "Redshirt Junior": "JR", "Redshirt Senior": "SR",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def conference_tier(conference: str) -> str:
    c = (conference or "").strip()
    if c in P5_CONFERENCES:
        return "P5"
    if c in G5_TOP_CONFERENCES:
        return "G5_top"
    if c in G5_OTHER_CONFERENCES:
        return "G5"
    if not c or c == "NA":
        return "FCS"
    # Default unknown to FCS — keeps multiplier conservative.
    return "FCS"


def _safe_int(v) -> int:
    if v in (None, "", "NA"):
        return 0
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _safe_str(v) -> str:
    if v in (None, "NA"):
        return ""
    return str(v).strip()


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _season_cache_path(year: int) -> Path:
    return CACHE_DIR / f"season_{year}.json"


def _roster_cache_path(year: int) -> Path:
    return CACHE_DIR / f"roster_{year}.csv.gz"


def load_ncaa_seasons(
    min_season: int = MIN_NCAA_SEASON,
    max_season: int = DEFAULT_MAX_SEASON,
) -> list[dict]:
    """Return cached NCAA player-season records (list of dicts).

    Reads every ``season_<YYYY>.json`` in the cache dir for years in range.
    """
    out: list[dict] = []
    for y in range(min_season, max_season + 1):
        p = _season_cache_path(y)
        if not p.exists():
            continue
        try:
            rows = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(rows)
    return out


def load_ncaa_rosters(
    min_season: int = MIN_NCAA_SEASON,
    max_season: int = DEFAULT_MAX_SEASON,
) -> list[dict]:
    """Return cached roster records across all years."""
    out: list[dict] = []
    for y in range(min_season, max_season + 1):
        p = _roster_cache_path(y)
        if not p.exists():
            continue
        try:
            with gzip.open(p, "rt", encoding="utf-8", newline="") as fh:
                out.extend(list(csv.DictReader(fh)))
        except (OSError, csv.Error):
            continue
    return out


# ---------------------------------------------------------------------------
# Aggregation — turn cfbfastR PBP into per-player-per-season totals.
# ---------------------------------------------------------------------------

def _new_player_agg() -> dict:
    return {
        "cfb_player_id": "",
        "name": "",
        "team": "",
        "conference": "",
        "games": set(),  # game_id set; converted to len() at the end.
        "pass_att": 0, "pass_comp": 0, "pass_yds": 0, "pass_td": 0,
        "int_thrown": 0, "sacks_taken": 0,
        "rush_att": 0, "rush_yds": 0, "rush_td": 0,
        "rec": 0, "targets": 0, "rec_yds": 0, "rec_td": 0,
    }


def _attribute_play(agg: dict[str, dict], row: dict) -> None:
    """Update the per-player aggregate dict from one PBP row."""
    gid = row.get("game_id") or ""
    team = _safe_str(row.get("team"))
    conf = _safe_str(row.get("conference"))

    def _touch(pid: str, name: str) -> dict:
        a = agg.setdefault(pid, _new_player_agg())
        a["cfb_player_id"] = pid
        if not a["name"]:
            a["name"] = name
        if not a["team"]:
            a["team"] = team
        if not a["conference"]:
            a["conference"] = conf
        a["games"].add(gid)
        return a

    # ---- Passing
    pid = _safe_str(row.get("completion_player_id"))
    if pid:
        a = _touch(pid, _safe_str(row.get("completion_player")))
        a["pass_att"] += 1
        a["pass_comp"] += 1
        a["pass_yds"] += _safe_int(row.get("completion_yds"))
    pid = _safe_str(row.get("incompletion_player_id"))
    if pid:
        a = _touch(pid, _safe_str(row.get("incompletion_player")))
        a["pass_att"] += 1
    pid = _safe_str(row.get("interception_thrown_player_id"))
    if pid:
        a = _touch(pid, _safe_str(row.get("interception_thrown_player")))
        a["pass_att"] += 1
        a["int_thrown"] += 1
    pid = _safe_str(row.get("sack_taken_player_id"))
    if pid:
        a = _touch(pid, _safe_str(row.get("sack_taken_player")))
        a["sacks_taken"] += 1

    # ---- Rushing
    pid = _safe_str(row.get("rush_player_id"))
    if pid:
        a = _touch(pid, _safe_str(row.get("rush_player")))
        a["rush_att"] += 1
        a["rush_yds"] += _safe_int(row.get("rush_yds"))

    # ---- Receiving
    rec_pid = _safe_str(row.get("reception_player_id"))
    if rec_pid:
        a = _touch(rec_pid, _safe_str(row.get("reception_player")))
        a["rec"] += 1
        a["targets"] += 1
        a["rec_yds"] += _safe_int(row.get("reception_yds"))
    tgt_pid = _safe_str(row.get("target_player_id"))
    if tgt_pid and tgt_pid != rec_pid:
        a = _touch(tgt_pid, _safe_str(row.get("target_player")))
        a["targets"] += 1

    # ---- Touchdowns. cfbfastR's ``touchdown_player_id`` is unreliable
    # for passing TDs — sometimes it tags the receiver, sometimes the
    # completing QB. We instead key off ``touchdown_stat == 1`` and
    # credit by play context:
    #   * reception present  → rec_td to receiver AND pass_td to passer
    #   * rush present       → rush_td to rusher
    # This matches the official NCAA box-score convention.
    td_stat = _safe_str(row.get("touchdown_stat"))
    if td_stat in ("1", "1.0"):
        rush_pid = _safe_str(row.get("rush_player_id"))
        comp_pid = _safe_str(row.get("completion_player_id"))
        if rec_pid:
            agg[rec_pid]["rec_td"] += 1
            if comp_pid:
                agg[comp_pid]["pass_td"] += 1
        elif rush_pid:
            agg[rush_pid]["rush_td"] += 1


def _classify_position(a: dict) -> str:
    """Infer a primary position from production mix when roster lookup fails."""
    pass_att = a["pass_att"]
    rush = a["rush_att"]
    rec = a["rec"]
    if pass_att >= MIN_PASS_ATT_QB and pass_att >= 3 * rush and pass_att >= 3 * rec:
        return "QB"
    if rush >= rec * 1.5 and rush >= MIN_TOUCHES_RB:
        return "RB"
    if rec >= MIN_REC_WR_TE:
        return "WR"
    return ""


def _passes_filters(a: dict, pos: str) -> bool:
    games = a["games"]
    games = games if isinstance(games, int) else len(games)
    if games < MIN_GAMES:
        return False
    if pos == "QB":
        return a["pass_att"] >= MIN_PASS_ATT_QB
    if pos == "RB":
        return (a["rush_att"] + a["rec"]) >= MIN_TOUCHES_RB
    if pos in ("WR", "TE"):
        return a["rec"] >= MIN_REC_WR_TE
    return False


def _load_roster_for_year(year: int) -> dict[str, dict]:
    """Return {athlete_id: roster_row} from the cached roster CSV."""
    p = _roster_cache_path(year)
    if not p.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        with gzip.open(p, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                aid = _safe_str(r.get("athlete_id"))
                if aid:
                    out[aid] = r
    except (OSError, csv.Error):
        return {}
    return out


def aggregate_season_from_pbp(
    pbp_path: Path,
    year: int,
    top_n: int = TOP_N_PER_SEASON,
) -> list[dict]:
    """Stream-aggregate a cfbfastR PBP CSV into per-player-season rows.

    Reads the file row-by-row to avoid loading 50MB+ into memory. Filters
    to skill-position players with MIN_GAMES games and meaningful touches.
    Returns the top ``top_n`` players ordered by total scrimmage yards
    (scrimmage + 0.5*pass_yds proxy so QBs aren't lost).
    """
    agg: dict[str, dict] = {}
    with open(pbp_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            _attribute_play(agg, row)

    roster = _load_roster_for_year(year)

    # Materialize rows
    rows: list[dict] = []
    for pid, a in agg.items():
        games = len(a["games"])
        a["games"] = games

        # Roster-driven position takes precedence; PBP-inferred is fallback.
        r = roster.get(pid, {})
        pos_raw = _safe_str(r.get("position")).upper()
        if pos_raw == "FB":
            pos_raw = "RB"
        if pos_raw not in ("QB", "RB", "WR", "TE"):
            pos_raw = _classify_position(a)
        if not pos_raw:
            continue

        if not _passes_filters(a, pos_raw):
            continue

        class_year = CLASS_YEAR_TO_ABBR.get(_safe_str(r.get("year")), "")
        conf_tier = conference_tier(a["conference"])

        scrimmage_yds = a["rush_yds"] + a["rec_yds"]
        scrimmage_td = a["rush_td"] + a["rec_td"]

        rows.append({
            "cfb_player_id": pid,
            "season": year,
            "name": a["name"] or _safe_str(r.get("first_name")) + " " + _safe_str(r.get("last_name")),
            "team": a["team"],
            "conference": a["conference"],
            "conference_tier": conf_tier,
            "class_year": class_year,
            "position": pos_raw,
            "games": games,
            "pass_att": a["pass_att"],
            "pass_comp": a["pass_comp"],
            "pass_yds": a["pass_yds"],
            "pass_td": a["pass_td"],
            "int_thrown": a["int_thrown"],
            "sacks_taken": a["sacks_taken"],
            "rush_att": a["rush_att"],
            "rush_yds": a["rush_yds"],
            "rush_td": a["rush_td"],
            "rec": a["rec"],
            "targets": a["targets"],
            "rec_yds": a["rec_yds"],
            "rec_td": a["rec_td"],
            "scrimmage_yds": scrimmage_yds,
            "scrimmage_td": scrimmage_td,
        })

    # Rank-cap to top_n by a unified "value score" — gives QBs and skill
    # players a fair shot at making the cut.
    def _value(r: dict) -> float:
        return r["scrimmage_yds"] + 0.5 * r["pass_yds"] + 20.0 * (r["pass_td"] + r["scrimmage_td"])

    rows.sort(key=_value, reverse=True)
    return rows[:top_n]


# ---------------------------------------------------------------------------
# Live refresh — only when DYNASTY_FB_NCAA_LIVE=1
# ---------------------------------------------------------------------------

def refresh_cache(
    min_season: int = MIN_NCAA_SEASON,
    max_season: int = DEFAULT_MAX_SEASON,
    top_n: int = TOP_N_PER_SEASON,
) -> dict:
    """Re-pull cfbfastR-data PBP + rosters and rebuild the season cache.

    Gated by ``DYNASTY_FB_NCAA_LIVE=1``. Returns a small summary. No-op
    (with warning) if the env var is unset.

    Downloads the raw PBP CSV per year to a temp file, aggregates it, and
    writes the small per-season JSON. The raw PBP is NOT retained — only
    the aggregated rows.
    """
    if os.environ.get(LIVE_ENV_VAR) != "1":
        return {"ok": False, "reason": f"{LIVE_ENV_VAR} not set; refusing to hit network"}

    import tempfile
    import httpx

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    summary: dict = {"ok": True, "years": {}}

    with httpx.Client(timeout=120.0, follow_redirects=True, headers=headers) as c:
        for year in range(min_season, max_season + 1):
            # Roster first (small, cached as gzip CSV)
            roster_url = ROSTER_CSV_URL_TPL.format(year=year)
            try:
                rr = c.get(roster_url)
                if rr.status_code == 200 and rr.content:
                    with gzip.open(_roster_cache_path(year), "wb") as gz:
                        gz.write(rr.content)
            except httpx.HTTPError:
                pass  # roster optional

            # PBP → aggregate
            pbp_url = PBP_CSV_URL_TPL.format(year=year)
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=f"_cfb_pbp_{year}.csv", delete=False
                ) as tmp:
                    with c.stream("GET", pbp_url) as resp:
                        if resp.status_code != 200:
                            summary["years"][year] = {"ok": False, "code": resp.status_code}
                            continue
                        for chunk in resp.iter_bytes():
                            tmp.write(chunk)
                    tmp_path = Path(tmp.name)

                rows = aggregate_season_from_pbp(tmp_path, year, top_n=top_n)
                _season_cache_path(year).write_text(json.dumps(rows))
                summary["years"][year] = {"ok": True, "rows": len(rows)}
            except (httpx.HTTPError, OSError) as e:
                summary["years"][year] = {"ok": False, "error": str(e)}
            finally:
                try:
                    if "tmp_path" in dir():
                        tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    return summary


def cache_summary() -> dict:
    """Quick cache health check used by tests."""
    rows = load_ncaa_seasons()
    by_year: dict[int, int] = defaultdict(int)
    for r in rows:
        by_year[r["season"]] += 1
    years = sorted(by_year)
    return {
        "n_player_seasons": len(rows),
        "min_season": years[0] if years else None,
        "max_season": years[-1] if years else None,
        "rows_by_year": dict(by_year),
    }


# ---------------------------------------------------------------------------
# CLI entrypoint for `python -m dynasty.sources.historical_ncaa_football`.
# ---------------------------------------------------------------------------

def _cli() -> int:
    if "--summary" in sys.argv:
        print(json.dumps(cache_summary(), indent=2))
        return 0
    if "--refresh" in sys.argv:
        s = refresh_cache()
        print(json.dumps(s, indent=2, default=str))
        return 0 if s.get("ok") else 1
    print("Usage: python -m dynasty.sources.historical_ncaa_football [--summary|--refresh]")
    print("Refresh requires DYNASTY_FB_NCAA_LIVE=1")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
