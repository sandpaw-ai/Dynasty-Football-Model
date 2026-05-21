"""College -> NFL bridge (PR #16 -- rookie similarity chain).

Map every NCAA player-season in the cached NCAA corpus to the NFL PFR
player it became (if any). The bridge enables the rookie similarity chain
to project a college prospect's NFL career by:

  1. Finding the prospect's nearest college-comparable seasons.
  2. Resolving each comp's bridge entry -> NFL player id.
  3. Aggregating that NFL career's realized fantasy production.

Match strategy (in priority order):

  * (name, college, rookie_season +/- 1yr) -- strongest match. Uses
    ``last_college_season + 1 == rookie_season`` heuristic. NFL PFR
    ``college_name`` is normalized with the same rules as NCAA team names.
  * (name, rookie_season +/- 1yr) -- fallback when school strings disagree
    (e.g. "USC" vs "Southern California"). Conservative: skipped when
    multiple NFL candidates share the name.
  * Unmatched college seasons get ``nfl_pfr_player_id = None``, indicating
    the player did not reach a rosterable NFL career. They still
    contribute to longevity statistics (as "out-of-NFL-after-college").

Output: ``data/bridge/ncaa_to_nfl.json``, keyed by ``cfb_player_id``.

The bridge is committed to the repo. Rebuild it with:

    python -m dynasty.similarity.bridge --build
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from ..sources.historical_ncaa_football import load_ncaa_seasons
from ..sources.pro_football_reference import load_pfr_players, PLAYERS_GZ


_REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = _REPO_ROOT / "data" / "bridge"
BRIDGE_PATH = BRIDGE_DIR / "ncaa_to_nfl.json"

# School-name normalization -- maps cfbfastR's marketing names to PFR's
# college_name strings. NFL teams using "USC" vs PFR's "Southern California"
# etc. are the failure modes we have to bridge.
_SCHOOL_ALIASES = {
    "USC": "Southern California",
    "UCF": "Central Florida",
    "Pitt": "Pittsburgh",
    "Ole Miss": "Mississippi",
    "Miami (OH)": "Miami (Ohio)",
    "Miami": "Miami (FL)",
    "Hawai'i": "Hawaii",
    "App State": "Appalachian State",
    "Louisiana": "Louisiana-Lafayette",
    "NC State": "North Carolina State",
    "BYU": "Brigham Young",
    "SMU": "Southern Methodist",
    "TCU": "Texas Christian",
    "FIU": "Florida International",
    "FAU": "Florida Atlantic",
    "UAB": "Alabama-Birmingham",
    "UTSA": "Texas-San Antonio",
    "UTEP": "Texas-El Paso",
    "UMass": "Massachusetts",
    "UConn": "Connecticut",
    "Cal": "California",
    "LSU": "Louisiana State",
    "BC": "Boston College",
    "VT": "Virginia Tech",
}


def _normalize_name(s: str) -> str:
    """Normalize a player name for fuzzy matching.

    Lowercase, strip punctuation/jr/sr/iii, collapse whitespace.
    """
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[.,'`\-_]+", "", s)
    # also strip curly apostrophes
    s = s.replace("\u2019", "")
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_school(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    if s in _SCHOOL_ALIASES:
        s = _SCHOOL_ALIASES[s]
    return s.lower()


def _intish(v) -> Optional[int]:
    if v in (None, "", "NA"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _last_first_initial(nname: str) -> str:
    """From a normalized full name, return 'last firstinitial' (e.g.
    'trubisky m' for 'mitchell trubisky'). Used for nickname-tolerant
    fallback matching (Mitch <-> Mitchell, Bobby <-> Robert, etc.)."""
    parts = nname.split()
    if len(parts) < 2:
        return nname
    return f"{parts[-1]} {parts[0][0]}"


def _index_nfl_players() -> dict:
    """Return indices over PFR players keyed by (normname, normschool) and
    by (normname, rookie_season)."""
    players = load_pfr_players()
    by_name_school: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_name_rookie: dict[tuple[str, int], list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    by_lastinit_school: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in players:
        pos = (p.get("position") or "").upper()
        if pos not in ("QB", "RB", "WR", "TE", "FB"):
            continue
        if not p.get("gsis_id"):
            continue
        nname = _normalize_name(p.get("display_name") or "")
        nschool = _normalize_school(p.get("college_name") or "")
        rs = _intish(p.get("rookie_season"))
        if nname and nschool:
            by_name_school[(nname, nschool)].append(p)
        if nname and rs is not None:
            by_name_rookie[(nname, rs)].append(p)
        if nname:
            by_name[nname].append(p)
        # Last name + first initial + school -- nickname tolerant fallback.
        if nname and nschool:
            li = _last_first_initial(nname)
            if li != nname:
                by_lastinit_school[(li, nschool)].append(p)
    return {
        "by_name_school": by_name_school,
        "by_name_rookie": by_name_rookie,
        "by_name": by_name,
        "by_lastinit_school": by_lastinit_school,
    }


def build_bridge() -> dict:
    """Build {cfb_player_id: bridge_entry} mapping over all NCAA seasons.

    Returns a summary dict and (as a side-effect) writes the bridge to
    ``data/bridge/ncaa_to_nfl.json``.
    """
    if not PLAYERS_GZ.exists():
        raise RuntimeError(
            "PFR players cache missing; cannot build bridge. "
            "Run with DYNASTY_FB_PFR_LIVE=1 first."
        )

    ncaa = load_ncaa_seasons()
    if not ncaa:
        raise RuntimeError(
            "NCAA corpus empty; cannot build bridge. "
            "Run with DYNASTY_FB_NCAA_LIVE=1 first."
        )

    idx = _index_nfl_players()

    # Collapse the per-season NCAA rows into one record per cfb_player_id,
    # taking the most recent college season as ``last_college_season``.
    per_player: dict[str, dict] = {}
    for row in ncaa:
        pid = row.get("cfb_player_id") or ""
        if not pid:
            continue
        existing = per_player.get(pid)
        if not existing or row["season"] > existing["last_college_season"]:
            per_player[pid] = {
                "name": row.get("name") or "",
                "college": row.get("team") or "",
                "last_college_season": int(row["season"]),
            }

    bridge: dict[str, dict] = {}
    counts = {"name+college": 0, "name+season": 0, "unmatched": 0}

    for pid, info in per_player.items():
        nname = _normalize_name(info["name"])
        nschool = _normalize_school(info["college"])
        last_season = info["last_college_season"]
        # Plausible rookie seasons: last_college_season + 1 or + 2 (redshirts).
        # Also allow same-year (rare; early entrant).
        plausible_rookie = {last_season, last_season + 1, last_season + 2}

        match = None
        strategy = "unmatched"

        # Strategy 1: (name, school) -- strongest signal.
        cands = idx["by_name_school"].get((nname, nschool), [])
        cands = [
            p for p in cands
            if (_intish(p.get("rookie_season")) or 0) in plausible_rookie
        ]
        if len(cands) == 1:
            match = cands[0]
            strategy = "name+college"
        elif len(cands) > 1:
            cands.sort(
                key=lambda p: abs(
                    (_intish(p.get("rookie_season")) or 0) - (last_season + 1)
                )
            )
            match = cands[0]
            strategy = "name+college"

        # Strategy 2: (name, rookie_season +/-1). Conservative.
        if match is None:
            cands = []
            for rs in plausible_rookie:
                cands.extend(idx["by_name_rookie"].get((nname, rs), []))
            seen = set()
            dedup = []
            for p in cands:
                gid = p.get("gsis_id")
                if gid in seen:
                    continue
                seen.add(gid)
                dedup.append(p)
            if len(dedup) == 1:
                match = dedup[0]
                strategy = "name+season"

        # Strategy 3: (last_name + first_initial, school) within plausible
        # rookie window. Catches Mitch <-> Mitchell, Bobby <-> Robert,
        # etc.
        if match is None:
            li = _last_first_initial(nname)
            cands = idx["by_lastinit_school"].get((li, nschool), [])
            cands = [
                p for p in cands
                if (_intish(p.get("rookie_season")) or 0) in plausible_rookie
            ]
            if len(cands) == 1:
                match = cands[0]
                strategy = "name+college"  # bucket with name+college (it IS college-keyed)

        if match is not None:
            entry = {
                "nfl_pfr_player_id": match.get("gsis_id"),
                "nfl_display_name": match.get("display_name"),
                "nfl_position": (match.get("position") or "").upper(),
                "last_college_season": last_season,
                "draft_year": _intish(match.get("draft_year")),
                "college": info["college"],
                "match_strategy": strategy,
            }
            counts[strategy] += 1
        else:
            entry = {
                "nfl_pfr_player_id": None,
                "nfl_display_name": None,
                "nfl_position": None,
                "last_college_season": last_season,
                "draft_year": None,
                "college": info["college"],
                "match_strategy": "unmatched",
            }
            counts["unmatched"] += 1

        bridge[pid] = entry

    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE_PATH.write_text(json.dumps(bridge, indent=1))

    return {
        "bridge_size": len(bridge),
        "counts": counts,
        "path": str(BRIDGE_PATH),
    }


def load_bridge() -> dict:
    if not BRIDGE_PATH.exists():
        return {}
    try:
        return json.loads(BRIDGE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def coverage_summary(
    bridge: Optional[dict] = None,
    min_rookie_season: int = 2017,
    max_rookie_season: int = 2025,
) -> dict:
    """How many recent NFL skill players have a matched NCAA bridge entry?

    The default window is ``rookie_season in [2017, 2025]``. We start at
    2017 because cfbfastR's PBP coverage only starts in 2014, so a 2014
    rookie's full college career predates the corpus; 2015 and 2016 rookies
    have only 1-2 college seasons in the corpus, which biases coverage
    artificially low. From 2017 onwards a player's full 3-4 year college
    arc is captured.

    Players whose listed ``college_name`` is *not FBS* (FCS / D-II / D-III /
    non-football-school) are also excluded from the denominator -- cfbfastR
    only tracks FBS so those are corpus-out-of-scope, not bridge failures.
    """
    bridge = bridge or load_bridge()
    matched_nfl_pids = {
        e["nfl_pfr_player_id"] for e in bridge.values() if e.get("nfl_pfr_player_id")
    }

    # Build set of schools known to cfbfastR (i.e. they appear in the NCAA
    # corpus at least once). Any player whose college is outside this set
    # is an FBS-coverage gap, not a bridge bug.
    ncaa = load_ncaa_seasons()
    fbs_schools_normalized = {_normalize_school(r.get("team", "")) for r in ncaa}

    nfl = load_pfr_players()
    candidates = []
    excluded_non_fbs = 0
    for p in nfl:
        pos = (p.get("position") or "").upper()
        if pos not in ("QB", "RB", "WR", "TE", "FB"):
            continue
        try:
            rs = int(p.get("rookie_season") or 0)
        except ValueError:
            continue
        if rs < min_rookie_season or rs > max_rookie_season:
            continue
        college = p.get("college_name") or ""
        if _normalize_school(college) not in fbs_schools_normalized:
            excluded_non_fbs += 1
            continue
        candidates.append(p.get("gsis_id"))
    if not candidates:
        return {
            "coverage_pct": 0.0,
            "n_candidates": 0,
            "n_matched": 0,
            "excluded_non_fbs": excluded_non_fbs,
        }
    matched = sum(1 for gid in candidates if gid in matched_nfl_pids)
    return {
        "coverage_pct": round(100.0 * matched / len(candidates), 2),
        "n_candidates": len(candidates),
        "n_matched": matched,
        "excluded_non_fbs": excluded_non_fbs,
    }


def _cli() -> int:
    if "--build" in sys.argv:
        summary = build_bridge()
        print(json.dumps(summary, indent=2))
        cov = coverage_summary()
        print(json.dumps({"coverage": cov}, indent=2))
        return 0
    if "--coverage" in sys.argv:
        cov = coverage_summary()
        print(json.dumps(cov, indent=2))
        return 0
    print("Usage: python -m dynasty.similarity.bridge [--build|--coverage]")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
