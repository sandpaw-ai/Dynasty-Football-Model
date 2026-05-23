"""Sports-Reference CFB standings scraper (v3.0 PR 2 SOS corpus).

The standings page at ``cfb/years/{YYYY}-standings.html`` exposes per-team
Strength-of-Schedule (``data-stat="sos"``) and Simple Rating System
(``data-stat="srs"``) values. Cloudflare blocks scripted requests against
the live site, so — same pattern as the v2.4 PFR scraper — we go through
the Internet Archive Wayback Machine, which mirrors the full
``data-stat``-annotated HTML and is friendly to a polite scraper.

Public surface:

* :func:`fetch_standings(year)`  → raw HTML string (cached on disk)
* :func:`parse_standings(html, year)` → ``list[dict]`` of per-team rows

Cache layout:

    data/sr_cache/standings/{year}.html

The cache is **shared** with v3.0 PR 1 — the PR 1 scraper is instructed
to pre-populate the same files even though it doesn't consume them.
``fetch_standings`` reads the cache before going to the network, so the
two PRs cooperate without coordination.

User-Agent: ``sandpaw-dynasty-model/3.0
(+https://pstiehl.github.io/Dynasty-Football-Model/)``.
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

# Empirical Wayback ceiling is ~10-15 req/min; v2.4 PR 1 learnt this the
# hard way. We default to a 4s gap with exponential backoff up to ~3 min.
# Years 2000-2025 = 26 pages, so total runtime is ~3-5 minutes worst case.
MIN_REQUEST_INTERVAL_SEC = float(
    os.environ.get("SR_SCRAPER_INTERVAL_SEC", "4.0")
)

# Snapshot fallback order, per the v2.4 lesson: 2024 → 2023 → 2025 → 2022.
WAYBACK_TIMESTAMPS = ("2024", "2023", "2025", "2022")

# We use the HTTP variant of web.archive.org because the HTTPS host is
# unreachable from some sandboxed network environments while the HTTP one
# (which 302s into the archived snapshot) works. Wayback itself enforces
# whatever scheme the *snapshot* used.
SR_BASE = "https://www.sports-reference.com"


# Cache dir is shared with v3.0 PR 1. Resolves relative to this file so
# the module works whether invoked from the repo root or a script dir.
_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "data" / "sr_cache" / "standings"


# Module-level shared throttle state.
_last_request_at: float = 0.0


# ---------------------------------------------------------------------------
# HTTP / cache plumbing
# ---------------------------------------------------------------------------

def _throttle() -> None:
    """Block until at least MIN_REQUEST_INTERVAL_SEC has passed."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_SEC:
        time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
    _last_request_at = time.monotonic()


