#!/usr/bin/env python3
"""Convenience CLI for v3.0 PR 3 — emit the prospect corpus to disk.

Usage::

    PYTHONPATH=src python scripts/build_prospect_engine.py \
        [--seasons data/historical_ncaa_football] \
        [--sos data/sos] \
        [--bridge data/bridge/ncaa_to_nfl.json] \
        [--out data/engine_v3/prospect_corpus.json.gz]

The output is a gzipped JSON file with the shape::

    {
        "version": "v3.0-pr3",
        "n_prospects": 14431,
        "bridge_coverage": {"n": ..., "matched": ..., "rate": ...},
        "prospects": [ { ProspectVector dict }, ... ]
    }

Loaded by future PRs to avoid re-walking 26 season JSONs each time the
engine runs.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
from dataclasses import asdict
from pathlib import Path

from dynasty.engine.prospect_similarity import (
    DEFAULT_BRIDGE_FILE,
    DEFAULT_SEASONS_ROOT,
    DEFAULT_SOS_ROOT,
    NameCollisionResolver,
    build_prospect_corpus,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seasons", type=Path, default=DEFAULT_SEASONS_ROOT)
    parser.add_argument("--sos", type=Path, default=DEFAULT_SOS_ROOT)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE_FILE)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/engine_v3/prospect_corpus.json.gz"),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    log = logging.getLogger("build_prospect_engine")
    log.info("Building prospect corpus from %s", args.seasons)
    corpus = build_prospect_corpus(seasons_root=args.seasons, sos_root=args.sos)
    log.info("Built %d prospects", len(corpus))

    log.info("Loading bridge from %s", args.bridge)
    resolver = NameCollisionResolver.from_file(args.bridge)
    coverage = resolver.coverage(corpus)
    log.info(
        "Bridge coverage: %.1f%% (%d/%d)",
        100.0 * coverage["rate"],
        int(coverage["matched"]),
        int(coverage["n"]),
    )

    out = {
        "version": "v3.0-pr3",
        "n_prospects": len(corpus),
        "bridge_coverage": coverage,
        "prospects": [asdict(pv) for pv in corpus],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, indent=2).encode("utf-8")
    with gzip.open(args.out, "wb") as f:
        f.write(payload)
    log.info("Wrote %s (%d bytes)", args.out, args.out.stat().st_size)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual CLI
    raise SystemExit(main())
