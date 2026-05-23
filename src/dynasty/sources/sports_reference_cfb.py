"""Sports-Reference (CFB) seasonal stats scraper — v3.0 PR 1.

College Football Reference (sub-site of sports-reference.com) is
Cloudflare-protected, but the Internet Archive's Wayback Machine has
stable snapshots of every player page and per-year leaderboard. We
mirror the exact pattern v2.4 uses for ``pro_football_reference_seasonal``:

* polite throttle (default 4 s/req, env-overridable),
* timestamped Wayback URL with fallback chain
  (2024 → 2023 → 2025 → 2022),
* HTML cache on disk so re-runs and re-parses are free.

Surface (all functions are cache-backed + pure-ish):

* :func:`fetch_year_leaderboard(year, table)` → raw HTML
* :func:`parse_year_leaderboard(html, table, year)` → ``list[dict]``
* :func:`fetch_year_standings(year)` → raw HTML (cached for PR 2's SOS work; not parsed here)
* :func:`fetch_player_page(slug)` → raw HTML
* :func:`parse_player_page(html, slug)` → ``list[dict]`` one row per career season

Supported leaderboard tables are ``passing``, ``rushing``, ``receiving``,
and ``scoring`` (the closest analogue to PFR's "fantasy" — gives every
position's TDs in one shot). Sports-reference doesn't expose a
combined-fantasy view on the CFB sub-site.

The dicts emitted by these parsers use sports-reference's native
``data-stat`` keys, plus a few normalizations:

* ``sr_slug`` — from ``data-append-csv`` on the name cell
* ``player_name`` — name cleaned of award markers (``*``, ``+``)
* ``season`` / ``table`` — copied from arguments
* ``team`` / ``conference`` — aliased from ``team_name_abbr`` /
  ``conf_abbr`` for downstream convenience
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "sandpaw-dynasty-model/3.0 "
    "(+https://pstiehl.github.io/Dynasty-Football-Model/)"
)

SUPPORTED_LEADERBOARDS = ("passing", "rushing", "receiving", "scoring")

# Wayback occasionally serves stale 403s for a specific timestamped
# capture even while neighbouring captures respond 200. We try a few
# different timestamps (most recent first) before giving up. Same chain
# v2.4 PR 1 settled on for PFR — empirically robust.
WAYBACK_TIMESTAMPS = ("2024", "2023", "2025", "2022")
SR_BASE = "https://www.sports-reference.com"

# Polite throttle. Sports-reference's own ToS floor is 1 req/sec, but
# Wayback's edge proxy enforces a much tighter cap — empirically ~10-15
# req/min before it starts refusing TCP connections. v2.4 PR 1 settled
# on 4 s/req with exponential backoff to ~3 min. We re-use the same
# tuning here. Override via the env var ``SR_CFB_SCRAPER_INTERVAL_SEC``
# if you really know what you're doing.
MIN_REQUEST_INTERVAL_SEC = float(
    os.environ.get("SR_CFB_SCRAPER_INTERVAL_SEC", "4.0")
)

# Cache lives at <repo_root>/data/sr_cache/. Resolve relative to this
# file so it works whether invoked from the repo root or a script dir.
_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "data" / "sr_cache"

# Wall-clock-shared throttle state. Module-level intentionally: this is
# a single-process scraper.
_last_request_at: float = 0.0


# ---------------------------------------------------------------------------
# HTTP / cache plumbing  (lifted from v2.4 PFR scraper; same shape)
# ---------------------------------------------------------------------------


def _throttle() -> None:
    """Block until at least MIN_REQUEST_INTERVAL_SEC has passed since last fetch."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_SEC:
        time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
    _last_request_at = time.monotonic()


