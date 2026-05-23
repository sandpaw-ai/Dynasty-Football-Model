#!/usr/bin/env python3
"""Build ``data/historical_ncaa_football/season_{2000..2013}.json`` from SR-CFB.

v3.0 PR 1 — extends the existing cfbfastR-derived 2014-2025 corpus
backwards to 2000 using sports-reference.com's CFB sub-site (scraped
via the Wayback Machine; see ``src.dynasty.sources.sports_reference_cfb``).

Two-phase scrape, mirrored after v2.4 PR 1's PFR corpus builder:

  Phase A  Fetch four leaderboards per season for 2000-2013 (passing,
           rushing, receiving, scoring) + the standings page. That's
           14 * 5 = 70 page fetches. Build the player universe from
           these batch tables \u2014 most player-seasons can be filled in
           from leaderboards alone, with position inferred from which
           table they showed up on.

  Phase B  For "priority" slugs \u2014 anyone who appears in the existing
           college\u2192NFL bridge (``data/bridge/ncaa_to_nfl.json``) OR a
           manually-curated high-priority list \u2014 fetch the full player
           page to get the per-season ``pos`` field (handles
           position-shift cases like WR who briefly played QB) and any
           seasons that didn't make the leaderboards.

Output:

  data/historical_ncaa_football/season_2000.json
  data/historical_ncaa_football/season_2001.json
  ...
  data/historical_ncaa_football/season_2013.json
  data/historical_ncaa_football/id_map.json

Run as::

    python scripts/build_pre2014_cfb_corpus.py
    python scripts/build_pre2014_cfb_corpus.py --dry-run  # parse cache only
    python scripts/build_pre2014_cfb_corpus.py --years 2010 2011
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty.sources import sports_reference_cfb as sr  # noqa: E402

log = logging.getLogger("build_pre2014_cfb_corpus")

OUT_DIR = ROOT / "data" / "historical_ncaa_football"
BRIDGE_PATH = ROOT / "data" / "bridge" / "ncaa_to_nfl.json"

DEFAULT_YEARS = list(range(2000, 2014))

# Skill-position threshold (per scope doc \u00a7 10 Q3): if a player\u2019s
# leaderboard rows sum to >= one of these volumes, we keep them in the
# universe even without a player-page fetch. The thresholds are loose
# enough to cover situational rushers / RB-by-committee guys while
# excluding pure special-teams returners.
MIN_RUSH_ATT = 50
MIN_RUSH_YDS = 200
MIN_REC_YDS = 200
MIN_PASS_YDS = 500
MIN_REC = 15


# ---------------------------------------------------------------------------
# Phase A \u2014 leaderboard scrape
# ---------------------------------------------------------------------------

LEADERBOARDS = ("passing", "rushing", "receiving", "scoring")

# When a player shows up *only* in one of these tables and we have no
# player-page position, we infer position from the table itself. This is
# the fallback for players whose careers didn't generate enough NFL
# interest to warrant a player-page fetch.
_TABLE_DEFAULT_POSITION = {
    "passing": "QB",
    "rushing": "RB",
    "receiving": "WR",  # may be TE \u2014 player-page fetch corrects
    "scoring": None,    # ambiguous; never the *only* source
}


def fetch_leaderboards(year: int, *, network: bool) -> dict[str, list[dict]]:
    """Return ``{table_name: parsed_rows}`` for one season's leaderboards."""
    out: dict[str, list[dict]] = {}
    for table in LEADERBOARDS:
        cache_path = sr._leaderboard_cache_path(year, table)
        if not cache_path.exists() and not network:
            log.info("  [dry-run] skip %d %s (no cache)", year, table)
            out[table] = []
            continue
        try:
            html = sr.fetch_year_leaderboard(year, table)
        except Exception as exc:  # noqa: BLE001
            log.error("  failed to fetch %d %s: %s", year, table, exc)
            out[table] = []
            continue
        out[table] = sr.parse_year_leaderboard(html, table, year)
    # Standings: cache-only side-effect for PR 2.
    standings_cache = sr._standings_cache_path(year)
    if not standings_cache.exists() and network:
        try:
            sr.fetch_year_standings(year)
        except Exception as exc:  # noqa: BLE001
            log.warning("  failed to fetch %d standings: %s", year, exc)
    return out


