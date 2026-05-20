"""Generate a computed-RAS CSV from nflverse Combine data.

NOT a substitute for Kent Lee Platte's canonical RAS.football database.
This is a transparent re-implementation of the *idea* (position-adjusted
z-scores of Combine measurements, mapped to a 0-10 scale) using only the
public nflverse Combine CSV.

Differences from canonical RAS:
  - Uses raw nflverse Combine data only. Kent's database also incorporates
    Pro Day results when Combine numbers are missing — we don't.
  - Peer cohort is fantasy-skill players (QB/RB/WR/TE) from 2000 onward, not
    1987-onward including all positions.
  - Z-score → 0-10 mapping uses min/max within the per-position cohort
    rather than Kent's bespoke distribution mapping.
  - No 3-cone, shuttle, or broad-jump weighting tweaks (we treat all eight
    measurements equally when present).

So treat the output as "approximate Combine athleticism" rather than canonical
RAS. The `cfbd_breakouts` adapter's column-alias parser handles whatever
column names we ship, so swapping in Kent's real CSV later is one file drop.

Usage:
    python scripts/build_ras_csv.py
    # writes data/ras/ras_database.csv (and overwrites if present)

Reads nflverse Combine CSV at
    https://github.com/nflverse/nflverse-data/releases/download/combine/combine.csv
"""
from __future__ import annotations
import csv
import io
import os
import statistics
import sys
from collections import defaultdict
from urllib.request import urlopen


COMBINE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "combine/combine.csv"
)
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "ras", "ras_database.csv",
)

# Skill positions we care about. nflverse uses these short codes.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "FB"}

# Measurements to include + whether higher = better.
# (col_name, higher_is_better)
MEASUREMENTS = [
    ("forty",      False),  # lower 40-time = better
    ("bench",      True),
    ("vertical",   True),
    ("broad_jump", True),
    ("cone",       False),
    ("shuttle",    False),
    ("ht",         True),   # height (parsed below)
    ("wt",         True),
]


def _parse_height(value: str) -> float | None:
    """Parse '6-3' (feet-inches) → total inches; otherwise return None."""
    if not value:
        return None
    s = str(value).strip()
    if "-" in s:
        try:
            ft, inch = s.split("-", 1)
            return float(ft) * 12.0 + float(inch)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _floatish(value) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def fetch_combine_csv() -> list[dict]:
    print(f"Fetching {COMBINE_URL}")
    with urlopen(COMBINE_URL) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        pos = (row.get("pos") or "").strip().upper()
        if pos not in SKILL_POSITIONS:
            continue
        if pos == "FB":
            pos = "RB"
        row["pos"] = pos
        # Parse height into inches up front.
        ht = _parse_height(row.get("ht", ""))
        row["ht"] = ht
        for k, _ in MEASUREMENTS:
            if k == "ht":
                continue
            row[k] = _floatish(row.get(k))
        rows.append(row)
    return rows


def compute_ras(rows: list[dict]) -> list[dict]:
    """Per-position z-scores → 0-10 RAS.

    For each measurement and position cohort:
      1. Drop None.
      2. Compute mean + stdev.
      3. Z-score each player (negate if lower-is-better so higher = better).
      4. Map per-cohort min..max → 0..10.

    Per-player RAS = average of the per-measurement 0-10 scores (only over
    measurements they actually completed).
    """
    cohorts: dict[str, dict[str, list[float | None]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for k, higher_is_better in MEASUREMENTS:
            v = r.get(k)
            cohorts[r["pos"]][k].append(v if v is not None else None)

    # Per (pos, measurement) z-score parameters.
    stats: dict[tuple[str, str], tuple[float, float, bool]] = {}
    for pos, by_metric in cohorts.items():
        for metric, vals in by_metric.items():
            higher_is_better = next(b for k, b in MEASUREMENTS if k == metric)
            clean = [v for v in vals if v is not None]
            if len(clean) < 5:
                continue
            mean = statistics.mean(clean)
            stdev = statistics.pstdev(clean) or 1.0
            stats[(pos, metric)] = (mean, stdev, higher_is_better)

    # For each player, compute per-measurement z then map to 0-10 vs the
    # cohort's z-distribution min/max.
    cohort_z_min: dict[tuple[str, str], float] = {}
    cohort_z_max: dict[tuple[str, str], float] = {}
    for (pos, metric), (mean, stdev, higher_is_better) in stats.items():
        vals = cohorts[pos][metric]
        z_values: list[float] = []
        for v in vals:
            if v is None:
                continue
            z = (v - mean) / stdev
            if not higher_is_better:
                z = -z
            z_values.append(z)
        if z_values:
            cohort_z_min[(pos, metric)] = min(z_values)
            cohort_z_max[(pos, metric)] = max(z_values)

    out_rows = []
    for r in rows:
        pos = r["pos"]
        per_metric_score: list[float] = []
        per_metric_breakdown: dict[str, float] = {}
        for metric, higher_is_better in MEASUREMENTS:
            v = r.get(metric)
            key = (pos, metric)
            if v is None or key not in stats:
                continue
            mean, stdev, hib = stats[key]
            z = (v - mean) / stdev
            if not hib:
                z = -z
            zmin = cohort_z_min[key]
            zmax = cohort_z_max[key]
            if zmax == zmin:
                m_score = 5.0
            else:
                m_score = 10.0 * (z - zmin) / (zmax - zmin)
                m_score = max(0.0, min(10.0, m_score))
            per_metric_score.append(m_score)
            per_metric_breakdown[metric] = round(m_score, 2)

        if not per_metric_score:
            continue

        ras = round(statistics.mean(per_metric_score), 2)
        out_rows.append({
            "name": (r.get("player_name") or "").strip(),
            "position": pos,
            "college": (r.get("school") or "").strip() or None,
            "draft_year": r.get("draft_year") or r.get("season"),
            "ras": ras,
            "metrics_used": len(per_metric_score),
            "forty": r.get("forty"),
            "vertical": r.get("vertical"),
            "broad_jump": r.get("broad_jump"),
            "shuttle": r.get("shuttle"),
            "cone": r.get("cone"),
            "bench": r.get("bench"),
            "ht_inches": r.get("ht"),
            "wt_lbs": r.get("wt"),
            "pfr_id": r.get("pfr_id"),
            "source": "nflverse-combine-derived",
        })

    return out_rows


def write_csv(rows: list[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        print("No rows to write.")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {len(rows)} rows to {path}")


def main():
    rows = fetch_combine_csv()
    print(f"Fetched {len(rows)} skill-position combine entries")
    scored = compute_ras(rows)
    print(f"Scored {len(scored)} players with computed-RAS")
    write_csv(scored, OUTPUT_PATH)


if __name__ == "__main__":
    main()
