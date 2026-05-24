#!/usr/bin/env python3
"""v3.0 PR 5 — Back-test ship gate for the prospect engine.

Deterministic, network-free validation that the v3.0 prospect engine
is good enough to ship the PR 6 UI. If any gate fails by ≥5%, this
script exits 1 and PR 6 must NOT be merged.

Hold-out classes: 2017, 2018, 2019, 2020, 2021. These all have ≥ 3
NFL seasons of follow-up data in nflverse and are exactly the window
KTC has published per-rookie-class hit-rate stats for.

Method:
  1. For each hold-out player, treat them as if they were a prospect
     in their draft class.
  2. Restrict the comp pool to players with class_year < target.class_year
     − 1 (i.e. their last NFL data is older than the target's
     pre-draft cutoff). Prevents leakage.
  3. Project NFL career-fp from similarity-weighted comps as PR 4
     does — same projection function.
  4. Compare projection rank to ACTUAL NFL career-fp rank using the
     bridge → nflverse linkage.

Ship-gate thresholds (must ALL pass within ±5%):
  * Hit@10 (top-50 projected → became NFL elite): ≥ 22%
  * Bust@10 (bottom-50 of top-200 → became NFL bust): ≥ 55%
  * Spearman ρ(model_rank, actual_nfl_career_fp_rank): ≥ 0.28
  * KTC head-to-head: model wins ambiguous within-tier pairs ≥ 50%

Exit codes:
  0 — all gates pass (ship green)
  1 — at least one gate fails by ≥ 5% (do NOT ship)
  2 — at least one gate fails by < 5% (ship with `experimental` flag)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# Make sibling scripts importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dynasty.engine.prospect_similarity import (  # noqa: E402
    DEFAULT_BRIDGE_FILE,
    NameCollisionResolver,
    ProspectVector,
    find_similar_prospects,
)
import build_prospects_v3 as bp  # noqa: E402

log = logging.getLogger("backtest_v3_engine")

# Hold-out draft classes used to evaluate the engine. last_season values
# = class_year − 1, so {2016..2020} are the prospect last-college seasons.
DEFAULT_HOLDOUT_CLASSES: Tuple[int, ...] = (2017, 2018, 2019, 2020, 2021)

# Soft / hard fail thresholds (relative to the gate's target).
SHIP_SOFT_FAIL_FRAC = 0.05  # within 5% of target = experimental

# Ship-gate thresholds.
GATE_HIT_AT_10 = 0.22
GATE_BUST_AT_10 = 0.55
GATE_SPEARMAN = 0.28
GATE_KTC_H2H = 0.50

# Top-K for elite/bust gates — explicitly the brief's top-50 / bottom-50.
HIT_GATE_TOP_N = 50
BUST_GATE_BOTTOM_OF = 200
BUST_GATE_BOTTOM_N = 50


# ---------------------------------------------------------------------------
# Spearman correlation (no scipy dep)
# ---------------------------------------------------------------------------

def _spearman_rho(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman ρ between two equal-length sequences.

    Implementation: rank both arrays, then compute Pearson's r over the
    ranks. Ties get average ranks.
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0

    def _ranks(vs: Sequence[float]) -> List[float]:
        sorted_idx = sorted(range(n), key=lambda i: vs[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[sorted_idx[j + 1]] == vs[sorted_idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-based
            for k in range(i, j + 1):
                ranks[sorted_idx[k]] = avg
            i = j + 1
        return ranks

    rx = _ranks(xs)
    ry = _ranks(ys)
    mux = statistics.fmean(rx)
    muy = statistics.fmean(ry)
    num = sum((rx[i] - mux) * (ry[i] - muy) for i in range(n))
    denx = math.sqrt(sum((r - mux) ** 2 for r in rx))
    deny = math.sqrt(sum((r - muy) ** 2 for r in ry))
    if denx == 0 or deny == 0:
        return 0.0
    return num / (denx * deny)


# ---------------------------------------------------------------------------
# Hold-out evaluation
# ---------------------------------------------------------------------------

def _project_for_holdout(
    target: ProspectVector,
    full_corpus: Sequence[ProspectVector],
    resolver: NameCollisionResolver,
    nfl_careers: Mapping[str, Dict],
    top_k: int = bp.DEFAULT_TOP_K,
) -> Tuple[Dict, List[Dict]]:
    """Project a single hold-out, with a strictly-pre-target comp pool.

    Restricts the comp corpus to players whose last_season is at least
    2 years older than the target's last_season (i.e. their draft_class
    is < target.draft_class − 1). Prevents leakage.
    """
    target_class = target.last_season + 1
    leak_cutoff = target_class - 1  # comp class_year must be STRICTLY < this
    sub_corpus = [pv for pv in full_corpus
                  if (pv.last_season + 1) < leak_cutoff
                  and pv.cfb_player_id != target.cfb_player_id]
    comps = find_similar_prospects(target, sub_corpus, top_k=top_k, resolver=resolver)
    comp_records: List[Dict] = []
    for c in comps:
        nfl = nfl_careers.get(c.nfl_gsis_id) if c.nfl_gsis_id else None
        comp_records.append({
            "name": c.comp_player_name,
            "distance": c.distance,
            "similarity": c.similarity,
            "nfl_gsis_id": c.nfl_gsis_id,
            "nfl_career": nfl,
            "hit_label": bp._hit_label(nfl),
        })
    projection = bp._project_arc(comp_records)
    return projection, comp_records


def _actual_nfl_career_for_target(
    target: ProspectVector,
    resolver: NameCollisionResolver,
    nfl_careers: Mapping[str, Dict],
) -> Optional[Dict]:
    """Resolve the hold-out's OWN NFL career via the same bridge."""
    info = resolver.resolve(target) or {}
    gsis = info.get("nfl_pfr_player_id")
    if not gsis:
        return None
    return nfl_careers.get(gsis)


