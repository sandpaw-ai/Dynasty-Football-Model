#!/usr/bin/env python3
"""v3.0 PR 4 — orchestrate the prospect projection layer.

Reads (or rebuilds) the PR 3 prospect corpus, finds top-25 comps for
every recent draft-class prospect (skill positions only), projects an
NFL career arc from comp NFL careers (similarity-weighted), and writes
JSON artifacts that PR 6's UI consumes.

Draft-class convention (Phil's wording in the brief):
    draft_class_year = last_college_season + 1
The brief asked for draft classes 2022..2026 inclusive, which maps to
last-college-season ∈ {2021, 2022, 2023, 2024, 2025}.

Outputs:
    data/engine_v3/prospects_<draft_class>.json     (one per class)
    data/engine_v3/prospects_all.json               (aggregated)

Usage::

    PYTHONPATH=src python3 scripts/build_prospects_v3.py \
        [--corpus data/engine_v3/prospect_corpus.json.gz] \
        [--bridge data/bridge/ncaa_to_nfl.json] \
        [--ktc    data/consensus/ktc_latest.json] \
        [--nfl    data/nflverse/player_stats_season.csv.gz] \
        [--out-dir data/engine_v3] \
        [--classes 2022,2023,2024,2025,2026] \
        [--top-k 25]

Idempotent: re-running with the same inputs is a no-op overwrite that
produces byte-identical artifacts when KTC + corpus haven't changed.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import re
import statistics
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from dynasty.engine.prospect_similarity import (
    DEFAULT_BRIDGE_FILE,
    DEFAULT_SEASONS_ROOT,
    DEFAULT_SOS_ROOT,
    NameCollisionResolver,
    ProspectVector,
    build_prospect_corpus,
    find_similar_prospects,
)

log = logging.getLogger("build_prospects_v3")

# Skill positions only — mirrors the engine module's invariant.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# Default draft classes (last_season values are these minus 1).
# v3.4: 2027 is the upcoming class (no PFR draft data yet); we source
# it from Tankathon's big board, treating big-board rank as a proxy
# for draft pick.
DEFAULT_DRAFT_CLASSES: Tuple[int, ...] = (2022, 2023, 2024, 2025, 2026, 2027)
FUTURE_CLASSES_FROM_TANKATHON: Tuple[int, ...] = (2027,)

# Hit-label thresholds (PPR per-game over the player's best 3 NFL seasons).
# Calibrated so that historical elites (CMC, Justin Jefferson, Henry, etc.)
# come out "elite" and mid-tier starters land in "starter".
ELITE_PEAK3_FPG = 18.0   # ≥ → elite
STARTER_PEAK3_FPG = 12.0  # ≥ → starter (else bust if ≥ 3 seasons, else unknown)
BUST_MIN_SEASONS = 3      # need at least this many NFL seasons to call bust

# Top-K comps per prospect (PR brief: top-25).
DEFAULT_TOP_K = 25

# v3.4 (Phil 2026-05-28) — NFL draft-pick-tier career_fp baselines
# (Superflex PPR, full career, anchored on what "a player drafted in
# this position+pick-tier historically produces over their first ~5 NFL
# seasons"). Derived from a blend of:
#   * 2022 PFR draft + nflverse career_fp through 2025 (the most
#     complete cohort we have draft+NFL data for)
#   * Published dynasty-rookie career-arc baselines (DLF / RotoViz / DP)
#     for pre-2022 cohorts
# These act as a PRIOR when the comp pool has zero NFL careers (e.g. a
# corpus full of college backups who never played in the NFL). Without
# the prior, the engine projected Fernando Mendoza (1st overall pick,
# 2026) to 1.4 career fp because his college-fp comps were UNC/Iowa
# backups, none of whom played in the NFL. Phil's brief: 'Just pull
# the classes from PFR. No players should appear in the 2026 tab
# unless they are on this link.' Pick-tier baselines anchor every
# drafted player so they appear with a sensible projection.
PICK_TIER_BASELINES_SF_PPR: Dict[Tuple[str, str], float] = {
    # (position, pick_tier) -> projected career fp under Superflex PPR.
    # Tiers: R1_top10, R1, R2, R3, R4, R5_6, R7, UDFA.
    ("QB", "R1_top10"): 3200.0,
    ("QB", "R1"):       2200.0,
    ("QB", "R2"):       1100.0,
    ("QB", "R3"):        600.0,
    ("QB", "R4"):        320.0,
    ("QB", "R5_6"):      170.0,
    ("QB", "R7"):         85.0,
    ("QB", "UDFA"):       40.0,
    ("RB", "R1_top10"): 1600.0,
    ("RB", "R1"):       1200.0,
    ("RB", "R2"):        780.0,
    ("RB", "R3"):        500.0,
    ("RB", "R4"):        320.0,
    ("RB", "R5_6"):      180.0,
    ("RB", "R7"):         95.0,
    ("RB", "UDFA"):       55.0,
    ("WR", "R1_top10"): 1700.0,
    ("WR", "R1"):       1250.0,
    ("WR", "R2"):        820.0,
    ("WR", "R3"):        480.0,
    ("WR", "R4"):        300.0,
    ("WR", "R5_6"):      170.0,
    ("WR", "R7"):         85.0,
    ("WR", "UDFA"):       40.0,
    ("TE", "R1_top10"):  900.0,
    ("TE", "R1"):        700.0,
    ("TE", "R2"):        520.0,
    ("TE", "R3"):        330.0,
    ("TE", "R4"):        210.0,
    ("TE", "R5_6"):      120.0,
    ("TE", "R7"):         60.0,
    ("TE", "UDFA"):       30.0,
}

# Per-pick-tier expected peak-3 fp/g and seasons-in-league (rough
# averages). Used as the prior for the same fields when the comp pool
# is empty.
PICK_TIER_PEAK3_FPG: Dict[str, float] = {
    "R1_top10": 13.5,
    "R1":       11.0,
    "R2":        9.0,
    "R3":        7.5,
    "R4":        6.0,
    "R5_6":      4.5,
    "R7":        3.0,
    "UDFA":      2.0,
}
PICK_TIER_YEARS_IN_LEAGUE: Dict[str, float] = {
    "R1_top10": 7.5,
    "R1":       6.5,
    "R2":       5.5,
    "R3":       4.5,
    "R4":       3.5,
    "R5_6":     2.5,
    "R7":       1.8,
    "UDFA":     1.2,
}


def _pick_tier(pick: Optional[int]) -> str:
    if pick is None:
        return "UDFA"
    if pick <= 10:
        return "R1_top10"
    if pick <= 32:
        return "R1"
    if pick <= 64:
        return "R2"
    if pick <= 100:
        return "R3"
    if pick <= 150:
        return "R4"
    if pick <= 200:
        return "R5_6"
    return "R7"


def _baseline_projection(position: str, pick: Optional[int]) -> Dict[str, float]:
    """Pick-tier-baseline projection used when the comp pool has no
    NFL careers (or to anchor a confidence-blend when comps are thin)."""
    tier = _pick_tier(pick)
    return {
        "projected_career_fp": float(PICK_TIER_BASELINES_SF_PPR.get((position, tier), 100.0)),
        "projected_peak3_fp_pg": float(PICK_TIER_PEAK3_FPG.get(tier, 4.0)),
        "projected_years_in_league": float(PICK_TIER_YEARS_IN_LEAGUE.get(tier, 2.0)),
        "projected_career_fp_stdev": 0.0,
        "n_comps_with_nfl": 0,
        "projection_source": f"pick_tier_baseline_{tier}",
    }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_corpus_from_cache(path: Path) -> List[ProspectVector]:
    """Load the gzipped JSON corpus produced by build_prospect_engine.py.

    Re-hydrates each dict into a ProspectVector dataclass.
    """
    with gzip.open(path, "rb") as f:
        payload = json.loads(f.read().decode("utf-8"))
    out: List[ProspectVector] = []
    for d in payload.get("prospects", []):
        # ProspectVector is frozen; build via constructor.
        out.append(ProspectVector(
            cfb_player_id=d.get("cfb_player_id", ""),
            player_name=d.get("player_name", ""),
            position=d.get("position", ""),
            school_last=d.get("school_last", ""),
            first_season=int(d.get("first_season", 0)),
            last_season=int(d.get("last_season", 0)),
            career_stage_length=int(d.get("career_stage_length", 0)),
            age_at_last_season=float(d.get("age_at_last_season", 0.0)),
            age_inferred=bool(d.get("age_inferred", False)),
            conference_tier_last=d.get("conference_tier_last", "FCS"),
            raw_features=dict(d.get("raw_features") or {}),
            features=dict(d.get("features") or {}),
            notes=list(d.get("notes") or []),
        ))
    return out


def _ensure_corpus(corpus_path: Path, seasons_root: Optional[Path],
                   sos_root: Optional[Path]) -> List[ProspectVector]:
    """Load the corpus from cache; rebuild from seasons if missing."""
    if corpus_path and corpus_path.exists():
        log.info("Loading prospect corpus cache %s", corpus_path)
        return _load_corpus_from_cache(corpus_path)
    log.info("No cache at %s; rebuilding corpus from %s",
             corpus_path, seasons_root or DEFAULT_SEASONS_ROOT)
    return build_prospect_corpus(
        seasons_root=seasons_root or DEFAULT_SEASONS_ROOT,
        sos_root=sos_root or DEFAULT_SOS_ROOT,
    )


def _normalize_name(name: str) -> str:
    """Mirrors the engine module's normalization (lowercase, strip suffixes)."""
    if not name:
        return ""
    n = name.lower().strip()
    for suf in (" jr.", " jr", " sr.", " sr", " ii", " iii", " iv", " v"):
        if n.endswith(suf):
            n = n[: -len(suf)].rstrip()
    return " ".join(n.split())


