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
DEFAULT_DRAFT_CLASSES: Tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)

# Hit-label thresholds (PPR per-game over the player's best 3 NFL seasons).
# Calibrated so that historical elites (CMC, Justin Jefferson, Henry, etc.)
# come out "elite" and mid-tier starters land in "starter".
ELITE_PEAK3_FPG = 18.0   # ≥ → elite
STARTER_PEAK3_FPG = 12.0  # ≥ → starter (else bust if ≥ 3 seasons, else unknown)
BUST_MIN_SEASONS = 3      # need at least this many NFL seasons to call bust

# Top-K comps per prospect (PR brief: top-25).
DEFAULT_TOP_K = 25


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


def _project_arc(comp_records: Sequence[Mapping]) -> Dict[str, float]:
    """Similarity-weighted projection from comp NFL careers.

    Weight ∝ 1 / (1 + distance). Comps without an NFL career contribute
    a zero career_fp / peak3, which is appropriate — they are evidence
    that "this college profile didn't reach the NFL" and shouldn't be
    silently dropped from the projection (would bias every projection
    upward).
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
        return {
            "projected_career_fp": 0.0,
            "projected_peak3_fp_pg": 0.0,
            "projected_years_in_league": 0.0,
            "projected_career_fp_stdev": 0.0,
            "n_comps_with_nfl": 0,
        }
    proj_career = sum(w * x for w, x in zip(weights, career_fps)) / tot
    proj_peak3 = sum(w * x for w, x in zip(weights, peak3s)) / tot
    proj_years = sum(w * x for w, x in zip(weights, yrs_list)) / tot
    # Sample stdev of weighted career_fp (unweighted population stdev is
    # close enough for the CI we surface; we just need a "confidence" knob).
    stdev = statistics.pstdev(career_fps) if len(career_fps) > 1 else 0.0
    n_with_nfl = sum(1 for c in comp_records if c.get("nfl_career"))
    return {
        "projected_career_fp": round(proj_career, 1),
        "projected_peak3_fp_pg": round(proj_peak3, 2),
        "projected_years_in_league": round(proj_years, 2),
        "projected_career_fp_stdev": round(stdev, 1),
        "n_comps_with_nfl": n_with_nfl,
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
) -> Dict:
    """Build the full prospect dict for ``target``."""
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

    projection = _project_arc(comp_records)
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

def build_prospect_records(
    corpus: Sequence[ProspectVector],
    resolver: NameCollisionResolver,
    nfl_careers: Mapping[str, Dict],
    ktc: Mapping[Tuple[str, str], Dict],
    draft_classes: Iterable[int] = DEFAULT_DRAFT_CLASSES,
    top_k: int = DEFAULT_TOP_K,
) -> Dict[int, List[Dict]]:
    """Return {draft_class: [prospect_record, ...]}.

    Stable ordering: prospects are sorted by projected_career_fp desc
    within each class — UI consumers don't have to re-sort.
    """
    draft_classes = set(int(x) for x in draft_classes)
    # Index corpus once for fast filtering.
    targets = [pv for pv in corpus if (pv.last_season + 1) in draft_classes
               and pv.position in SKILL_POSITIONS]
    log.info("Building records for %d targets across draft classes %s",
             len(targets), sorted(draft_classes))

    records: List[Dict] = []
    for pv in targets:
        rec = build_prospect_record(pv, corpus, resolver, nfl_careers, top_k=top_k)
        records.append(rec)

    _attach_ktc_and_rank(records, ktc)

    by_class: Dict[int, List[Dict]] = {dc: [] for dc in sorted(draft_classes)}
    for r in records:
        by_class.setdefault(r["draft_class"], []).append(r)
    for dc, rows in by_class.items():
        rows.sort(key=lambda r: (
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

    pfr_draft = _load_pfr_draft_classes(args.pfr_draft)

    by_class = build_prospect_records(
        corpus=corpus,
        resolver=resolver,
        nfl_careers=nfl_careers,
        ktc=ktc,
        draft_classes=args.classes,
        top_k=args.top_k,
    )
    # v3.3 — stamp drafted status onto every record (after model rank
    # / KTC join; doesn't affect rankings, just enriches the UI payload).
    all_records: List[Dict] = []
    for rows in by_class.values():
        all_records.extend(rows)
    _attach_pfr_draft(all_records, pfr_draft)
    _write_artifacts(by_class, args.out_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual CLI
    sys.exit(main())
