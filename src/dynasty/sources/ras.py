"""Relative Athletic Score (RAS) adapter — Kent Lee Platte's free public database.

RAS is a 0–10 position-adjusted composite of NFL Combine + Pro Day testing
(40-yard, vertical, broad, shuttle, 3-cone, bench, height, weight). It's the
best free single-number athleticism score available.

Why it matters
--------------
Per the research doc §A2:

- WR / TE / RB studies (Brainy Ballers, Sharp Football, PFF) show a small
  but positive correlation between RAS and NFL fantasy production
  (~0.10–0.15 Pearson for WR).
- *Low* RAS is a much stronger negative filter than *high* RAS is a positive
  signal. RAS is most useful as a **bust filter**, especially for prospects
  going earlier than their athleticism suggests they should.

Data source
-----------
Kent Lee Platte publishes RAS on https://ras.football/ and posts per-player
scores live on social media each Combine week. He explicitly encourages
redistribution with attribution but does not (as of 2026-05) host a stable
public download URL.

The model is set up to read from a local CSV at ``data/ras/ras_database.csv``
(path overridable via ``DYNASTY_RAS_CSV_PATH``). Expected schema is the format
Kent's spreadsheet exports use; we tolerate any column-naming variant we've
seen.

Expected columns (case-insensitive, any of these aliases per field):

    name           : Name | Player | full_name
    position       : Pos | Position
    college        : College | school
    draft_year     : Year | season | draft_year
    ras_score      : RAS | RAS_Score | score | composite

Optional component columns (we just keep RAS itself for now):
    height, weight, forty, vertical, broad, shuttle, three_cone, bench

If the CSV is missing, the adapter yields nothing (rather than erroring) so
``sync-all`` keeps working. Adding RAS data is then a one-file drop-in.

Output
------
For each player with a usable RAS score in the most recent N draft classes
(default 6), we emit a per-position rookie-only ranking where rank = position
order by descending RAS *within that draft year*. So in the 2026 class,
the WR with the highest RAS gets rank 1, second-highest rank 2, etc. The
scorer treats these like any other per-format ranking.

We also enrich the canonical Player row with college / draft_year / position
when those fields are currently NULL, so resolution stays sticky.
"""
from __future__ import annotations
import csv
import os
from collections import defaultdict
from datetime import datetime
from typing import Iterator, Optional

from .base import BaseSource, RankingRecord


DEFAULT_CSV_PATH = "data/ras/ras_database.csv"

_HEADER_ALIASES = {
    "name":        ("name", "player", "full_name", "playername"),
    "position":    ("pos", "position", "primary_position"),
    "college":     ("college", "school"),
    "draft_year":  ("year", "season", "draft_year", "draftyear", "class"),
    "ras":         ("ras", "ras_score", "score", "composite", "ras_grade"),
}


def _norm_key(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_").replace("-", "_")


def _pick(row: dict, aliases: tuple[str, ...]) -> Optional[str]:
    for k in aliases:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _floatish(v) -> Optional[float]:
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _intish(v) -> Optional[int]:
    f = _floatish(v)
    if f is None:
        return None
    try:
        return int(f)
    except (ValueError, TypeError):
        return None


class RAS(BaseSource):
    slug = "ras"
    name = "RAS — Relative Athletic Score (Kent Lee Platte)"
    category = "model"
    update_frequency = "event"  # annual, around Combine + Pro Days
    tos_compliant = True
    # Modest weight (research §A2 recommends 0.8). The position-aware
    # weighting in PR #6 will let us boost WR/TE/RB and cancel it for QB.
    default_weight = 0.8
    homepage = "https://ras.football/"
    notes = (
        "Local CSV ingestion (set DYNASTY_RAS_CSV_PATH or drop file at "
        "data/ras/ras_database.csv). Best free athleticism composite; "
        "most useful as a bust filter."
    )

    LEAGUE_FORMAT = "sf_ppr"
    DEFAULT_EMIT_YEARS_BACK = 6

    # Only emit rankings for fantasy skill positions.
    _SKILL = {"QB", "RB", "WR", "TE", "FB"}

    def __init__(
        self,
        *args,
        csv_path: Optional[str] = None,
        emit_years_back: int = DEFAULT_EMIT_YEARS_BACK,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.csv_path = (
            csv_path
            or os.environ.get("DYNASTY_RAS_CSV_PATH")
            or DEFAULT_CSV_PATH
        )
        self.emit_years_back = emit_years_back

    def _read_rows(self) -> list[dict]:
        if not os.path.exists(self.csv_path):
            return []
        with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = []
            for raw in reader:
                row = {_norm_key(k): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
                rows.append(row)
        return rows

    def fetch(self) -> Iterator[RankingRecord]:
        rows = self._read_rows()
        if not rows:
            return iter([])

        cutoff_year = datetime.utcnow().year - self.emit_years_back

        # 1. Parse and filter rows.
        parsed: list[dict] = []
        for row in rows:
            name = _pick(row, _HEADER_ALIASES["name"])
            if not name:
                continue
            pos = (_pick(row, _HEADER_ALIASES["position"]) or "").upper()
            if pos == "FB":
                pos = "RB"
            if pos not in self._SKILL:
                continue
            ras = _floatish(_pick(row, _HEADER_ALIASES["ras"]))
            if ras is None:
                continue
            draft_year = _intish(_pick(row, _HEADER_ALIASES["draft_year"]))
            college = _pick(row, _HEADER_ALIASES["college"])
            parsed.append({
                "name": name.strip(),
                "position": pos,
                "draft_year": draft_year,
                "college": (college.strip() if isinstance(college, str) else None),
                "ras": ras,
            })

        # 2. Compute per-position-per-year ranks (1 = best RAS).
        by_year_pos: dict[tuple[Optional[int], str], list[dict]] = defaultdict(list)
        for p in parsed:
            by_year_pos[(p["draft_year"], p["position"])].append(p)
        for key, group in by_year_pos.items():
            group.sort(key=lambda x: x["ras"], reverse=True)
            for i, row in enumerate(group, start=1):
                row["pos_rank"] = i

        # 3. Emit.
        for p in parsed:
            year = p["draft_year"]
            in_emit_window = year is not None and year >= cutoff_year
            yield RankingRecord(
                source_slug=self.slug,
                full_name=p["name"],
                position=p["position"],
                college=p["college"],
                draft_year=year,
                # Use position-rank as both overall_rank and position_rank.
                # Scoring uses overall_rank with depth-normalization; since
                # RAS rankings are per-position, the "depth" is really
                # per-position class size. A position-aware scoring path
                # (PR #6) will make this far more meaningful. Until then,
                # the depth=300 default flattens these contributions which
                # is fine — RAS is a small-weight signal anyway.
                overall_rank=p["pos_rank"] if in_emit_window else None,
                position_rank=p["pos_rank"] if in_emit_window else None,
                # Stash the raw RAS as market_value (0..10 → renormalized in
                # scoring by source-max). This lets RAS flow through the
                # value-based normalization branch with a meaningful range.
                market_value=p["ras"] if in_emit_window else None,
                league_format=self.LEAGUE_FORMAT,
                is_dynasty=True,
                # Mark all as rookie-only for now — RAS is fundamentally a
                # pre-draft / draft-class signal. Veteran rankings should
                # rely on production, not their Combine 7 years ago.
                is_rookie_only=in_emit_window,
            )