def _load_ktc(path: Path) -> Dict[Tuple[str, str], Dict]:
    """Return (normalized_name, position) → {ktc_rank, ktc_value, is_rookie}.

    Uses the superflex ranking by default — the consensus the league
    builders use for rookie / dynasty trades. Falls back gracefully when
    the file or the keys are missing.
    """
    if not path.exists():
        log.warning("KTC file %s missing; delta column will be empty", path)
        return {}
    raw = json.loads(path.read_text())
    players = raw.get("players") if isinstance(raw, dict) else raw
    out: Dict[Tuple[str, str], Dict] = {}
    if not isinstance(players, list):
        return out
    for p in players:
        name = p.get("name") or p.get("display_name") or ""
        pos = (p.get("position") or "").upper()
        if not name or pos not in SKILL_POSITIONS:
            continue
        sf = p.get("superflex") or {}
        one = p.get("one_qb") or {}
        out[(_normalize_name(name), pos)] = {
            "ktc_rank_sf": sf.get("rank"),
            "ktc_value_sf": sf.get("value"),
            "ktc_pos_rank_sf": sf.get("positional_rank"),
            "ktc_rank_1qb": one.get("rank"),
            "ktc_value_1qb": one.get("value"),
            "ktc_pos_rank_1qb": one.get("positional_rank"),
            "is_rookie": bool(p.get("rookie")),
            "ktc_team": p.get("team"),
        }
    return out


