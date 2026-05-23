#!/usr/bin/env python3
"""Birth-date backfill for pre-1999 PFR players.

Reads the unique PFR ids from
``data/nflverse/player_stats_season_pre1999.csv.gz`` (which must be
built first via ``build_pre1999_corpus.py``), fetches each player's PFR
bio page through Wayback (cache-backed), parses the ``data-birth``
attribute, and writes:

    data/pfr_birth_dates.csv  →  pfr_id, name, birth_date (YYYY-MM-DD)

This is the v2.4 replacement for the ``rookie_season + 22`` age
fallback documented as a Known Limitation in the current model — for
pre-1999 entries the actual DoB is available and worth ~15 min of
rate-limited scraping.
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from dynasty.sources.pro_football_reference_seasonal import (  # noqa: E402
    fetch_player_bio,
)

log = logging.getLogger("build_pre1999_birth_dates")

CORPUS_PATH = _REPO_ROOT / "data" / "nflverse" / "player_stats_season_pre1999.csv.gz"
OUTPUT_PATH = _REPO_ROOT / "data" / "pfr_birth_dates.csv"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not CORPUS_PATH.exists():
        raise SystemExit(
            f"corpus not found: {CORPUS_PATH}\n"
            "run scripts/build_pre1999_corpus.py first."
        )

    df = pd.read_csv(CORPUS_PATH, low_memory=False)
    # player_id is "pfr_<ID>" — strip the prefix.
    ids = (
        df.loc[df["player_id"].str.startswith("pfr_"), "player_id"]
        .str.removeprefix("pfr_")
        .unique()
        .tolist()
    )
    ids.sort()
    log.info("backfilling birth dates for %d unique PFR ids", len(ids))

    # Resume support: re-read any existing output rows and skip ids
    # that already have a birth_date.
    done: dict[str, dict] = {}
    if OUTPUT_PATH.exists():
        existing = pd.read_csv(OUTPUT_PATH)
        for _, r in existing.iterrows():
            if pd.notna(r.get("birth_date")):
                done[r["pfr_id"]] = {"name": r["name"], "birth_date": r["birth_date"]}
        log.info("  resume: %d already have birth dates", len(done))

    todo = [pid for pid in ids if pid not in done]
    log.info("  %d still to fetch", len(todo))

    results: list[dict] = list(done.values())
    # Write the header once.
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not OUTPUT_PATH.exists()
    with OUTPUT_PATH.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["pfr_id", "name", "birth_date"])
        if write_header:
            writer.writeheader()

        for i, pid in enumerate(todo, 1):
            try:
                bio = fetch_player_bio(pid)
            except Exception as exc:  # noqa: BLE001
                log.warning("bio fetch failed for %s: %s", pid, exc)
                continue
            row = {
                "pfr_id": pid,
                "name": bio.get("name") or "",
                "birth_date": bio.get("birth_date") or "",
            }
            writer.writerow(row)
            fh.flush()
            results.append(row)
            if i % 25 == 0:
                log.info("  progress: %d/%d", i, len(todo))

    # Final summary.
    df_out = pd.read_csv(OUTPUT_PATH)
    n_with = df_out["birth_date"].notna().sum()
    log.info("wrote %d rows → %s (%d with birth_date)", len(df_out), OUTPUT_PATH, n_with)


if __name__ == "__main__":
    main()
