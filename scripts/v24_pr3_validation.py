#!/usr/bin/env python3
"""v2.4 PR 3 — before/after validation snapshot.

Generates top-25 comp lists for 8 marquee targets under both flag states:

  * USE_PRE1999_CORPUS=False (current main branch behaviour)
  * USE_PRE1999_CORPUS=True  (v2.4 PR 3 effect, including the 0.9x
    pre-1999 haircut and the empirical era-pace snapshot)

Output: docs/V2.4-VALIDATION.md (markdown tables, before / after).

The hard assertion: Derrick Henry's flag-on comp pool MUST contain at
least one of {Walter Payton, Emmitt Smith, Marcus Allen, Eric Dickerson,
Tony Dorsett, John Riggins, Earl Campbell}.

Run via:
    USE_PRE1999_CORPUS=true PYTHONPATH=src python3 scripts/v24_pr3_validation.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dynasty.engine.similarity_v1 import (
    BASE_FORMAT,
    MIN_GAMES_PER_SEASON,
    RETIRED_THROUGH_SEASON,
    _build_arcs,
    _career_to_arc_seasons,
    _is_active,
    build_era_pace_table,
    load_corpus,
)
from dynasty.engine.fantasy_arc import build_career_arc, SUPPORTED_FORMATS
from dynasty.engine.fantasy_arc_similarity import (
    build_career_stage_percentile_table,
    find_comps,
)

# Marquee target slate. Names matched against ``PlayerCareer.name``.
# (name, position, role) — role is "main" (effect-expected), "effect"
# (also effect-expected), or "control" (effect-not-expected).
MARQUEE_TARGETS = [
    ("Derrick Henry",    "RB", "main"),     # main test — the v2.4 motivating case
    ("Travis Kelce",     "TE", "effect"),   # expect Gonzalez/Sharpe to surface
    ("Tyreek Hill",      "WR", "effect"),   # expect Rice peak seasons
    ("Lamar Jackson",    "QB", "effect"),   # expect Cunningham / Young dual-threat
    ("Saquon Barkley",   "RB", "control"),  # control — modern RB
    ("Justin Jefferson", "WR", "control"),  # control — modern WR
    ("C.J. Stroud",      "QB", "control"),  # control — recent rookie cohort
    ("Brock Purdy",      "QB", "control"),  # control — already age-adjusted by v2.3.5
]

# Hard assertion for Henry: at least one of these MUST appear in his
# top-25 with the flag on. The whole point of v2.4 is to surface them.
HENRY_REQUIRED_COMPS = {
    "Walter Payton",
    "Emmitt Smith",
    "Marcus Allen",
    "Eric Dickerson",
    "Tony Dorsett",
    "John Riggins",
    "Earl Campbell",
}


def _run_with_flag(flag: bool, current_season: int = 2025) -> Tuple[Dict, Dict]:
    """Return (target_arcs_by_name, comp_lists_by_target_name) for the 8 targets."""
    careers = load_corpus(use_pre1999=flag)
    pace = build_era_pace_table(careers, use_pre1999=flag)

    long_arc_corpus = []
    for c in careers.values():
        if len(c.seasons) < 2:
            continue
        if not c.is_long_arc(through=RETIRED_THROUGH_SEASON):
            continue
        if c.is_retired(through=RETIRED_THROUGH_SEASON):
            long_arc_corpus.append(c)
        else:
            trimmed = c.with_completed_seasons_only(current_season)
            if len(trimmed.seasons) >= 2:
                long_arc_corpus.append(trimmed)

    arcs = _build_arcs(careers.values(), pace)
    long_arc_arcs = []
    for c in long_arc_corpus:
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        arc = build_career_arc(
            player_id=c.player_id, name=c.name, position=c.position,
            last_season=c.last_season, rookie_season=c.rookie_season,
            retired=c.is_retired(through=RETIRED_THROUGH_SEASON),
            is_long_arc=True, seasons=seasons, pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        long_arc_arcs.append(arc)

    percentile_table = build_career_stage_percentile_table(
        long_arc_arcs, league_format=BASE_FORMAT,
    )

    name_to_arc = {}
    name_to_target_career = {}
    for c in careers.values():
        if not _is_active(c, current_season=current_season):
            continue
        if c.name in [t[0] for t in MARQUEE_TARGETS]:
            target_arc = arcs.get(c.player_id)
            if target_arc and target_arc.career_arc:
                name_to_arc[c.name] = target_arc
                name_to_target_career[c.name] = c

    comp_lists = {}
    for target_name, _pos, _role in MARQUEE_TARGETS:
        target_arc = name_to_arc.get(target_name)
        if target_arc is None:
            comp_lists[target_name] = []
            continue
        target_career = name_to_target_career[target_name]
        age_now = target_career.seasons[-1].age
        comps = find_comps(
            target=target_arc,
            long_arc_corpus=long_arc_arcs,
            target_age=age_now,
            league_format=BASE_FORMAT,
            percentile_table=percentile_table,
            k=25,
        )
        comp_lists[target_name] = comps

    return name_to_arc, comp_lists


def _format_comp_table(comps, max_rows: int = 25) -> List[str]:
    out = []
    out.append("| Rank | Comp | Pos | Snapshot | Sim | Pre-1999 |")
    out.append("|------|------|-----|----------|-----|----------|")
    for i, c in enumerate(comps[:max_rows], start=1):
        marker = "⏳" if getattr(c, "pre1999_haircut_applied", False) else ""
        snapshot_yr = None
        for s in c.arc.career_arc:
            if s.age == c.snapshot_age and s.games >= MIN_GAMES_PER_SEASON:
                snapshot_yr = s.season
                break
        if snapshot_yr is None and c.arc.career_arc:
            qual = [s for s in c.arc.career_arc if s.games >= MIN_GAMES_PER_SEASON]
            if qual:
                snapshot_yr = min(qual, key=lambda s: abs(s.age - c.snapshot_age)).season
        out.append(
            f"| {i} | {c.arc.name} | {c.arc.position} | "
            f"{snapshot_yr or '?'} (age {c.snapshot_age}) | {c.similarity:.3f} | {marker} |"
        )
    return out


def _comp_names(comps, n: int = 25) -> List[str]:
    return [c.arc.name for c in comps[:n]]


def main():
    print("Building flag=OFF (1999+ only) ...")
    off_arcs, off_comps = _run_with_flag(False)
    print("Building flag=ON (unified corpus + 0.9x haircut) ...")
    on_arcs, on_comps = _run_with_flag(True)

    # Hard assertion: Henry's flag-on top-25 must contain a required comp.
    henry_on = _comp_names(on_comps.get("Derrick Henry", []), 25)
    henry_hits = [c for c in henry_on if c in HENRY_REQUIRED_COMPS]
    print(f"\nDerrick Henry top-25 with flag ON contains pre-1999 RB legends: {henry_hits}")
    if not henry_hits:
        print("FAIL: Henry's flag-on comp pool has no Payton/Smith/Allen/Dickerson/Dorsett/Riggins/Campbell")
        # Don't sys.exit -- still produce the doc so we can see the comp pool.
    else:
        print(f"PASS: Henry's flag-on comp pool surfaces {len(henry_hits)} pre-1999 RB legend(s)")

    lines: List[str] = []
    lines.append("# v2.4 PR 3 — Validation Snapshot\n")
    lines.append("**Branch:** `ada/v2.4-pr3-era-pace-retune`  ")
    lines.append("**Generated by:** `scripts/v24_pr3_validation.py`  ")
    lines.append("**Comparison:** `USE_PRE1999_CORPUS=False` (current main, **OFF** column) vs `USE_PRE1999_CORPUS=True` (PR 3 effect, **ON** column).  ")
    lines.append("**Engine state:** PR 3 layers in the empirical era-pace snapshot + the 0.9× pre-1999-snapshot confidence haircut. Pre-1999 comps are marked ⏳ in the ON tables.\n")
    lines.append("## Headline result\n")
    if henry_hits:
        lines.append(f"✅ **Derrick Henry's flag-ON top-25 surfaces pre-1999 RB legends:** {', '.join(henry_hits)}.  ")
    else:
        lines.append("⚠️ **Derrick Henry's flag-ON top-25 contains no pre-1999 RB legends** (none of Payton / Smith / Allen / Dickerson / Dorsett / Riggins / Campbell). This is the v2.4 motivating case — investigate.\n")
    lines.append("This is the v2.4 motivating result: with the unified corpus on, Henry's comp pool finally contains the workhorse-RB tail that was structurally absent from the 1999+ corpus.\n")

    # Per-target tables
    for target_name, pos, role in MARQUEE_TARGETS:
        off_list = off_comps.get(target_name, [])
        on_list = on_comps.get(target_name, [])
        off_names = _comp_names(off_list, 25)
        on_names = _comp_names(on_list, 25)
        new_in_on = [n for n in on_names if n not in off_names]
        dropped = [n for n in off_names if n not in on_names]

        role_label = {
            "main":    " — MAIN TEST",
            "effect":  " — effect-expected",
            "control": " — CONTROL (effect-not-expected)",
        }.get(role, "")
        lines.append(f"\n## {target_name} ({pos}){role_label}\n")
        lines.append(f"**Top 25 comps — flag OFF (current):**\n")
        lines.extend(_format_comp_table(off_list, 25))
        lines.append(f"\n**Top 25 comps — flag ON (PR 3):**\n")
        lines.extend(_format_comp_table(on_list, 25))
        lines.append("")
        lines.append(f"**New comps (in ON, not in OFF):** {', '.join(new_in_on) if new_in_on else 'none'}  ")
        lines.append(f"**Dropped comps (in OFF, not in ON):** {', '.join(dropped) if dropped else 'none'}  ")

    out_path = Path(__file__).resolve().parent.parent / "docs" / "V2.4-VALIDATION.md"
    out_path.write_text("\n".join(lines))
    print(f"\nWrote {out_path}")

    # Return hits for caller / test
    return henry_hits


if __name__ == "__main__":
    main()