def _load_nfl_careers(path: Path) -> Dict[str, Dict]:
    """Aggregate nflverse player_stats_season into per-gsis career summary.

    Returns {gsis_id: {career_fp, peak3_fp_pg, seasons_played, max_year,
                       per_season_fp_pg}}.
    """
    if not path.exists():
        log.warning("NFL stats file %s missing; comp NFL careers will be empty", path)
        return {}
    # Per-season fp/g lookup
    per_player: Dict[str, List[Tuple[int, float, float, int]]] = {}
    # Read gzipped CSV without pandas (keep dep surface small for tests).
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("season_type") or "REG") != "REG":
                continue
            pid = row.get("player_id") or ""
            if not pid or pid == "0":
                continue
            try:
                season = int(row.get("season") or 0)
                games = float(row.get("games") or 0)
                fp_ppr = float(row.get("fantasy_points_ppr") or 0)
            except ValueError:
                continue
            if games <= 0:
                continue
            fp_pg = fp_ppr / games
            per_player.setdefault(pid, []).append((season, fp_ppr, fp_pg, int(games)))
    out: Dict[str, Dict] = {}
    for pid, rows in per_player.items():
        rows.sort()
        career_fp = sum(r[1] for r in rows)
        # Peak 3-year fp/g — average of the best 3 single-season fp/g values.
        fp_pg_list = sorted([r[2] for r in rows], reverse=True)
        peak3 = statistics.fmean(fp_pg_list[:3]) if fp_pg_list else 0.0
        out[pid] = {
            "career_fp": round(career_fp, 2),
            "peak3_fp_pg": round(peak3, 3),
            "seasons_played": len(rows),
            "max_year": max(r[0] for r in rows),
            "min_year": min(r[0] for r in rows),
        }
    return out


# ---------------------------------------------------------------------------
# Hit labels + projection math
# ---------------------------------------------------------------------------

def _hit_label(career: Optional[Mapping]) -> str:
    """Classify a comp's NFL career into elite / starter / bust / unknown."""
    if not career:
        return "unknown"
    peak3 = float(career.get("peak3_fp_pg") or 0.0)
    seasons = int(career.get("seasons_played") or 0)
    if peak3 >= ELITE_PEAK3_FPG:
        return "elite"
    if peak3 >= STARTER_PEAK3_FPG:
        return "starter"
    if seasons >= BUST_MIN_SEASONS and peak3 < 6.0:
        return "bust"
    return "unknown"


# v3.4: minimum number of NFL-career-bearing comps to FULLY trust the
# comp-weighted projection. Below this, we blend toward the
# pick-tier baseline (Phil 2026-05-28: an actual 1st-overall pick like
# Fernando Mendoza shouldn't show projected_career_fp=1.4 just because
# his college-fp comp pool happens to be UNC/Iowa backups who never
# played in the NFL).
FULL_CONFIDENCE_NFL_COMPS = 8


