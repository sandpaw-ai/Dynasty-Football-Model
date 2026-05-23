"""Build ``data/nflverse/players_pre1999.csv.gz`` — sidecar player records.

The v2.4 unified corpus loader concatenates
``data/nflverse/player_stats_season.csv.gz`` (1999+, nflverse) with
``data/nflverse/player_stats_season_pre1999.csv.gz`` (1980-1998, scraped
from Pro-Football-Reference in PR 1). Most of the pre-1999 PFR player
universe (1,338 of 1,355) already has a row in nflverse's
``players.csv.gz`` via the PFR↔gsis crosswalk maintained by the
nflverse maintainers, even for guys who retired before 1999 (Walter
Payton, Earl Campbell, Tony Dorsett, etc. — nflverse keeps them around
for historical references and synthetic gsis_ids like ``PAY738296``).

What this script does
---------------------

1. Identify the 17-ish ``pfr_id`` values that appear in the pre-1999
   corpus but do NOT have a row in nflverse's ``players.csv.gz``.
2. Build minimal ``players`` rows for them, using whatever metadata
   the pre-1999 corpus exposes (display name, position, last team) and
   the birth-date sidecar (``data/pfr_birth_dates.csv``) when
   available. ``rookie_season`` and ``last_season`` are derived from
   the player's first and last appearance in the pre-1999 corpus.
3. Write the resulting frame to ``data/nflverse/players_pre1999.csv.gz``
   with the *same column structure* as ``players.csv.gz``, padded
   with empty strings / NaN where PFR doesn't expose the field.

The loader concatenates ``players.csv.gz`` + ``players_pre1999.csv.gz``
on read. The original nflverse file stays pristine.

This script is idempotent. Re-run it any time the pre-1999 corpus or
nflverse ``players.csv.gz`` are refreshed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


_REPO_ROOT = Path(__file__).resolve().parents[1]
NFLVERSE_DIR = _REPO_ROOT / "data" / "nflverse"
PRE_CORPUS = NFLVERSE_DIR / "player_stats_season_pre1999.csv.gz"
PLAYERS_NFLVERSE = NFLVERSE_DIR / "players.csv.gz"
PFR_BIRTH_DATES = _REPO_ROOT / "data" / "pfr_birth_dates.csv"
OUTPUT_PATH = NFLVERSE_DIR / "players_pre1999.csv.gz"


def _normalize_name(display_name: str) -> tuple[str, str]:
    """Split ``display_name`` into (first, last). Best-effort: trailing
    Roman numerals and ``Jr.`` / ``Sr.`` are dropped from the last name.
    """
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    first = parts[0]
    # Drop trailing suffix tokens.
    last_tokens = parts[1:]
    suffixes = {"Jr.", "Sr.", "II", "III", "IV", "Jr", "Sr"}
    while last_tokens and last_tokens[-1] in suffixes:
        last_tokens.pop()
    last = " ".join(last_tokens) if last_tokens else parts[-1]
    return first, last


def main() -> int:
    if not PRE_CORPUS.exists():
        print(f"ERROR: missing {PRE_CORPUS}", file=sys.stderr)
        return 1
    if not PLAYERS_NFLVERSE.exists():
        print(f"ERROR: missing {PLAYERS_NFLVERSE}", file=sys.stderr)
        return 1

    pre = pd.read_csv(PRE_CORPUS)
    players = pd.read_csv(PLAYERS_NFLVERSE)

    # All ``pfr_X`` ids in the pre-1999 corpus.
    pre_pfr = {
        pid.replace("pfr_", "")
        for pid in pre["player_id"].dropna().unique()
        if isinstance(pid, str) and pid.startswith("pfr_")
    }
    known_pfr = set(players["pfr_id"].dropna())
    missing = sorted(pre_pfr - known_pfr)
    print(f"pre-1999 unique pfr_ids: {len(pre_pfr)}")
    print(f"already in nflverse players.csv.gz: {len(pre_pfr & known_pfr)}")
    print(f"sidecar will add: {len(missing)} rows")

    # Optional birth-date sidecar.
    if PFR_BIRTH_DATES.exists():
        bd = pd.read_csv(PFR_BIRTH_DATES)
        bd_map = dict(zip(bd["pfr_id"], bd["birth_date"]))
    else:
        bd_map = {}

    rows: list[dict] = []
    for pfr in missing:
        synth_player_id = f"pfr_{pfr}"
        player_rows = pre[pre["player_id"] == synth_player_id]
        if player_rows.empty:
            continue
        display_name = str(player_rows["player_display_name"].iloc[0])
        position = str(player_rows["position"].iloc[0])
        latest_team = str(player_rows.sort_values("season")["recent_team"].iloc[-1])
        rookie_season = int(player_rows["season"].min())
        last_season = int(player_rows["season"].max())
        first, last = _normalize_name(display_name)
        rows.append({
            "gsis_id": synth_player_id,  # use the pfr_X id as the synthetic gsis_id
            "display_name": display_name,
            "common_first_name": first,
            "first_name": first,
            "last_name": last,
            "short_name": "",
            "football_name": "",
            "suffix": "",
            "esb_id": "",
            "nfl_id": "",
            "pfr_id": pfr,
            "pff_id": "",
            "otc_id": "",
            "espn_id": "",
            "smart_id": "",
            "birth_date": bd_map.get(pfr, ""),
            "position_group": position,
            "position": position,
            "ngs_position_group": "",
            "ngs_position": "",
            "height": "",
            "weight": "",
            "headshot": "",
            "college_name": "",
            "college_conference": "",
            "jersey_number": "",
            "rookie_season": rookie_season,
            "last_season": last_season,
            "latest_team": latest_team,
            "status": "RET",
            "ngs_status": "",
            "ngs_status_short_description": "",
            "years_of_experience": last_season - rookie_season + 1,
            "pff_position": "",
            "pff_status": "",
            "draft_year": "",
            "draft_round": "",
            "draft_pick": "",
            "draft_team": "",
        })

    if not rows:
        print("Nothing to write — sidecar already current.")
        return 0

    out = pd.DataFrame(rows, columns=list(players.columns))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUTPUT_PATH, index=False, compression="gzip")
    print(f"Wrote {OUTPUT_PATH} ({len(out)} rows, {len(out.columns)} columns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