def _hit_actual(career: Optional[Mapping]) -> str:
    """Use the same label thresholds as PR 4."""
    return bp._hit_label(career)


def evaluate(
    corpus: Sequence[ProspectVector],
    resolver: NameCollisionResolver,
    nfl_careers: Mapping[str, Dict],
    ktc: Mapping[Tuple[str, str], Dict],
    holdout_classes: Sequence[int] = DEFAULT_HOLDOUT_CLASSES,
    top_k: int = bp.DEFAULT_TOP_K,
) -> Dict:
    """Run the back-test and return per-gate + aggregate metrics."""
    holdout_classes_set = set(int(c) for c in holdout_classes)
    holdouts = [pv for pv in corpus
                if (pv.last_season + 1) in holdout_classes_set
                and pv.position in bp.SKILL_POSITIONS]
    log.info("Hold-out targets: %d players across classes %s",
             len(holdouts), sorted(holdout_classes_set))

    rows: List[Dict] = []
    for target in holdouts:
        actual = _actual_nfl_career_for_target(target, resolver, nfl_careers)
        if actual is None:
            # No bridge → can't score
            continue
        projection, _comps = _project_for_holdout(
            target, corpus, resolver, nfl_careers, top_k=top_k,
        )
        rows.append({
            "name": target.player_name,
            "position": target.position,
            "school": target.school_last,
            "class_year": target.last_season + 1,
            "projected_career_fp": projection["projected_career_fp"],
            "projected_peak3_fp_pg": projection["projected_peak3_fp_pg"],
            "actual_career_fp": actual.get("career_fp", 0.0),
            "actual_peak3_fp_pg": actual.get("peak3_fp_pg", 0.0),
            "actual_seasons_played": actual.get("seasons_played", 0),
            "actual_hit_label": _hit_actual(actual),
            "ktc_key": (bp._normalize_name(target.player_name), target.position),
        })
    log.info("Scored (bridge resolved): %d / %d", len(rows), len(holdouts))

    # ---- Hit@10 (top-50 by projection -> actual elite) ----
    rows_sorted = sorted(rows, key=lambda r: -r["projected_career_fp"])
    top50 = rows_sorted[:HIT_GATE_TOP_N]
    n_elite_in_top50 = sum(1 for r in top50 if r["actual_hit_label"] == "elite")
    hit_at_10 = n_elite_in_top50 / max(len(top50), 1)

    # ---- Bust@10 (bottom-50 of top-200 -> actual bust) ----
    top200 = rows_sorted[:BUST_GATE_BOTTOM_OF]
    bottom50 = top200[-BUST_GATE_BOTTOM_N:]
    n_bust_in_bottom = sum(1 for r in bottom50 if r["actual_hit_label"] == "bust")
    bust_at_10 = n_bust_in_bottom / max(len(bottom50), 1)

    # ---- Spearman ρ(model_rank, actual_rank) ----
    model_vals = [r["projected_career_fp"] for r in rows]
    actual_vals = [r["actual_career_fp"] for r in rows]
    rho = _spearman_rho(model_vals, actual_vals)

    # ---- KTC head-to-head ----
    # Pair prospects within the same KTC positional_tier; for each pair
    # compare whose model says is higher vs. whose actual career_fp is
    # higher. Skip pairs where the model is tied or where neither has
    # KTC data.
    ktc_pairs_total = 0
    ktc_pairs_model_wins = 0
    by_class_pos: Dict[Tuple[int, str], List[Dict]] = {}
    for r in rows:
        k = ktc.get(r["ktc_key"])
        if not k or k.get("ktc_pos_rank_sf") is None:
            r["_ktc_tier_sf"] = None
        else:
            # Use the SF rank to build tiers — same as the UI.
            r["_ktc_tier_sf"] = k.get("ktc_pos_rank_sf")
        by_class_pos.setdefault((r["class_year"], r["position"]), []).append(r)
    for (_cls, _pos), group in by_class_pos.items():
        kteer = [r for r in group if r.get("_ktc_tier_sf") is not None]
        if len(kteer) < 2:
            continue
        # Sort by KTC positional rank; group into 5-player ambiguity windows.
        kteer.sort(key=lambda r: r["_ktc_tier_sf"])
        for i in range(len(kteer)):
            for j in range(i + 1, len(kteer)):
                a, b = kteer[i], kteer[j]
                # Within-tier ambiguity = KTC ranks within 5 positions
                if abs(a["_ktc_tier_sf"] - b["_ktc_tier_sf"]) > 5:
                    continue
                # Actual winner
                if a["actual_career_fp"] == b["actual_career_fp"]:
                    continue
                actual_winner = a if a["actual_career_fp"] > b["actual_career_fp"] else b
                # Model winner
                if a["projected_career_fp"] == b["projected_career_fp"]:
                    continue
                model_winner = a if a["projected_career_fp"] > b["projected_career_fp"] else b
                # KTC winner = lower rank
                ktc_winner = a if a["_ktc_tier_sf"] < b["_ktc_tier_sf"] else b
                # Only score pairs where model disagrees with KTC (ambig.
                # is interesting case — if model agrees with KTC we
                # learn nothing new about whether model adds signal).
                if model_winner is ktc_winner:
                    continue
                ktc_pairs_total += 1
                if model_winner is actual_winner:
                    ktc_pairs_model_wins += 1
    ktc_h2h = ktc_pairs_model_wins / ktc_pairs_total if ktc_pairs_total else 0.0

    summary = {
        "n_holdouts": len(holdouts),
        "n_scored": len(rows),
        "hit_at_10": round(hit_at_10, 4),
        "hit_at_10_n": n_elite_in_top50,
        "hit_at_10_of": len(top50),
        "bust_at_10": round(bust_at_10, 4),
        "bust_at_10_n": n_bust_in_bottom,
        "bust_at_10_of": len(bottom50),
        "spearman_rho": round(rho, 4),
        "ktc_h2h": round(ktc_h2h, 4),
        "ktc_h2h_n": ktc_pairs_model_wins,
        "ktc_h2h_of": ktc_pairs_total,
        "per_class": {},
    }
    # Per-class breakdown (mostly informational, for the docs)
    by_class: Dict[int, List[Dict]] = {}
    for r in rows:
        by_class.setdefault(r["class_year"], []).append(r)
    for cls, cls_rows in sorted(by_class.items()):
        cls_sorted = sorted(cls_rows, key=lambda r: -r["projected_career_fp"])
        top10 = cls_sorted[:10]
        n_elite = sum(1 for r in top10 if r["actual_hit_label"] == "elite")
        summary["per_class"][cls] = {
            "n_scored": len(cls_rows),
            "top10_elite": n_elite,
        }
    summary["gates"] = _evaluate_gates(summary)
    return summary