def _project_arc(
    comp_records: Sequence[Mapping],
    *,
    position: str = "",
    pick: Optional[int] = None,
) -> Dict[str, float]:
    """Similarity-weighted projection from comp NFL careers, blended
    with a draft-pick-tier baseline (v3.4) when the comp pool is thin
    on actual NFL careers.

    Weight ∝ 1 / (1 + distance). Comps without an NFL career contribute
    a zero career_fp / peak3 (their college profile didn't reach the
    NFL — real signal). But when fewer than ``FULL_CONFIDENCE_NFL_COMPS``
    comps have any NFL data at all, we blend the comp projection with
    the pick-tier prior: a 1st-overall pick is going to have a real
    NFL career even if no historical-similar college player happened
    to play professionally.
    """
    weights: List[float] = []
    career_fps: List[float] = []
    peak3s: List[float] = []
    yrs_list: List[float] = []
    for c in comp_records:
        d = float(c.get("distance") or 0.0)
        w = 1.0 / (1.0 + d)
        nfl = c.get("nfl_career") or {}
        career_fps.append(float(nfl.get("career_fp") or 0.0))
        peak3s.append(float(nfl.get("peak3_fp_pg") or 0.0))
        yrs_list.append(float(nfl.get("seasons_played") or 0.0))
        weights.append(w)
    tot = sum(weights)
    if tot <= 0 or not comp_records:
        # No comps at all — use pure pick-tier baseline.
        return _baseline_projection(position, pick)
    proj_career = sum(w * x for w, x in zip(weights, career_fps)) / tot
    proj_peak3 = sum(w * x for w, x in zip(weights, peak3s)) / tot
    proj_years = sum(w * x for w, x in zip(weights, yrs_list)) / tot
    stdev = statistics.pstdev(career_fps) if len(career_fps) > 1 else 0.0
    n_with_nfl = sum(1 for c in comp_records if c.get("nfl_career"))

    # v3.4 — confidence-blend with pick-tier baseline.
    confidence = min(n_with_nfl / float(FULL_CONFIDENCE_NFL_COMPS), 1.0)
    baseline = _baseline_projection(position, pick)
    blended_career = (
        confidence * proj_career + (1 - confidence) * baseline["projected_career_fp"]
    )
    blended_peak3 = (
        confidence * proj_peak3 + (1 - confidence) * baseline["projected_peak3_fp_pg"]
    )
    blended_years = (
        confidence * proj_years
        + (1 - confidence) * baseline["projected_years_in_league"]
    )
    if confidence >= 0.999:
        projection_source = "comp_weighted"
    elif confidence <= 0.001:
        projection_source = baseline["projection_source"]
    else:
        projection_source = f"blend_{confidence:.2f}_{baseline['projection_source']}"
    return {
        "projected_career_fp": round(blended_career, 1),
        "projected_peak3_fp_pg": round(blended_peak3, 2),
        "projected_years_in_league": round(blended_years, 2),
        "projected_career_fp_stdev": round(stdev, 1),
        "n_comps_with_nfl": n_with_nfl,
        "projection_confidence": round(confidence, 3),
        "projection_source": projection_source,
        "comp_only_career_fp": round(proj_career, 1),
        "pick_tier_baseline_fp": baseline["projected_career_fp"],
    }


# ---------------------------------------------------------------------------
# Production summary (raw / SOS-adjusted) — read from PR 3's raw features
# ---------------------------------------------------------------------------