# ---------------------------------------------------------------------------
# Phase B \u2014 player-page fetch (selective)
# ---------------------------------------------------------------------------


def load_bridge_slugs() -> set[str]:
    """Slugs from ``ncaa_to_nfl.json`` \u2014 priority for player-page fetch.

    The bridge crosswalk uses cfbfastR player ids (numeric) for the
    college side, not SR slugs. But the slugs we discover from
    leaderboards include the player name + 'a-number' suffix; the
    bridge entries we want are *NFL-realized* players. We can't directly
    map without a name lookup.

    For PR 1's purposes we adopt a looser definition of 'priority':
    any leaderboard row whose ``(player_name, team, season)`` could
    plausibly correspond to a bridge entry. The orchestrator handles
    this below via name-match heuristics. So this function returns the
    set of normalized names from the bridge for downstream filtering.
    """
    if not BRIDGE_PATH.exists():
        return set()
    bridge = json.loads(BRIDGE_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for entry in (bridge.values() if isinstance(bridge, dict) else bridge):
        nm = entry.get("nfl_display_name") or entry.get("name") or ""
        if nm:
            names.add(_normalize_name(nm))
    return names


def _normalize_name(name: str) -> str:
    """Lower, strip jr/sr/iii, collapse punctuation."""
    n = name.lower()
    for token in (" jr.", " jr", " sr.", " sr", " iii", " ii", " iv"):
        if n.endswith(token):
            n = n[: -len(token)]
    n = n.replace(".", "").replace("'", "").replace("-", " ")
    return " ".join(n.split())


# ---------------------------------------------------------------------------
# Universe + schema build
# ---------------------------------------------------------------------------


def qualifies(stats: dict) -> bool:
    """Skill-position production threshold (per scope doc \u00a7 10 Q3)."""
    rush_att = stats.get("rush_att") or 0
    rush_yds = stats.get("rush_yds") or 0
    rec = stats.get("rec") or 0
    rec_yds = stats.get("rec_yds") or 0
    pass_yds = stats.get("pass_yds") or 0
    return (
        rush_att >= MIN_RUSH_ATT
        or rush_yds >= MIN_RUSH_YDS
        or rec >= MIN_REC
        or rec_yds >= MIN_REC_YDS
        or pass_yds >= MIN_PASS_YDS
    )


def _coalesce(*vals):
    for v in vals:
        if v not in (None, "", 0):
            return v
    for v in vals:
        if v == 0:
            return 0
    return None


def merge_leaderboard_rows(
    rows_by_table: dict[str, list[dict]],
) -> dict[tuple[str, str], dict]:
    """Merge multi-table leaderboard rows by ``(sr_slug, team)``.

    Returns ``{(slug, team): merged_dict}`` for one season. Position is
    inferred from which leaderboards the slug shows up on (passing
    \u2192 QB, rushing-only \u2192 RB, receiving-only \u2192 WR; ambiguous
    multi-table cases default to the highest-volume table).
    """
    merged: dict[tuple[str, str], dict] = {}

    # First pass: union of all rows.
    for table, rows in rows_by_table.items():
        for r in rows:
            slug = r.get("sr_slug")
            if not slug:
                continue
            team = r.get("team") or r.get("team_name_abbr") or ""
            key = (slug, team)
            bucket = merged.setdefault(
                key,
                {
                    "sr_slug": slug,
                    "season": r.get("season"),
                    "player_name": r.get("player_name") or "",
                    "team": team,
                    "conference": r.get("conference") or r.get("conf_abbr") or "",
                    "_tables_seen": set(),
                    "_inferred_position": None,
                },
            )
            bucket["_tables_seen"].add(table)
            # Pull stat columns we care about. We do NOT clobber
            # existing values \u2014 first non-empty wins (leaderboards
            # usually agree).
            for k in (
                "games", "pass_att", "pass_cmp", "pass_yds", "pass_td",
                "pass_int", "rush_att", "rush_yds", "rush_td",
                "rec", "targets", "rec_yds", "rec_td",
                "yds_from_scrimmage", "scrim_yds", "scrim_td",
            ):
                if k in r and k not in bucket:
                    bucket[k] = r[k]
            # The 'scoring' table uses 'rec_td' / 'rush_td' too; harmless.
            # Keep player_name if we discover a better version.
            if not bucket["player_name"] and r.get("player_name"):
                bucket["player_name"] = r["player_name"]
            if not bucket["conference"] and r.get("conference"):
                bucket["conference"] = r["conference"]

    # Second pass: infer position from tables seen.
    for bucket in merged.values():
        tables = bucket["_tables_seen"]
        if "passing" in tables:
            # Anyone on the passing leaderboard with significant volume
            # is a QB. Player-page fetch will refine for WRs / RBs who
            # threw the occasional pass (rare on the leaderboard since
            # it filters by Att).
            bucket["_inferred_position"] = "QB"
        elif "rushing" in tables and "receiving" in tables:
            # Both tables \u2014 use volume to decide. More rush att than
            # 2x rec \u2192 RB. Otherwise WR (we treat hybrid SLOT/WR as WR).
            rush_att = _to_int_safe(bucket.get("rush_att"))
            rec = _to_int_safe(bucket.get("rec"))
            if rush_att >= 2 * rec:
                bucket["_inferred_position"] = "RB"
            else:
                bucket["_inferred_position"] = "WR"
        elif "rushing" in tables:
            bucket["_inferred_position"] = "RB"
        elif "receiving" in tables:
            bucket["_inferred_position"] = "WR"
        else:
            # Only scoring \u2014 ambiguous; will be dropped unless we get
            # a player-page hit.
            bucket["_inferred_position"] = None

    return merged


def _to_int_safe(v) -> int:
    """Best-effort int conversion that returns 0 on missing/garbage."""
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def _coalesce_int(*vals) -> Optional[int]:
    """Pick the first stringy/number value that converts to int, or None."""
    for v in vals:
        if v in (None, ""):
            continue
        try:
            return int(float(str(v).replace(",", "")))
        except (ValueError, TypeError):
            continue
    return None


def leaderboard_row_to_cfb_schema(
    bucket: dict, *, season: int
) -> Optional[dict]:
    """Convert one merged leaderboard bucket to the cfbfastR schema.

    Falls back to the table-inferred position when the row didn't get a
    player-page fetch. Returns ``None`` if no position could be
    inferred (skip) or if the row falls below the skill-position
    volume threshold.
    """
    stats = {
        "rush_att": _coalesce_int(bucket.get("rush_att")),
        "rush_yds": _coalesce_int(bucket.get("rush_yds")),
        "rec": _coalesce_int(bucket.get("rec")),
        "rec_yds": _coalesce_int(bucket.get("rec_yds")),
        "pass_yds": _coalesce_int(bucket.get("pass_yds")),
    }
    if not qualifies(stats):
        return None

    pos = bucket.get("_inferred_position")
    if pos is None:
        return None

    conf = bucket.get("conference") or ""
    tier = sr.classify_conference_tier(conf, season)

    return {
        "cfb_player_id": f"sr_{bucket['sr_slug']}",
        "season": season,
        "name": bucket.get("player_name") or "",
        "team": bucket.get("team") or "",
        "conference": conf,
        "conference_tier": tier,
        "class_year": None,  # not on leaderboards \u2014 player-page only
        "position": pos,
        "games": _coalesce_int(bucket.get("games")),
        "pass_att": _coalesce_int(bucket.get("pass_att")),
        "pass_comp": _coalesce_int(bucket.get("pass_cmp")),
        "pass_yds": _coalesce_int(bucket.get("pass_yds")),
        "pass_td": _coalesce_int(bucket.get("pass_td")),
        "int_thrown": _coalesce_int(bucket.get("pass_int")),
        "sacks_taken": None,
        "rush_att": _coalesce_int(bucket.get("rush_att")),
        "rush_yds": _coalesce_int(bucket.get("rush_yds")),
        "rush_td": _coalesce_int(bucket.get("rush_td")),
        "rec": _coalesce_int(bucket.get("rec")),
        "targets": _coalesce_int(bucket.get("targets")),
        "rec_yds": _coalesce_int(bucket.get("rec_yds")),
        "rec_td": _coalesce_int(bucket.get("rec_td")),
        "scrimmage_yds": _coalesce_int(
            bucket.get("yds_from_scrimmage"),
            bucket.get("scrim_yds"),
        ),
        "scrimmage_td": _coalesce_int(bucket.get("scrim_td")),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def select_priority_slugs(
    merged_by_year: dict[int, dict[tuple[str, str], dict]],
    bridge_names: set[str],
    *,
    max_pages: int,
) -> list[str]:
    """Pick slugs whose player pages we'll fetch in Phase B.

    Strategy (in order, up to ``max_pages`` total):

    1. Any slug whose normalized player_name matches a bridge entry
       (i.e., the player made the NFL post-2000).
    2. Any slug that put up *Heisman-level* numbers in any single season
       (rush_yds >= 1400, rec_yds >= 1100, or pass_yds >= 3500) \u2014
       these are the prospects worth a full career arc.
    3. Any slug that showed up in 3+ different seasons across our window
       (multi-year college careers; comp value).

    Returns a deduplicated list, ordered by phase.
    """
    seen: set[str] = set()
    out: list[str] = []

    # Tier 1: bridge name match
    for year, merged in merged_by_year.items():
        for (slug, _team), bucket in merged.items():
            if slug in seen:
                continue
            name_norm = _normalize_name(bucket.get("player_name") or "")
            if name_norm and name_norm in bridge_names:
                out.append(slug)
                seen.add(slug)
                if len(out) >= max_pages:
                    return out

    # Tier 2: elite single-season production
    for year, merged in merged_by_year.items():
        for (slug, _team), bucket in merged.items():
            if slug in seen:
                continue
            rush = _to_int_safe(bucket.get("rush_yds"))
            rec_y = _to_int_safe(bucket.get("rec_yds"))
            pass_y = _to_int_safe(bucket.get("pass_yds"))
            if rush >= 1400 or rec_y >= 1100 or pass_y >= 3500:
                out.append(slug)
                seen.add(slug)
                if len(out) >= max_pages:
                    return out

    # Tier 3: multi-year careers
    career_counts: dict[str, int] = defaultdict(int)
    for year, merged in merged_by_year.items():
        slugs_this_year = {slug for (slug, _) in merged.keys()}
        for s in slugs_this_year:
            career_counts[s] += 1
    for slug, cnt in sorted(
        career_counts.items(), key=lambda kv: -kv[1]
    ):
        if cnt < 3 or slug in seen:
            continue
        out.append(slug)
        seen.add(slug)
        if len(out) >= max_pages:
            return out

    return out


def overlay_player_pages(
    merged_by_year: dict[int, dict[tuple[str, str], dict]],
    priority_slugs: list[str],
    *,
    network: bool,
) -> tuple[int, int]:
    """Fetch + parse player pages for ``priority_slugs`` and overlay onto buckets.

    Returns ``(fetched_count, applied_count)``.
    """
    fetched = 0
    applied = 0
    for slug in priority_slugs:
        cache_path = sr._player_cache_path(slug)
        if not cache_path.exists() and not network:
            continue
        try:
            html = sr.fetch_player_page(slug)
        except Exception as exc:  # noqa: BLE001
            log.warning("  player-page fetch failed for %s: %s", slug, exc)
            continue
        fetched += 1
        try:
            rows = sr.parse_player_page(html, slug)
        except Exception as exc:  # noqa: BLE001
            log.warning("  player-page parse failed for %s: %s", slug, exc)
            continue
        for r in rows:
            season = int(r.get("season", 0))
            team = r.get("team_name_abbr", "")
            year_buckets = merged_by_year.get(season)
            if year_buckets is None:
                continue
            key = (slug, team)
            bucket = year_buckets.get(key)
            # If the leaderboard didn't have this (slug, team) row,
            # insert a new bucket from the player page (handles seasons
            # where the player didn't crack the leaderboard).
            if bucket is None:
                year_buckets[key] = {
                    "sr_slug": slug,
                    "season": season,
                    "player_name": sr._slug_to_name(slug),
                    "team": team,
                    "conference": r.get("conf_abbr", ""),
                    "_tables_seen": set(),
                    "_inferred_position": sr.normalize_position(r.get("pos")),
                }
                bucket = year_buckets[key]
            # Refine position from the per-row 'pos' field.
            ppos = sr.normalize_position(r.get("pos"))
            if ppos:
                bucket["_inferred_position"] = ppos
            # Fill in class_year + any missing stat columns.
            if r.get("class") and not bucket.get("class"):
                bucket["class"] = r["class"]
            for k in (
                "games", "pass_att", "pass_cmp", "pass_yds", "pass_td",
                "pass_int", "rush_att", "rush_yds", "rush_td",
                "rec", "targets", "rec_yds", "rec_td",
                "yds_from_scrimmage", "scrim_yds", "scrim_td",
            ):
                if k in r and not bucket.get(k):
                    bucket[k] = r[k]
            applied += 1
    return fetched, applied


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="*", default=DEFAULT_YEARS)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse only from existing cache; no network fetches.",
    )
    parser.add_argument(
        "--max-player-pages", type=int, default=4000,
        help="Cap on Phase-B player-page fetches (default 4000).",
    )
    parser.add_argument(
        "--skip-player-pages", action="store_true",
        help="Phase A only \u2014 leaderboards alone, no player pages.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    network = not args.dry_run
    years = sorted(set(args.years))
    log.info("Building corpus for years: %s (network=%s)", years, network)

    # Phase A
    log.info("=== Phase A: leaderboards ===")
    merged_by_year: dict[int, dict[tuple[str, str], dict]] = {}
    for year in years:
        log.info("  year %d ...", year)
        leaderboards = fetch_leaderboards(year, network=network)
        merged = merge_leaderboard_rows(leaderboards)
        merged_by_year[year] = merged
        log.info("    %d (slug, team) buckets", len(merged))

    # Phase B
    if not args.skip_player_pages:
        log.info("=== Phase B: player pages ===")
        bridge_names = load_bridge_slugs()
        log.info("  bridge has %d unique normalized names", len(bridge_names))
        priority_slugs = select_priority_slugs(
            merged_by_year, bridge_names, max_pages=args.max_player_pages,
        )
        log.info("  selected %d priority slugs for player-page fetch",
                 len(priority_slugs))
        fetched, applied = overlay_player_pages(
            merged_by_year, priority_slugs, network=network,
        )
        log.info(
            "  fetched %d player pages, applied %d season overlays",
            fetched, applied,
        )

    # Schema-convert + write
    log.info("=== Writing season files ===")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    rows_per_year: dict[int, int] = {}
    id_map: dict[str, str] = {}
    for year in years:
        out_path = OUT_DIR / f"season_{year}.json"
        # Safety guard: never overwrite 2014+ files (the cfbfastR ones).
        if year >= 2014:
            log.warning("Skipping %s (would overwrite cfbfastR corpus)", out_path)
            continue
        rows: list[dict] = []
        for (slug, team), bucket in merged_by_year[year].items():
            schema_row = leaderboard_row_to_cfb_schema(bucket, season=year)
            if schema_row is None:
                continue
            rows.append(schema_row)
            id_map[schema_row["cfb_player_id"]] = slug
        rows.sort(key=lambda r: (r["position"], -(r.get("scrimmage_yds") or 0),
                                  -(r.get("pass_yds") or 0), r["name"]))
        out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        log.info("  %s -> %d rows", out_path.name, len(rows))
        total_rows += len(rows)
        rows_per_year[year] = len(rows)

    # id_map.json \u2014 PR 2/3 will extend this with the cfbfastR\u2194SR
    # mapping for overlap years (2014+). PR 1 establishes just the SR
    # half of the map.
    id_map_path = OUT_DIR / "id_map.json"
    if id_map_path.exists():
        # Merge with existing without clobbering.
        existing = json.loads(id_map_path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            for k, v in existing.items():
                id_map.setdefault(k, v)
    id_map_path.write_text(
        json.dumps(id_map, indent=2, sort_keys=True), encoding="utf-8"
    )
    log.info("  id_map.json -> %d entries", len(id_map))

    log.info("=== Summary ===")
    log.info("  total rows: %d", total_rows)
    for year in sorted(rows_per_year):
        log.info("    %d: %d rows", year, rows_per_year[year])

    return 0


if __name__ == "__main__":
    sys.exit(main())