def _http_get(url: str, *, max_retries: int = 5, timeout: int = 60) -> str:
    """GET with throttle + exponential backoff on 4xx/5xx + connection errors.

    Wayback's 429-style response often manifests as a TCP refusal (the
    edge node simply stops accepting) or a stale 403. We treat both the
    same as 5xx and back off aggressively (up to ~3 min) before giving up.
    """
    backoff = 5.0
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        _throttle()
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                return resp.text
            log.warning(
                "SR-CFB fetch failed (status=%s) for %s (attempt %d/%d)",
                resp.status_code, url, attempt + 1, max_retries,
            )
            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
        except requests.RequestException as exc:
            last_exc = exc
            log.warning(
                "SR-CFB fetch exception for %s (attempt %d/%d): %s",
                url, attempt + 1, max_retries, exc,
            )
        if attempt < max_retries - 1:
            sleep_s = backoff + random.uniform(0, backoff * 0.25)
            log.info("  sleeping %.1fs before retry", sleep_s)
            time.sleep(sleep_s)
            backoff = min(backoff * 2, 180.0)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"SR-CFB fetch exhausted retries for {url}")


def _http_get_with_timestamp_fallback(sr_path: str) -> str:
    """Try each Wayback timestamp in turn until one returns 200."""
    last_exc: Optional[Exception] = None
    for ts in WAYBACK_TIMESTAMPS:
        url = f"https://web.archive.org/web/{ts}/{SR_BASE}{sr_path}"
        try:
            return _http_get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Wayback timestamp %s failed for %s: %s", ts, sr_path, exc
            )
            last_exc = exc
            time.sleep(10.0)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Per-year leaderboards (passing / rushing / receiving / scoring)
# ---------------------------------------------------------------------------


def _leaderboard_cache_path(year: int, table: str) -> Path:
    return CACHE_DIR / "leaderboard" / str(year) / f"{table}.html"