def _http_get(url: str, *, max_retries: int = 5, timeout: int = 60) -> str:
    """GET with throttle + exponential backoff on 4xx/5xx + connection errors.

    Wayback's rate-limit response often manifests as a TCP refusal (edge
    node simply stops accepting) or a stale 403/429. We treat those the
    same as 5xx and back off aggressively (up to ~3 min).
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
                "SR fetch failed (status=%s) for %s (attempt %d/%d)",
                resp.status_code, url, attempt + 1, max_retries,
            )
            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
        except requests.RequestException as exc:
            last_exc = exc
            log.warning(
                "SR fetch exception for %s (attempt %d/%d): %s",
                url, attempt + 1, max_retries, exc,
            )
        if attempt < max_retries - 1:
            sleep_s = backoff + random.uniform(0, backoff * 0.25)
            log.info("  sleeping %.1fs before retry", sleep_s)
            time.sleep(sleep_s)
            backoff = min(backoff * 2, 180.0)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"SR fetch exhausted retries for {url}")


def _wayback_url(timestamp: str, sr_path: str) -> str:
    """Build a Wayback Machine URL for a sports-reference path.

    We use the HTTP variant of web.archive.org (Wayback issues a 302 to
    the actual snapshot timestamp). HTTPS resolves to the same content
    but is not universally reachable from sandboxed environments.
    """
    return f"http://web.archive.org/web/{timestamp}/{SR_BASE}{sr_path}"


def _http_get_with_timestamp_fallback(sr_path: str) -> str:
    """Try each Wayback timestamp until one returns 200."""
    last_exc: Optional[Exception] = None
    for ts in WAYBACK_TIMESTAMPS:
        url = _wayback_url(ts, sr_path)
        try:
            return _http_get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Wayback timestamp %s failed for %s: %s", ts, sr_path, exc,
            )
            last_exc = exc
            # Brief cooldown before trying next timestamp.
            time.sleep(10.0)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

def _standings_cache_path(year: int) -> Path:
    return CACHE_DIR / f"{year}.html"


def fetch_standings(year: int) -> str:
    """Fetch HTML for one season's standings page (Wayback-backed, cached).

    Cached at ``data/sr_cache/standings/{year}.html``. Re-runs that hit
    the cache do not make network requests. The cache is shared with
    v3.0 PR 1's scraper.
    """
    cache_path = _standings_cache_path(year)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")

    log.info("Fetching SR standings %d from Wayback", year)
    html = _http_get_with_timestamp_fallback(
        f"/cfb/years/{year}-standings.html"
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Pre-pended rank markers like "(7) Alabama" appear in some other SR
# tables but the standings table uses bare school names; strip
# defensively anyway.
_RANK_PREFIX_RE = re.compile(r"^\s*\(\d+\)\s*")

# Wayback's archived links are namespaced under /web/{snapshot}/<orig>.
# Strip that prefix to recover the canonical SR href.
_WAYBACK_PREFIX_RE = re.compile(r"^/web/\d+(?:[a-z_]+)?/")

# /cfb/schools/<slug>/<year>.html
_SCHOOL_HREF_RE = re.compile(
    r"/cfb/schools/(?P<slug>[a-z0-9\-]+)/(?P<year>\d{4})\.html",
    re.IGNORECASE,
)


def _to_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _clean_school_name(raw: str) -> str:
    """Strip in-name ranking prefix and surrounding whitespace."""
    return _RANK_PREFIX_RE.sub("", raw or "").strip()


def _extract_slug(school_cell) -> Optional[str]:
    """Pull the canonical sports-reference school slug from the <a href>."""
    if school_cell is None:
        return None
    a = school_cell.find("a")
    if not a:
        return None
    href = a.get("href") or ""
    # Strip Wayback prefix if present.
    href = _WAYBACK_PREFIX_RE.sub("/", href)
    m = _SCHOOL_HREF_RE.search(href)
    if m:
        return m.group("slug").lower()
    return None


def _strip_division(conf_abbr: str) -> str:
    """Strip the parenthesized division suffix from a conf_abbr.

    Examples:
        "ACC(Atlantic)" -> "ACC"
        "SEC(West)"     -> "SEC"
        "Big 12(South)" -> "Big 12"
        "American"      -> "American"
    """
    s = (conf_abbr or "").strip()
    paren = s.find("(")
    if paren >= 0:
        s = s[:paren]
    return s.strip()


# Map sports-reference conference abbreviations to the canonical full
# names used by the existing cfbfastR-derived corpus under
# data/historical_ncaa_football/season_*.json.
#
# Sports-reference uses its own abbreviation style (CUSA, MWC, Ind,
# Pac-10, Pac-12, etc.). We map to the cfbfastR name space so downstream
# code can join the two corpora on `conference`.
_SR_CONF_NORMALIZE = {
    "ACC": "ACC",
    "American": "American Athletic",
    "AAC": "American Athletic",
    "Big East": "Big East",
    "Big Ten": "Big Ten",
    "Big 12": "Big 12",
    "Big West": "Big West",
    "CUSA": "Conference USA",
    "C-USA": "Conference USA",
    "Ind": "FBS Independents",
    "MAC": "Mid-American",
    "MWC": "Mountain West",
    "Pac-10": "Pac-10",
    "Pac-12": "Pac-12",
    "SEC": "SEC",
    "Sun Belt": "Sun Belt",
    "WAC": "WAC",
}

# Conference → tier mapping matches the existing classifications baked
# into data/historical_ncaa_football/season_*.json. For pre-2014 we
# preserve the league name *as it existed that year* (Pac-10, Big East,
# WAC). Tiering is derived from auto-bid + revenue posture at the time:
#
#   P5  - power-five (with their period-correct names)
#   G5_top - upper non-power group (American, MWC, Sun Belt post-2014)
#   G5  - lower non-power (MAC, CUSA, etc.)
#   FCS - sub-FBS
_CANONICAL_CONF_TIER = {
    # P5 / historical equivalents
    "ACC": "P5",
    "Big 12": "P5",
    "Big East": "P5",  # FBS-era Big East (pre-2013 split)
    "Big Ten": "P5",
    "Pac-10": "P5",
    "Pac-12": "P5",
    "SEC": "P5",
    "FBS Independents": "P5",  # Notre Dame, BYU pre-2023, etc.

    # G5 top
    "American Athletic": "G5_top",
    "Mountain West": "G5_top",
    "Sun Belt": "G5_top",

    # G5
    "Conference USA": "G5",
    "Mid-American": "G5",
    "WAC": "G5",      # WAC pre-2013 split, FBS group of five
    "Big West": "G5", # Big West football pre-2001 dissolution

    # FCS conferences seen in standings (when SR includes them)
    "Big Sky": "FCS",
    "Big South": "FCS",
    "Big South-OVC": "FCS",
    "CAA": "FCS",
    "FCS Independents": "FCS",
    "Ivy": "FCS",
    "MEAC": "FCS",
    "MVFC": "FCS",
    "NEC": "FCS",
    "Patriot": "FCS",
    "Pioneer": "FCS",
    "Southern": "FCS",
    "Southland": "FCS",
    "SWAC": "FCS",
    "UAC": "FCS",
    "OVC": "FCS",
    "Big South-OVC": "FCS",
}


def _normalize_conference(conf_abbr: str) -> str:
    """Convert a sports-reference conf_abbr to the cfbfastR canonical name.

    Strips division parens and maps the abbreviation. Returns the input
    unchanged if it's not in the lookup (defensive: lets us surface new
    conferences without silently corrupting).
    """
    stripped = _strip_division(conf_abbr)
    return _SR_CONF_NORMALIZE.get(stripped, stripped)


def _conference_tier(canonical_conf: str) -> Optional[str]:
    """Return the P5 / G5_top / G5 / FCS tier for a canonical conference name."""
    return _CANONICAL_CONF_TIER.get(canonical_conf)


def parse_standings(html: str, year: int) -> list[dict]:
    """Parse one season's standings table to per-team row dicts.

    Each row dict carries:

        year, school, school_canonical_slug, conference, conference_tier,
        wins, losses, srs, sos

    ``srs_rank`` and ``sos_rank`` are *not* set here — they're computed
    in the corpus builder (where we have the full year cohort).

    Missing SOS / SRS values come back as ``None`` (e.g., partial-FCS
    rows in some years).
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="standings")
    if table is None:
        log.warning("no <table id=\"standings\"> in SR %d", year)
        return []

    tbody = table.find("tbody")
    if tbody is None:
        return []

    out: list[dict] = []
    for tr in tbody.find_all("tr"):
        cls = tr.get("class") or []
        if "thead" in cls:
            continue

        cells = {}
        for cell in tr.find_all(["th", "td"]):
            stat = cell.get("data-stat")
            if not stat:
                continue
            cells[stat] = cell

        school_cell = cells.get("school_name")
        if school_cell is None:
            continue

        school = _clean_school_name(school_cell.get_text(strip=True))
        if not school:
            continue

        conf_abbr = (cells.get("conf_abbr").get_text(strip=True)
                     if cells.get("conf_abbr") else "")
        canonical_conf = _normalize_conference(conf_abbr)

        row = {
            "year": year,
            "school": school,
            "school_canonical_slug": _extract_slug(school_cell),
            "conference": canonical_conf,
            "conference_tier": _conference_tier(canonical_conf),
            "wins": _to_int(cells["wins"].get_text(strip=True)
                            if "wins" in cells else None),
            "losses": _to_int(cells["losses"].get_text(strip=True)
                              if "losses" in cells else None),
            "srs": _to_float(cells["srs"].get_text(strip=True)
                             if "srs" in cells else None),
            "sos": _to_float(cells["sos"].get_text(strip=True)
                             if "sos" in cells else None),
        }
        out.append(row)

    return out


# ---------------------------------------------------------------------------
# Convenience: combined fetch + parse
# ---------------------------------------------------------------------------

def get_standings(year: int) -> list[dict]:
    """Fetch + parse standings for one year. Cache-aware."""
    return parse_standings(fetch_standings(year), year)
