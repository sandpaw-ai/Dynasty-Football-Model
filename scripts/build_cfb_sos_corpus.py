#!/usr/bin/env python3
"""Build the v3.0 College Football SOS corpus.

Fetches sports-reference CFB standings via Wayback for 2000-2025, parses
per-team Strength-of-Schedule (SOS) + Simple Rating System (SRS) values,
normalizes school + conference names, computes per-year corpus stats,
and writes:

* ``data/sos/team_sos_{year}.csv``     - per-year, one row per team
* ``data/sos/team_sos_all.csv.gz``     - unified all-years concatenation
* ``data/sos/corpus_stats.json``       - per-year median/sd for normalization

Usage:

    python scripts/build_cfb_sos_corpus.py [--start YEAR] [--end YEAR]

Re-runs are cheap: HTML is cached at ``data/sr_cache/standings/{year}.html``
and skipped on second pass. The cache is shared with v3.0 PR 1.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import statistics
import sys
from pathlib import Path

# Make src/ importable when run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = _REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty.sources.sports_reference_cfb_standings import (  # noqa: E402
    fetch_standings,
    parse_standings,
)


log = logging.getLogger("build_cfb_sos_corpus")


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

OUT_DIR = _REPO_ROOT / "data" / "sos"


# Columns in the per-year CSV and unified CSV (same schema for both).
CSV_COLUMNS = [
    "year",
    "school",
    "school_canonical_slug",
    "conference",
    "conference_tier",
    "wins",
    "losses",
    "srs",
    "sos",
    "srs_rank",
    "sos_rank",
]


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------

def _assign_ranks(rows: list[dict]) -> None:
    """Mutate ``rows`` to add ``srs_rank`` + ``sos_rank`` (1 = highest).

    Rows with missing SOS or SRS get ``None`` for that rank. Ties get
    dense rank by sorted-stable order (Pandas-style ``method='min'``
    would be a follow-up nice-to-have but isn't needed for downstream
    use).
    """
    def _rank_field(field: str, rank_field: str) -> None:
        ranked = sorted(
            [r for r in rows if r.get(field) is not None],
            key=lambda r: r[field],
            reverse=True,
        )
        for i, r in enumerate(ranked, start=1):
            r[rank_field] = i
        for r in rows:
            r.setdefault(rank_field, None)

    _rank_field("srs", "srs_rank")
    _rank_field("sos", "sos_rank")


def _corpus_stats_for_year(rows: list[dict]) -> dict:
    """Compute per-year summary stats for downstream normalization."""
    sos_values = [r["sos"] for r in rows if r.get("sos") is not None]
    srs_values = [r["srs"] for r in rows if r.get("srs") is not None]

    def _safe_median(xs):
        return statistics.median(xs) if xs else None

    def _safe_pstdev(xs):
        # Population std-dev: we treat the year's teams as the full cohort.
        return statistics.pstdev(xs) if len(xs) >= 2 else None

    # Bucket team counts by tier.
    n_by_tier: dict[str, int] = {}
    for r in rows:
        t = r.get("conference_tier") or "Unknown"
        n_by_tier[t] = n_by_tier.get(t, 0) + 1

    return {
        "median_sos": _safe_median(sos_values),
        "sd_sos": _safe_pstdev(sos_values),
        "median_srs": _safe_median(srs_values),
        "sd_srs": _safe_pstdev(srs_values),
        "n_teams_total": len(rows),
        "n_teams_p5": n_by_tier.get("P5", 0),
        "n_teams_g5_top": n_by_tier.get("G5_top", 0),
        "n_teams_g5": n_by_tier.get("G5", 0),
        "n_teams_fcs": n_by_tier.get("FCS", 0),
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _write_per_year_csv(year: int, rows: list[dict]) -> Path:
    path = OUT_DIR / f"team_sos_{year}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k) for k in CSV_COLUMNS})
    return path


def _write_unified_csv_gz(all_rows: list[dict]) -> Path:
    path = OUT_DIR / "team_sos_all.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in all_rows:
            writer.writerow({k: r.get(k) for k in CSV_COLUMNS})
    return path


def _write_corpus_stats(stats_by_year: dict) -> Path:
    path = OUT_DIR / "corpus_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(stats_by_year, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------
# Smoke aggregate (printed at end of run)
# ---------------------------------------------------------------------------

def _print_smoke_aggregate(all_rows: list[dict]) -> None:
    """Print mean SOS by tier + mean SRS per conference for sanity-check."""
    # Mean SOS by conference_tier, across all years.
    by_tier: dict[str, list[float]] = {}
    for r in all_rows:
        sos = r.get("sos")
        t = r.get("conference_tier")
        if sos is None or t is None:
            continue
        by_tier.setdefault(t, []).append(sos)
    print("\nMean SOS by conference_tier (all 26 seasons):")
    tier_order = ["P5", "G5_top", "G5", "FCS"]
    for t in tier_order:
        vals = by_tier.get(t, [])
        if not vals:
            continue
        print(f"  {t:<7}  n={len(vals):>5}  mean SOS = {statistics.mean(vals):+.3f}")
    extras = [t for t in by_tier if t not in tier_order]
    for t in sorted(extras):
        vals = by_tier[t]
        print(f"  {t:<7}  n={len(vals):>5}  mean SOS = {statistics.mean(vals):+.3f}")

    # Mean SRS by conference, top 5 across the window.
    by_conf: dict[str, list[float]] = {}
    for r in all_rows:
        srs = r.get("srs")
        c = r.get("conference")
        if srs is None or not c:
            continue
        by_conf.setdefault(c, []).append(srs)
    # Sort by mean SRS, descending, considering only conferences with
    # enough samples to be meaningful (avoid 4-team conferences winning).
    eligible = [
        (c, statistics.mean(v), len(v))
        for c, v in by_conf.items() if len(v) >= 40
    ]
    eligible.sort(key=lambda t: t[1], reverse=True)
    print("\nMean SRS by conference (top 10, ≥40 team-seasons):")
    for c, m, n in eligible[:10]:
        print(f"  {c:<22}  n={n:>4}  mean SRS = {m:+.3f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=2000)
    parser.add_argument("--end", type=int, default=2025)
    parser.add_argument(
        "--verbose", "-v", action="count", default=0,
        help="Increase log verbosity (-v for INFO, -vv for DEBUG).",
    )
    args = parser.parse_args(argv)

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=max(level, logging.DEBUG),
    )

    all_rows: list[dict] = []
    stats_by_year: dict[str, dict] = {}

    for year in range(args.start, args.end + 1):
        log.info("processing %d", year)
        html = fetch_standings(year)
        rows = parse_standings(html, year)
        if not rows:
            log.warning("year %d: 0 rows parsed (skipping)", year)
            continue

        _assign_ranks(rows)
        _write_per_year_csv(year, rows)
        all_rows.extend(rows)

        stats_by_year[str(year)] = _corpus_stats_for_year(rows)
        log.info("  %d teams; FBS-ish: P5=%d G5_top=%d G5=%d FCS=%d",
                 len(rows),
                 stats_by_year[str(year)]["n_teams_p5"],
                 stats_by_year[str(year)]["n_teams_g5_top"],
                 stats_by_year[str(year)]["n_teams_g5"],
                 stats_by_year[str(year)]["n_teams_fcs"])

    _write_unified_csv_gz(all_rows)
    _write_corpus_stats(stats_by_year)

    print(f"\n=== v3.0 SOS corpus build complete ===")
    print(f"Years:           {args.start}-{args.end}")
    print(f"Team-seasons:    {len(all_rows)}")
    print(f"Years processed: {len(stats_by_year)}")
    print(f"Output dir:      {OUT_DIR}")

    _print_smoke_aggregate(all_rows)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