def _evaluate_gates(summary: Mapping) -> Dict:
    """Compare each metric to its gate and classify pass / soft / hard."""
    out: Dict[str, Dict] = {}
    def _cls(metric, target):
        if metric >= target:
            return "pass"
        delta = (target - metric) / target if target else 1.0
        if delta <= SHIP_SOFT_FAIL_FRAC:
            return "soft_fail"
        return "hard_fail"

    out["hit_at_10"] = {"target": GATE_HIT_AT_10,
                        "metric": summary["hit_at_10"],
                        "status": _cls(summary["hit_at_10"], GATE_HIT_AT_10)}
    out["bust_at_10"] = {"target": GATE_BUST_AT_10,
                          "metric": summary["bust_at_10"],
                          "status": _cls(summary["bust_at_10"], GATE_BUST_AT_10)}
    out["spearman_rho"] = {"target": GATE_SPEARMAN,
                            "metric": summary["spearman_rho"],
                            "status": _cls(summary["spearman_rho"], GATE_SPEARMAN)}
    out["ktc_h2h"] = {"target": GATE_KTC_H2H,
                       "metric": summary["ktc_h2h"],
                       "status": _cls(summary["ktc_h2h"], GATE_KTC_H2H)}
    return out


def _overall_status(gates: Mapping[str, Mapping]) -> str:
    statuses = [g["status"] for g in gates.values()]
    if all(s == "pass" for s in statuses):
        return "pass"
    if any(s == "hard_fail" for s in statuses):
        return "hard_fail"
    return "soft_fail"


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def _format_report(summary: Mapping) -> str:
    g = summary["gates"]
    lines = [
        "=" * 64,
        "v3.0 prospect engine — back-test results",
        "=" * 64,
        f"Hold-outs total:  {summary['n_holdouts']}",
        f"Scored (bridged): {summary['n_scored']}",
        "",
        f"  Hit@10        : {summary['hit_at_10']:.1%}  "
        f"({summary['hit_at_10_n']}/{summary['hit_at_10_of']})  "
        f"target ≥ {GATE_HIT_AT_10:.0%}  [{g['hit_at_10']['status']}]",
        f"  Bust@10       : {summary['bust_at_10']:.1%}  "
        f"({summary['bust_at_10_n']}/{summary['bust_at_10_of']})  "
        f"target ≥ {GATE_BUST_AT_10:.0%}  [{g['bust_at_10']['status']}]",
        f"  Spearman ρ    : {summary['spearman_rho']:+.3f}  "
        f"target ≥ {GATE_SPEARMAN:.2f}  [{g['spearman_rho']['status']}]",
        f"  KTC H2H       : {summary['ktc_h2h']:.1%}  "
        f"({summary['ktc_h2h_n']}/{summary['ktc_h2h_of']})  "
        f"target ≥ {GATE_KTC_H2H:.0%}  [{g['ktc_h2h']['status']}]",
        "",
        "Per-class top-10 elites:",
    ]
    for cls, p in sorted(summary["per_class"].items()):
        lines.append(f"    {cls}: top-10 elites = {p['top10_elite']}  (n_scored={p['n_scored']})")
    lines.append("")
    lines.append(f"OVERALL: {_overall_status(g).upper()}")
    return "\n".join(lines)


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
    parser.add_argument("--nfl", type=Path,
                        default=Path("data/nflverse/player_stats_season.csv.gz"))
    parser.add_argument("--out", type=Path,
                        default=Path("data/engine_v3/backtest_results.json"))
    parser.add_argument("--top-k", type=int, default=bp.DEFAULT_TOP_K)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    corpus = bp._ensure_corpus(args.corpus, args.seasons, args.sos)
    log.info("Corpus loaded: %d ProspectVectors", len(corpus))
    resolver = NameCollisionResolver.from_file(args.bridge)
    ktc = bp._load_ktc(args.ktc)
    nfl_careers = bp._load_nfl_careers(args.nfl)
    log.info("NFL careers loaded: %d", len(nfl_careers))

    summary = evaluate(
        corpus=corpus,
        resolver=resolver,
        nfl_careers=nfl_careers,
        ktc=ktc,
        top_k=args.top_k,
    )

    print(_format_report(summary))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    log.info("Wrote %s", args.out)

    status = _overall_status(summary["gates"])
    if status == "pass":
        return 0
    if status == "soft_fail":
        return 2
    return 1


if __name__ == "__main__":  # pragma: no cover - manual CLI
    sys.exit(main())