def fetch_year_leaderboard(year: int, table: str) -> str:
    """Fetch HTML for one SR-CFB per-year leaderboard.

    ``table`` ∈ {``passing``, ``rushing``, ``receiving``, ``scoring``}.
    Cached at ``data/sr_cache/leaderboard/{year}/{table}.html``. Cache
    hits do not make network requests.
    """
    if table not in SUPPORTED_LEADERBOARDS:
        raise ValueError(
            f"unsupported table: {table!r}; expected one of {SUPPORTED_LEADERBOARDS}"
        )

    cache_path = _leaderboard_cache_path(year, table)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")

    log.info("Fetching SR-CFB %s leaderboard %d from Wayback", table, year)
    html = _http_get_with_timestamp_fallback(
        f"/cfb/years/{year}-{table}.html"
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# Per-year team standings (cached for PR 2's SOS pipeline; we only fetch)
# ---------------------------------------------------------------------------


def _standings_cache_path(year: int) -> Path:
    return CACHE_DIR / "leaderboard" / str(year) / "standings.html"


def fetch_year_standings(year: int) -> str:
    """Fetch per-year standings HTML and cache it.

    PR 2 (SOS corpus) will be the one that *parses* this, but caching
    now means PR 2 spends zero network budget. The standings page is one
    extra fetch per year — cheap.
    """
    cache_path = _standings_cache_path(year)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")

    log.info("Fetching SR-CFB standings %d from Wayback", year)
    html = _http_get_with_timestamp_fallback(
        f"/cfb/years/{year}-standings.html"
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# Per-player career page
# ---------------------------------------------------------------------------


_SR_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+-\d+$")


def _player_cache_path(slug: str) -> Path:
    return CACHE_DIR / "players" / f"{slug}.html"


def fetch_player_page(slug: str) -> str:
    """Fetch one SR-CFB player page from Wayback (with disk cache).

    ``slug`` is the dashed identifier sports-reference uses, e.g.
    ``"tim-tebow-1"`` for /cfb/players/tim-tebow-1.html. Validated
    against a permissive regex to catch obvious typos.
    """
    if not _SR_SLUG_RE.match(slug):
        raise ValueError(f"invalid SR slug: {slug!r}")

    cache_path = _player_cache_path(slug)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")

    log.info("Fetching SR-CFB player %s from Wayback", slug)
    html = _http_get_with_timestamp_fallback(
        f"/cfb/players/{slug}.html"
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

# SR pre-pends "*" / "+" markers (Hall of Fame / All-American) to names.
_NAME_MARKER_RE = re.compile(r"[\*\+]+$")


def _clean_name(raw: str) -> str:
    return _NAME_MARKER_RE.sub("", raw).strip()


def _to_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_table_rows(
    soup: BeautifulSoup,
    table_id: str,
    *,
    require_slug: bool,
) -> list[dict[str, str]]:
    """Return one raw ``data-stat → text`` dict per data row in ``table_id``.

    Skips in-table sub-header rows (sports-reference injects them every
    ~30 rows on long leaderboards) and visual spacer rows.

    * ``require_slug=True``  — leaderboard mode: drop any row that
      doesn't carry a ``data-append-csv`` slug on its name cell (kills
      career-total / aggregate rows).
    * ``require_slug=False`` — player-page mode: keep every row that
      has a ``year_id`` (per-season rows). The 'Career' summary row at
      the bottom has no year_id and is dropped automatically.
    """
    tab = soup.find("table", id=table_id)
    if tab is None:
        return []
    tbody = tab.find("tbody")
    if tbody is None:
        return []

    rows: list[dict[str, str]] = []
    for tr in tbody.find_all("tr"):
        cls = tr.get("class") or []
        # 'thead' = injected sub-header; 'spacer' = visual separator
        if "thead" in cls or "spacer" in cls:
            continue
        row: dict[str, str] = {}
        slug: Optional[str] = None
        for cell in tr.find_all(["th", "td"]):
            stat = cell.get("data-stat")
            if not stat:
                continue
            row[stat] = cell.get_text(strip=True)
            ac = cell.get("data-append-csv")
            if ac:
                slug = ac
        if require_slug:
            if slug is None:
                # Career-total / aggregate row on a leaderboard — skip.
                continue
            row["sr_slug"] = slug
        else:
            # Player page: per-season rows MUST have a year_id. The
            # 'Career' aggregate row at the bottom has no year_id.
            if not row.get("year_id"):
                continue
        rows.append(row)
    return rows


# Sports-reference quietly migrated their stat tables in August 2024 to
# include a ``_standard`` suffix and richer column metadata. Older
# Wayback snapshots use the legacy table ids (``passing`` /
# ``rushing`` / ``receiving`` / ``scoring`` without a suffix) plus
# legacy column names (``player`` instead of ``name_display``,
# ``school_name`` instead of ``team_name_abbr``, ``g`` instead of
# ``games``). We accept both shapes.

# Old data-stat → new data-stat alias mapping. Anything not in this map
# passes through unchanged.
_LEGACY_COLUMN_ALIASES = {
    "player": "name_display",
    "school_name": "team_name_abbr",
    "g": "games",
}


def parse_year_leaderboard(
    html: str, table: str, year: int
) -> list[dict]:
    """Parse one SR-CFB per-year leaderboard into row dicts.

    Returns one dict per ranked player. Keys are SR's modern
    ``data-stat`` names (we alias legacy snapshot columns automatically),
    plus:

    * ``sr_slug`` — sports-reference player slug
    * ``player_name`` — cleaned of award markers
    * ``season`` / ``table`` — copied from arguments
    * ``team`` / ``conference`` — normalized aliases

    Handles both the post-August-2024 ``{table}_standard`` table ids
    and the legacy ``{table}`` ids. (Wayback occasionally serves a
    pre-upgrade capture even for a 2024 timestamp.)

    Leaderboards do **not** expose player position; that has to come
    from the per-player page (or be inferred from which table the
    player showed up on).
    """
    if table not in SUPPORTED_LEADERBOARDS:
        raise ValueError(f"unsupported table: {table!r}")

    soup = BeautifulSoup(html, "lxml")

    # Try the modern id first, fall back to the legacy id.
    raws = _parse_table_rows(soup, f"{table}_standard", require_slug=True)
    if not raws:
        raws = _parse_table_rows(soup, table, require_slug=True)

    out: list[dict] = []
    for raw in raws:
        # Alias legacy columns to the modern names.
        norm = {}
        for k, v in raw.items():
            norm[_LEGACY_COLUMN_ALIASES.get(k, k)] = v
        raw_name = norm.get("name_display") or norm.get("player") or ""
        row = dict(norm)
        row["season"] = year
        row["table"] = table
        row["player_name"] = _clean_name(raw_name)
        row["team"] = (
            norm.get("team_name_abbr")
            or norm.get("team_name")
            or norm.get("school_name")
            or ""
        )
        row["conference"] = norm.get("conf_abbr") or norm.get("conf") or ""
        out.append(row)
    return out


# Sports-reference uses these position labels; v3.0 normalizes them to
# the cfbfastR canonical {QB, RB, WR, TE}. Anything else is dropped.
_POSITION_NORMALIZATION = {
    "QB": "QB",
    # Running back family
    "RB": "RB",
    "HB": "RB",
    "TB": "RB",
    "FB": "RB",
    # Wide receiver family
    "WR": "WR",
    "FL": "WR",
    "SE": "WR",
    "SL": "WR",
    "WB": "WR",
    # Tight end
    "TE": "TE",
}


def normalize_position(raw_pos: Optional[str]) -> Optional[str]:
    """Map SR position label → canonical {QB,RB,WR,TE} or None.

    Multi-position strings (``"WR/RB"``) take the first recognized
    skill-position token. Defensive / line / special-teams positions
    return ``None`` to signal "not a skill player" — caller drops them.
    """
    if not raw_pos:
        return None
    # SR sometimes writes multi-position like "QB/WR" or "RB/HB".
    tokens = re.split(r"[/\\,\s]+", raw_pos.strip().upper())
    for tok in tokens:
        if tok in _POSITION_NORMALIZATION:
            return _POSITION_NORMALIZATION[tok]
    return None


def parse_player_page(html: str, slug: str) -> list[dict]:
    """Parse one SR-CFB player page into per-season rows.

    A player can show up on multiple tables (passing + rushing for QBs,
    rushing + scoring for RBs). We merge them by ``(season, team)`` and
    emit one row per season. Position comes from the row itself
    (``pos`` column), which lets us handle WRs who briefly played QB or
    RBs who shifted to FB without forcing a career-level position.

    Each row gets normalized to the cfbfastR ``season_*.json`` schema
    (see :func:`row_to_cfb_schema`). Returns the *raw* per-season merge
    here; orchestration script does the schema-final mapping.
    """
    soup = BeautifulSoup(html, "lxml")

    # ``passing_standard`` and ``rushing_standard`` are the two skill
    # tables SR exposes on a player page (rushing includes receiving
    # columns inline). ``scoring_standard`` adds TD breakdowns; we read
    # it for completeness even though most of its info is duplicated.
    passing_rows = _parse_table_rows(soup, "passing_standard", require_slug=False)
    rushing_rows = _parse_table_rows(soup, "rushing_standard", require_slug=False)
    scoring_rows = _parse_table_rows(soup, "scoring_standard", require_slug=False)

    # Receiving info on a player page lives inside rushing_standard
    # (rec / rec_yds / rec_td columns). No standalone receiving_standard.

    # Merge by (season, team) — a transfer mid-career creates two rows
    # for one season's slug.
    def _key(r: dict) -> tuple[str, str]:
        return (r.get("year_id", ""), r.get("team_name_abbr", ""))

    merged: dict[tuple[str, str], dict] = {}
    for r in passing_rows + rushing_rows + scoring_rows:
        # SR uses '2007*' to mark championship years etc. Strip markers
        # for season parsing.
        season_raw = (r.get("year_id") or "").strip()
        season_clean = re.sub(r"[^0-9]", "", season_raw)
        if not season_clean:
            # 'Career' summary row — skip.
            continue
        season = int(season_clean)
        key = (str(season), r.get("team_name_abbr", ""))
        bucket = merged.setdefault(
            key,
            {
                "sr_slug": slug,
                "season": season,
                "team_name_abbr": r.get("team_name_abbr", ""),
                "conf_abbr": r.get("conf_abbr", ""),
                "class": r.get("class", ""),
                "pos": r.get("pos", ""),
                "games": r.get("games", ""),
            },
        )
        # Latest non-empty wins for shared fields.
        for col in ("conf_abbr", "class", "pos", "games"):
            v = r.get(col)
            if v and not bucket.get(col):
                bucket[col] = v
        # Stat columns: passing tables carry pass_*, rushing tables
        # carry rush_*/rec_*/scrim_*, scoring carries punt_ret_td etc.
        # Just upsert everything we don't already have.
        for k, v in r.items():
            if k in bucket:
                continue
            bucket[k] = v

    rows = sorted(
        merged.values(),
        key=lambda x: (x.get("season", 0), x.get("team_name_abbr", "")),
    )
    return rows


# ---------------------------------------------------------------------------
# Conference → tier mapping
# ---------------------------------------------------------------------------

# Tier classification follows the existing 2014+ cfbfastR convention:
#   P5     = power conference (autonomy / "Power Five")
#   G5_top = AAC + Mountain West (the strongest Group of Five)
#   G5     = C-USA, MAC, Sun Belt, FBS Independents
#   FCS    = sub-FBS (Big Sky, MEAC, etc.)
#
# Historical complication: the Big East was a power conference through
# the 2012 season, then dissolved (basketball schools split off, football
# schools mostly became the AAC). We honour that timeline: Big East =
# P5 through 2012, then disappears (AAC takes its slot as G5_top).
#
# Pac-12 was Pac-10 through 2010 inclusive; mapped to the same P5 slot.
# WAC was effectively G5 through 2012 (Hawaii, Boise State years).
#
# Anything not in this mapping defaults to FCS. We DON'T silently drop
# unknowns — see ``CONFERENCE_TIER_DEFAULT``.

CONFERENCE_TIER_DEFAULT = "FCS"

P5_CONFERENCES = {
    "SEC", "Big Ten", "Big 12", "ACC", "Pac-10", "Pac-12", "Pac-8",
    # Big East was P5 through 2012; we encode that with a year guard
    # in :func:`classify_conference_tier` below.
}

# Big East was a Power-conference for football through 2012. After the
# 2012 season, its football schools became the AAC (renamed 2013). For
# the years 2000-2012 inclusive we tier Big East as P5; from 2013 on,
# any 'Big East' rows are tiered G5 because they refer to the
# non-football-power leftovers (which dissolved into the renamed
# American by 2013-14).
BIG_EAST_P5_THROUGH_YEAR = 2012

G5_TOP_CONFERENCES = {
    "AAC", "American",
    "MWC", "Mountain West",
    "WAC",  # WAC at its 2008-2010 peak (Boise, Hawaii) — flagged as G5_top
}

G5_CONFERENCES = {
    "CUSA", "Conference USA", "C-USA",
    "MAC",
    "Sun Belt", "SBC",
    "Ind", "Independent",
}


def classify_conference_tier(conference: str, season: int) -> str:
    """Map (conference, season) → tier ∈ {P5, G5_top, G5, FCS}.

    Year-sensitive: Big East is P5 through 2012, then disappears as an
    FBS conference. Pac-10 / Pac-12 are the same slot.
    """
    if not conference:
        return CONFERENCE_TIER_DEFAULT

    c = conference.strip()

    # Big East special case — power through 2012.
    if c in {"Big East", "BE"}:
        return "P5" if season <= BIG_EAST_P5_THROUGH_YEAR else "G5_top"

    if c in P5_CONFERENCES:
        return "P5"

    if c in G5_TOP_CONFERENCES:
        # WAC: only flag G5_top for the 2007-2012 peak; before/after,
        # it's plain G5. (Pre-2007 it was a sleepy mid-major; after
        # 2012 it lost football.)
        if c == "WAC" and not (2007 <= season <= 2012):
            return "G5"
        return "G5_top"

    if c in G5_CONFERENCES:
        return "G5"

    return CONFERENCE_TIER_DEFAULT


# ---------------------------------------------------------------------------
# Schema bridge — SR row → cfbfastR ``season_YYYY.json`` row
# ---------------------------------------------------------------------------

# Existing 2014+ schema (verified against ``season_2024.json``):
#
#   cfb_player_id, season, name, team, conference, conference_tier,
#   class_year, position, games, pass_att, pass_comp, pass_yds, pass_td,
#   int_thrown, sacks_taken, rush_att, rush_yds, rush_td, rec, targets,
#   rec_yds, rec_td, scrimmage_yds, scrimmage_td
#
# SR field map:
#   pass_att   ← pass_att
#   pass_comp  ← pass_cmp
#   pass_yds   ← pass_yds
#   pass_td    ← pass_td
#   int_thrown ← pass_int
#   sacks_taken ← (not exposed pre-modern era — emit None)
#   rush_att   ← rush_att
#   rush_yds   ← rush_yds
#   rush_td    ← rush_td
#   rec        ← rec
#   targets    ← (SR exposes this only sporadically post-2014; emit None)
#   rec_yds    ← rec_yds
#   rec_td     ← rec_td
#   scrimmage_yds ← yds_from_scrimmage  (a.k.a. scrim_yds on leaderboards)
#   scrimmage_td  ← scrim_td             (a.k.a. all_td less special-teams on scoring)
#
# We use ``sr_<slug>`` as ``cfb_player_id`` to keep the SR-sourced
# pre-2014 records distinguishable from the 2014+ cfbfastR numeric ids.

CFB_SCHEMA_KEYS = (
    "cfb_player_id", "season", "name", "team", "conference",
    "conference_tier", "class_year", "position", "games",
    "pass_att", "pass_comp", "pass_yds", "pass_td", "int_thrown",
    "sacks_taken", "rush_att", "rush_yds", "rush_td",
    "rec", "targets", "rec_yds", "rec_td",
    "scrimmage_yds", "scrimmage_td",
)


def row_to_cfb_schema(row: dict) -> Optional[dict]:
    """Convert one merged player-season row to the cfbfastR schema.

    Returns ``None`` if the row isn't a skill-position player after
    normalization (e.g. linebacker showed up because they had a
    receiving TD on a fumble return — we drop them).
    """
    pos = normalize_position(row.get("pos"))
    if pos is None:
        return None

    season = int(row.get("season", 0))
    if season <= 0:
        return None

    conference = row.get("conf_abbr") or row.get("conference") or ""
    tier = classify_conference_tier(conference, season)

    out = {
        "cfb_player_id": f"sr_{row['sr_slug']}",
        "season": season,
        "name": row.get("player_name") or _slug_to_name(row["sr_slug"]),
        "team": row.get("team_name_abbr") or row.get("team") or "",
        "conference": conference,
        "conference_tier": tier,
        "class_year": row.get("class") or None,
        "position": pos,
        "games": _to_int(row.get("games")),
        "pass_att": _to_int(row.get("pass_att")),
        "pass_comp": _to_int(row.get("pass_cmp")),
        "pass_yds": _to_int(row.get("pass_yds")),
        "pass_td": _to_int(row.get("pass_td")),
        "int_thrown": _to_int(row.get("pass_int")),
        "sacks_taken": _to_int(row.get("pass_sacked")),
        "rush_att": _to_int(row.get("rush_att")),
        "rush_yds": _to_int(row.get("rush_yds")),
        "rush_td": _to_int(row.get("rush_td")),
        "rec": _to_int(row.get("rec")),
        "targets": _to_int(row.get("targets")),  # usually None pre-2014
        "rec_yds": _to_int(row.get("rec_yds")),
        "rec_td": _to_int(row.get("rec_td")),
        "scrimmage_yds": _to_int(
            row.get("yds_from_scrimmage") or row.get("scrim_yds")
        ),
        "scrimmage_td": _to_int(row.get("scrim_td")),
    }
    return out


def _slug_to_name(slug: str) -> str:
    """Best-effort fallback: ``"tim-tebow-1"`` → ``"Tim Tebow"``."""
    parts = slug.split("-")
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts)
