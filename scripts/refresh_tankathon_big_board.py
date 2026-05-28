#!/usr/bin/env python3
"""v3.3 — Refresh Tankathon NFL Big Board (Phil 2026-05-28, 2027 fallback).

Phil's brief asked for PFF (https://www.pff.com/draft/big-board?season=2027)
as the 2027 source. PFF's data is gated behind login + paid subscription.
This script uses Tankathon's free, daily-refreshed big board as the
fallback so the 2027 column on the Prospects tab isn't empty.

Output:
    data/tankathon/big_board_<YEAR>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dynasty.sources.tankathon_big_board import fetch_and_parse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year", type=int, default=2027,
        help="Current draft year that Tankathon's main board reflects.",
    )
    parser.add_argument("--out-dir", default="data/tankathon")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prospects = fetch_and_parse(current_draft_year=args.year)
    skill = [p for p in prospects if p.position in ("QB", "RB", "WR", "TE")]
    by_year = {}
    for p in prospects:
        by_year.setdefault(str(p.draft_year), []).append(p.to_dict())
    out_path = out_dir / f"big_board_{args.year}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({
            "source": "tankathon",
            "current_year": args.year,
            "totals": {"all": len(prospects), "skill": len(skill)},
            "by_year": by_year,
            "prospects": [p.to_dict() for p in prospects],
        }, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    logging.info("wrote %s — %d prospects (%d skill)", out_path, len(prospects), len(skill))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
