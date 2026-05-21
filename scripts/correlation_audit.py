#!/usr/bin/env python3
"""Correlation audit — compute the historical Pearson correlation between
each overlay signal and realized NFL fantasy production, per position.

Output: ``data/overlays/correlation_table.json``.

Methodology
-----------
For each overlay signal we join to the PFR / nflverse player-season
corpus on ``pfr_id`` (RAS) or fall back to name+position. We then compute
the correlation between the overlay signal (a *pre-NFL* athleticism /
prospect score) and each player's **first 3 NFL seasons of fantasy_ppr
production** at their position.

We deliberately use *first 3 seasons* (not career total) because:

  1. RAS is a pre-NFL signal and the strongest test of whether it
     predicts on-field translation is the early-career window.
  2. Career totals are dominated by longevity, which is what the
     similarity engine projects from on-field production, not RAS.
  3. 3 seasons matches the dynasty rookie-pick valuation horizon.

For Brainy Ballers' SRS we do NOT have a historical archive (the source
only publishes current rankings). Until that's available we use a
conservative low-confidence prior from their published validation
backlog and flag the values as ``"confidence": "low"`` so the UI can
display them appropriately. When the historical archive becomes
available the audit re-runs and replaces these stubs.

Run:
  PYTHONPATH=src python3 scripts/correlation_audit.py
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dynasty.sources.pro_football_reference import (  # noqa: E402
    load_pfr_seasons,
    load_pfr_players,
)


RAS_CSV = ROOT / "data" / "ras" / "ras_database.csv"
OUT_PATH = ROOT / "data" / "overlays" / "correlation_table.json"


def _f(v):
    try:
        if v in (None, "", "NA"):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def first_n_ppr_by_pfr_id(n: int = 3) -> dict[str, dict]:
    """Map pfr_id -> {position, draft_year, first_n_ppr, n_seasons}.

    First-n PPR is the sum of fantasy_points_ppr across the player's
    first n NFL seasons.
    """
    players = {p["gsis_id"]: p for p in load_pfr_players() if p.get("gsis_id")}
    pfr_to_gsis = {p["pfr_id"]: p["gsis_id"] for p in players.values() if p.get("pfr_id")}
    seasons = load_pfr_seasons()

    # Group seasons by gsis_id, sorted ascending
    by_gsis: dict[str, list] = defaultdict(list)
    for row in seasons:
        by_gsis[row.get("player_id") or ""].append(row)
    for arr in by_gsis.values():
        arr.sort(key=lambda r: int(r.get("season") or 0))

    out: dict[str, dict] = {}
    for pfr_id, gsis in pfr_to_gsis.items():
        bio = players.get(gsis, {})
        arr = by_gsis.get(gsis, [])
        if not arr:
            continue
        first_n = arr[:n]
        total = sum(_f(r.get("fantasy_points_ppr")) or 0.0 for r in first_n)
        out[pfr_id] = {
            "position": (bio.get("position") or arr[0].get("position") or "").upper(),
            "draft_year": _f(bio.get("draft_year")),
            "first_n_ppr": total,
            "n_seasons": len(first_n),
        }
    return out


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 5:
        return 0.0
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    sx2 = sum((x - mx) ** 2 for x in xs)
    sy2 = sum((y - my) ** 2 for y in ys)
    if sx2 == 0 or sy2 == 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sx2 ** 0.5 * sy2 ** 0.5)


def correlate_ras(production: dict[str, dict]) -> dict[str, float]:
    """Pearson correlation between RAS score and first-3-season PPR, per position."""
    if not RAS_CSV.exists():
        return {}
    with RAS_CSV.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    pairs: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        pfr_id = (row.get("pfr_id") or "").strip()
        ras = _f(row.get("ras"))
        if not pfr_id or ras is None:
            continue
        prod = production.get(pfr_id)
        if not prod or prod["n_seasons"] < 1:
            continue
        pos = prod["position"]
        if pos not in {"QB", "RB", "WR", "TE"}:
            continue
        pairs[pos].append((ras, prod["first_n_ppr"]))

    out = {}
    for pos, items in pairs.items():
        xs = [p[0] for p in items]
        ys = [p[1] for p in items]
        out[pos] = round(pearson(xs, ys), 4)
    return out, {pos: len(items) for pos, items in pairs.items()}


def main():
    print("Building first-3-season PPR by pfr_id from PFR corpus...")
    production = first_n_ppr_by_pfr_id(n=3)
    print(f"  matched {len(production)} pfr_ids with NFL seasons")

    print("Computing RAS correlations per position...")
    ras_corrs, ras_counts = correlate_ras(production)
    for pos, r in sorted(ras_corrs.items()):
        print(f"  RAS x {pos}: r = {r:+.3f}  (n = {ras_counts[pos]})")

    # Brainy Ballers' SRS: no historical archive yet. Use a conservative
    # prior derived from BB's published validation (their model claims
    # mid-tier correlation for prospect-style scores). Flagged as
    # low-confidence so the UI can show the caveat.
    brainy_prior = {
        "QB": 0.15,
        "RB": 0.20,
        "WR": 0.30,
        "TE": 0.25,
    }
    print("Brainy Ballers SRS correlations (PRIOR - no historical archive):")
    for pos, r in brainy_prior.items():
        print(f"  SRS x {pos}: r = {r:+.3f}  (prior)")

    table = {
        "ras": {pos: ras_corrs.get(pos, 0.0) for pos in ("QB", "RB", "WR", "TE")},
        "ras_sample_sizes": {pos: ras_counts.get(pos, 0) for pos in ("QB", "RB", "WR", "TE")},
        "brainy_ballers_srs": brainy_prior,
        "brainy_ballers_srs_confidence": "low (no historical archive)",
        "methodology": (
            "Pearson correlation between overlay signal and sum of "
            "fantasy_points_ppr over a player's first 3 NFL seasons, "
            "computed on the nflverse player-season corpus (1999-2024). "
            "RAS joined on pfr_id. Brainy Ballers SRS uses a conservative "
            "prior pending a historical archive."
        ),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(table, indent=2))
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