def _slugify(name: str, pid: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    suffix = re.sub(r"[^A-Za-z0-9]+", "", pid or "")[-6:]
    return f"{s}-{suffix}" if suffix else s


def _summarize_career(pv: ProspectVector) -> Dict[str, float]:
    """Surface the per-game production summary from the ProspectVector."""
    return {
        "adj_career_fp_pg": round(pv.raw_features.get("adj_fp_pg_avg", 0.0), 2),
        "peak_season_fp_pg": round(pv.raw_features.get("adj_fp_pg_peak", 0.0), 2),
        "final_season_fp_pg": round(pv.raw_features.get("adj_fp_pg_final", 0.0), 2),
        "conference_tier_mult_avg": round(
            pv.raw_features.get("conference_tier_mult_avg", 0.65), 3),
    }


# ---------------------------------------------------------------------------
# Per-position model rank assignment + KTC delta
# ---------------------------------------------------------------------------

def _normalize_name_for_pfr(name: str) -> str:
    """v3.3 — fold name for join keys with PFR (lower, strip punctuation,
    collapse whitespace). Mirrors _normalize_name's intent but is a
    separate path so future tweaks to either don't break the other.
    """
    if not name:
        return ""
    s = name.lower()
    for ch in [".", ",", "'", "’", "-", "—", "–"]:
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _load_pfr_draft_classes(path: Path) -> Mapping[Tuple[str, str], Dict]:
    """v3.3 — load PFR NFL draft classes (years 2022..2026) so the
    prospects tab can mark prospects who were actually drafted, with
    pick / team / draft-year. Returns a (normalized_name, position)
    -> draft-pick dict map. The position key uses our skill positions;
    PFR's defensive positions (OLB, CB, etc.) are filtered out before
    join — they wouldn't be in the prospects record set anyway.
    """
    if not path.exists():
        log.warning("PFR draft data not found at %s — skip enrichment", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    by_year = payload.get("by_year", {})
    out: Dict[Tuple[str, str], Dict] = {}
    for year, picks in by_year.items():
        for p in picks:
            pos = (p.get("position") or "").upper()
            if pos not in SKILL_POSITIONS:
                continue
            key = (_normalize_name_for_pfr(p.get("player_name", "")), pos)
            out[key] = {
                "year": int(year),
                "round": p.get("rnd"),
                "pick": p.get("pick"),
                "team": p.get("team"),
                "college": p.get("college"),
                "pfr_id": p.get("pfr_id"),
            }
    log.info("PFR draft picks indexed: %d skill picks across years %s",
             len(out), sorted(by_year.keys()))
    return out


def _attach_pfr_draft(records: List[Dict],
                     pfr: Mapping[Tuple[str, str], Dict]) -> None:
    """Mutate ``records`` in place, adding a ``drafted`` block when the
    prospect shows up in PFR's draft data for their draft class. The
    UI uses this to mark drafted prospects (with team + pick) and to
    filter the 2026 class to actually-drafted players when desired.

    Match key: (normalized_name, position). We also require the PFR
    draft year to match the prospect's draft_class year; mismatches
    (e.g. a 2024 prospect with a same-name 2026 draftee) are dropped.
    """
    matched = 0
    for r in records:
        key = (_normalize_name_for_pfr(r["name"]), r["position"])
        pick = pfr.get(key)
        if not pick:
            r["drafted"] = None
            continue
        if pick.get("year") != r.get("draft_class"):
            r["drafted"] = None
            continue
        r["drafted"] = pick
        matched += 1
    log.info("PFR draft enrichment: %d of %d records matched",
             matched, len(records))


def _attach_ktc_and_rank(records: List[Dict], ktc: Mapping[Tuple[str, str], Dict]) -> None:
    """Compute per-position model_rank (within the supplied records) and
    join in KTC values + delta. Mutates ``records`` in place.

    model_rank is across the WHOLE supplied record set (all classes), so
    the rank a prospect carries is comparable across draft classes — a
    2024 prospect at model_rank=12 means "12th overall by projected
    career_fp at his position, vs. every other prospect in the set".
    """
    by_pos: Dict[str, List[Dict]] = {}
    for r in records:
        by_pos.setdefault(r["position"], []).append(r)
    for pos, rows in by_pos.items():
        rows.sort(key=lambda r: r["projection"]["projected_career_fp"], reverse=True)
        for i, r in enumerate(rows, start=1):
            r["model_pos_rank"] = i

    # Overall rank by projected_career_fp (across all positions, all classes).
    records_sorted = sorted(records,
                            key=lambda r: r["projection"]["projected_career_fp"],
                            reverse=True)
    for i, r in enumerate(records_sorted, start=1):
        r["model_overall_rank"] = i

    # KTC join + delta. Use SUPERFLEX positional rank as the "ktc rank" the
    # UI surfaces (closer to dynasty-rookie consensus than the 1QB version).
    for r in records:
        key = (_normalize_name(r["name"]), r["position"])
        k = ktc.get(key)
        if not k:
            r["ktc"] = None
            r["ktc_delta_pos"] = None
            r["ktc_delta_overall"] = None
            continue
        r["ktc"] = {
            "is_rookie_in_ktc": k["is_rookie"],
            "ktc_rank_sf": k["ktc_rank_sf"],
            "ktc_pos_rank_sf": k["ktc_pos_rank_sf"],
            "ktc_value_sf": k["ktc_value_sf"],
            "ktc_rank_1qb": k["ktc_rank_1qb"],
            "ktc_pos_rank_1qb": k["ktc_pos_rank_1qb"],
            "ktc_value_1qb": k["ktc_value_1qb"],
            "ktc_team": k["ktc_team"],
        }
        if k["ktc_pos_rank_sf"] is not None:
            r["ktc_delta_pos"] = int(k["ktc_pos_rank_sf"]) - int(r["model_pos_rank"])
        else:
            r["ktc_delta_pos"] = None
        if k["ktc_rank_sf"] is not None:
            r["ktc_delta_overall"] = int(k["ktc_rank_sf"]) - int(r["model_overall_rank"])
        else:
            r["ktc_delta_overall"] = None


# ---------------------------------------------------------------------------
# Core: build one prospect record
# ---------------------------------------------------------------------------

def build_prospect_record(
    target: ProspectVector,
    corpus: Sequence[ProspectVector],
    resolver: NameCollisionResolver,
    nfl_careers: Mapping[str, Dict],
    top_k: int = DEFAULT_TOP_K,
    pfr_pick: Optional[Mapping] = None,
) -> Dict:
    """Build the full prospect dict for ``target``.

    ``pfr_pick`` is the matching PFR draft entry (round/pick/team), used
    by v3.4 to anchor the projection on a pick-tier baseline when the
    comp pool has few NFL careers. Pass ``None`` for un-drafted prospects.
    """
    comps = find_similar_prospects(target, corpus, top_k=top_k, resolver=resolver)
    comp_records: List[Dict] = []
    for c in comps:
        nfl_career = None
        if c.nfl_gsis_id:
            nfl_career = nfl_careers.get(c.nfl_gsis_id)
        rec = {
            "name": c.comp_player_name,
            "slug": _slugify(c.comp_player_name, c.comp_cfb_player_id),
            "school": c.comp_school_last,
            "last_season": c.comp_last_season,
            "class_year": c.comp_last_season + 1,
            "similarity": c.similarity,
            "distance": c.distance,
            "nfl_gsis_id": c.nfl_gsis_id,
            "nfl_display_name": c.nfl_display_name,
            "nfl_career": nfl_career,
            "hit_label": _hit_label(nfl_career),
        }
        comp_records.append(rec)

    pick_number = pfr_pick.get("pick") if pfr_pick else None
    projection = _project_arc(
        comp_records, position=target.position, pick=pick_number,
    )
    # v3.4: when a pfr_pick is supplied (drafted-only mode), trust it as
    # the authoritative draft year — a corpus record may be off by one
    # for early declarees / redshirts. When no pfr_pick (legacy mode),
    # fall back to the corpus's last_season + 1.
    if pfr_pick is not None and pfr_pick.get("year"):
        draft_class = int(pfr_pick["year"])
    else:
        draft_class = target.last_season + 1
    rec = {
        "cfb_player_id": target.cfb_player_id,
        "name": target.player_name,
        "slug": _slugify(target.player_name, target.cfb_player_id),
        "position": target.position,
        "school": target.school_last,
        "draft_class": draft_class,
        "last_season_year": target.last_season,
        "first_season_year": target.first_season,
        "career_stage_length": target.career_stage_length,
        "age": round(target.age_at_last_season, 1),
        "age_inferred": target.age_inferred,
        "conference_tier_last": target.conference_tier_last,
        "production": _summarize_career(target),
        "projection": projection,
        "comps": comp_records,
    }
    return rec


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _build_stub_record_for_undiscovered_pick(
    pick: Mapping, draft_class: int,
) -> Dict:
    """v3.4 — PFR has the drafted player but we can't find them in our
    college corpus. Emit a stub record so they still appear in the
    Prospects table for their class. Projection falls back to the
    pick-tier baseline. The user sees “no college comp data” in the UI.
    """
    pos = pick.get("position") or ""
    pick_no = pick.get("pick")
    projection = _baseline_projection(pos, pick_no)
    fake_slug = re.sub(r"[^a-z0-9]+", "-", (pick.get("player_name") or "").lower()).strip("-")
    return {
        "cfb_player_id": pick.get("college_stats_slug") or fake_slug,
        "name": pick.get("player_name", ""),
        "slug": fake_slug + "-pfr",
        "position": pos,
        "school": pick.get("college"),
        "draft_class": draft_class,
        "last_season_year": draft_class - 1,
        "first_season_year": None,
        "career_stage_length": None,
        "age": None,
        "age_inferred": True,
        "conference_tier_last": None,
        "production": None,
        "projection": projection,
        "comps": [],
        "corpus_match": False,
    }


def _index_tankathon_by_class(
    tankathon_path: Path,
) -> Mapping[int, List[Mapping]]:
    """v3.4 — load Tankathon big-board for the 2027 class (upcoming
    draft, no PFR data yet). Returns {year: [pick-like dicts]} that
    are interchangeable with PFR picks. The rank from Tankathon is
    treated as the proxy ``pick`` so the pick-tier baseline projection
    has something to lean on for the prospects on the board.
    """
    if not tankathon_path.exists():
        log.warning("Tankathon data not found at %s — 2027 class will be empty", tankathon_path)
        return {}
    with tankathon_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    by_year_raw = payload.get("by_year") or {}
    out: Dict[int, List[Mapping]] = {}
    for year_str, prospects in by_year_raw.items():
        year = int(year_str)
        skill = [p for p in prospects
                 if (p.get("position") or "").upper() in SKILL_POSITIONS]
        normalised: List[Mapping] = []
        for p in skill:
            normalised.append({
                "year": year,
                "rnd": None,
                "pick": p.get("rank"),     # big-board rank as pick proxy
                "team": None,
                "player_name": p.get("name", ""),
                "pfr_id": None,
                "position": (p.get("position") or "").upper(),
                "college": p.get("school"),
                "college_stats_slug": p.get("college_slug"),
                "source": "tankathon_big_board",
            })
        normalised.sort(key=lambda p: p.get("pick") or 9999)
        out[year] = normalised
    return out


def _index_pfr_picks_by_class_and_name(
    pfr_path: Path,
) -> Tuple[Mapping[int, List[Mapping]], Mapping[Tuple[int, str, str], Mapping]]:
    """Load PFR draft picks and index them two ways:
      * by_class[year] -> ordered list of picks (skill positions only)
      * by_key[(year, normalized_name, position)] -> pick
    Returns ({}, {}) if the file is missing (caller falls back to
    corpus-only mode).
    """
    if not pfr_path.exists():
        log.warning("PFR draft data not found at %s — falling back to corpus-only mode", pfr_path)
        return ({}, {})
    with pfr_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    by_class: Dict[int, List[Mapping]] = {}
    by_key: Dict[Tuple[int, str, str], Mapping] = {}
    for year_str, picks in (payload.get("by_year") or {}).items():
        year = int(year_str)
        skill_picks = [p for p in picks
                       if (p.get("position") or "").upper() in SKILL_POSITIONS]
        skill_picks.sort(key=lambda p: p.get("pick", 9999))
        by_class[year] = skill_picks
        for p in skill_picks:
            name_norm = _normalize_name_for_pfr(p.get("player_name", ""))
            pos = (p.get("position") or "").upper()
            by_key[(year, name_norm, pos)] = p
    log.info("PFR draft indexed: years=%s, %d skill picks",
             sorted(by_class.keys()), len(by_key))
    return (by_class, by_key)


def build_prospect_records(
    corpus: Sequence[ProspectVector],
    resolver: NameCollisionResolver,
    nfl_careers: Mapping[str, Dict],
    ktc: Mapping[Tuple[str, str], Dict],
    draft_classes: Iterable[int] = DEFAULT_DRAFT_CLASSES,
    top_k: int = DEFAULT_TOP_K,
    pfr_path: Optional[Path] = None,
    drafted_only: bool = True,
) -> Dict[int, List[Dict]]:
    """Return {draft_class: [prospect_record, ...]}.

    v3.4 (Phil 2026-05-28): when ``drafted_only`` is True (default), a
    prospect is included in a draft class ONLY if a matching PFR draft
    pick exists for that (name, position, year). PFR picks with no
    corpus match get a stub record so the player still appears with a
    pick-tier baseline projection.

    When ``drafted_only`` is False (legacy / debug mode), the old
    behavior is preserved: every corpus prospect whose last_season + 1
    falls inside the requested draft classes is emitted, regardless
    of whether they were actually drafted.
    """
    draft_classes = set(int(x) for x in draft_classes)
    pfr_by_class, pfr_by_key = _index_pfr_picks_by_class_and_name(
        pfr_path or Path("data/pfr/draft_classes_all.json")
    )
    tankathon_by_class = _index_tankathon_by_class(
        Path("data/tankathon/big_board_2027.json")
    )

    records: List[Dict] = []

    if drafted_only and (pfr_by_class or tankathon_by_class):
        # Authoritative: PFR list (or Tankathon for the upcoming class).
        for year in draft_classes:
            picks_for_year = list(pfr_by_class.get(year, []))
            if year in tankathon_by_class:
                picks_for_year.extend(tankathon_by_class[year])
            for pick in picks_for_year:
                pos = (pick.get("position") or "").upper()
                pick_name_norm = _normalize_name_for_pfr(pick.get("player_name", ""))
                # Find the corpus match: same position, matching name
                # (normalized). Preferred season match is year - 1 for
                # already-drafted classes; for upcoming classes (e.g.
                # 2027) we accept any last_season within a 2-year
                # window (still in college, latest available cfbfastR
                # season is 2025).
                match: Optional[ProspectVector] = None
                same_name = [pv for pv in corpus
                             if pv.position == pos
                             and _normalize_name_for_pfr(pv.player_name) == pick_name_norm]
                if same_name:
                    # Prefer the closest last_season to year - 1, falling
                    # back to the most-recent season available for
                    # upcoming classes (Tankathon 2027 picks won't have
                    # 2026 college data yet).
                    same_name.sort(key=lambda pv: (
                        abs(pv.last_season - (year - 1)),
                        -pv.last_season,
                    ))
                    if abs(same_name[0].last_season - (year - 1)) <= 2:
                        match = same_name[0]
                if match is not None:
                    rec = build_prospect_record(
                        match, corpus, resolver, nfl_careers,
                        top_k=top_k, pfr_pick=pick,
                    )
                    rec["corpus_match"] = True
                else:
                    rec = _build_stub_record_for_undiscovered_pick(pick, year)
                # Stamp the drafted block on every record.
                rec["drafted"] = {
                    "year": pick.get("year"),
                    "round": pick.get("rnd"),
                    "pick": pick.get("pick"),
                    "team": pick.get("team"),
                    "college": pick.get("college"),
                    "pfr_id": pick.get("pfr_id"),
                }
                records.append(rec)
        log.info("v3.4 drafted-only mode: %d records across classes %s",
                 len(records), sorted(draft_classes))
    else:
        # Legacy mode: every corpus prospect whose last_season+1 is in
        # the requested set. Preserves backward compatibility for the
        # back-test harness and any caller that explicitly opts out.
        targets = [pv for pv in corpus if (pv.last_season + 1) in draft_classes
                   and pv.position in SKILL_POSITIONS]
        log.info("legacy mode: %d corpus targets across classes %s",
                 len(targets), sorted(draft_classes))
        for pv in targets:
            rec = build_prospect_record(pv, corpus, resolver, nfl_careers,
                                        top_k=top_k)
            rec["drafted"] = None
            rec["corpus_match"] = True
            records.append(rec)

    _attach_ktc_and_rank(records, ktc)

    by_class: Dict[int, List[Dict]] = {dc: [] for dc in sorted(draft_classes)}
    for r in records:
        by_class.setdefault(r["draft_class"], []).append(r)
    # Default sort: by NFL draft pick (ascending). UI can re-sort by
    # model projection. Picks with no draft data (stubs from legacy
    # mode) fall to the bottom.
    for dc, rows in by_class.items():
        rows.sort(key=lambda r: (
            (r.get("drafted") or {}).get("pick") or 10**6,
            -r["projection"]["projected_career_fp"],
            r["name"],
        ))
    return by_class


def _write_artifacts(by_class: Mapping[int, Sequence[Dict]], out_dir: Path,
                     version: str = "v3.0-pr4") -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    all_prospects: List[Dict] = []
    for dc in sorted(by_class.keys()):
        rows = list(by_class[dc])
        path = out_dir / f"prospects_{dc}.json"
        payload = {
            "version": version,
            "draft_class": dc,
            "n_prospects": len(rows),
            "prospects": rows,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        written.append(path)
        all_prospects.extend(rows)
        log.info("Wrote %s (%d prospects)", path, len(rows))
    # Aggregated artifact for the UI.
    all_path = out_dir / "prospects_all.json"
    all_payload = {
        "version": version,
        "draft_classes": sorted(by_class.keys()),
        "n_prospects": len(all_prospects),
        "prospects": all_prospects,
    }
    all_path.write_text(json.dumps(all_payload, indent=2, sort_keys=True))
    written.append(all_path)
    log.info("Wrote %s (%d aggregate prospects)", all_path, len(all_prospects))
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", type=Path,
                        default=Path("data/engine_v3/prospect_corpus.json.gz"))
    parser.add_argument("--seasons", type=Path, default=None)
    parser.add_argument("--sos", type=Path, default=None)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE_FILE)
    parser.add_argument("--ktc", type=Path,
                        default=Path("data/consensus/ktc_latest.json"))
    parser.add_argument("--pfr-draft", type=Path,
                        default=Path("data/pfr/draft_classes_all.json"),
                        help="PFR NFL draft-class JSON (v3.3, Phil 2026-05-28).")
    parser.add_argument("--nfl", type=Path,
                        default=Path("data/nflverse/player_stats_season.csv.gz"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/engine_v3"))
    parser.add_argument(
        "--classes",
        type=lambda s: tuple(int(x) for x in s.split(",")),
        default=DEFAULT_DRAFT_CLASSES,
        help="Comma-separated draft-class years (default 2022,2023,2024,2025,2026)",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument(
        "--all-corpus", action="store_true",
        help="v3.4 escape hatch: include every corpus prospect (legacy "
             "behavior). Default is drafted-only — only PFR-drafted "
             "players appear in each class.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    corpus = _ensure_corpus(args.corpus, args.seasons, args.sos)
    log.info("Corpus loaded: %d ProspectVectors", len(corpus))

    resolver = NameCollisionResolver.from_file(args.bridge)
    ktc = _load_ktc(args.ktc)
    log.info("KTC entries loaded: %d", len(ktc))
    nfl_careers = _load_nfl_careers(args.nfl)
    log.info("NFL careers loaded: %d gsis ids", len(nfl_careers))

    by_class = build_prospect_records(
        corpus=corpus,
        resolver=resolver,
        nfl_careers=nfl_careers,
        ktc=ktc,
        draft_classes=args.classes,
        top_k=args.top_k,
        pfr_path=args.pfr_draft,
        drafted_only=not args.all_corpus,
    )
    _write_artifacts(by_class, args.out_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual CLI
    sys.exit(main())
