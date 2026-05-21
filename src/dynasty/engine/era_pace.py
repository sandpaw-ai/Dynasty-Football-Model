"""Era-pace multipliers for projecting retired-player careers into the modern NFL.

The corpus we have on disk (nflverse player_stats_season.csv.gz) starts in 1999,
so the original brief's Era 1 = 1980-1994 / Era 2 = 1995-2004 buckets are
adjusted here to fit available data. The conceptual structure is unchanged:
four era buckets, monotonically scaling for passing volume and modern-QB
rushing usage, roughly flat for RB volume.

Computed multipliers (per-position, per-stat) are derived empirically from the
corpus at engine init by ``similarity_v1`` and cached. The defaults below are
the *fallback* table the brief documents — used when corpus-derived ratios are
unavailable or to sanity-check tests. The corpus-derived values land in the
same neighbourhood, and tests assert ranges, not exact values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

# Era buckets. NOTE: corpus starts 1999, so Era 1 here covers 1999-2004
# instead of the originally-specified 1980-1994. The behaviour (lower-pace
# passing, lower-pace QB rushing) holds either way for that bucket; this is
# documented as a Known Limitation in CHANGELOG.
ERA_BOUNDS: Tuple[Tuple[int, int, int], ...] = (
    (1, 1980, 2004),   # Pre-modern (data available from 1999 onward)
    (2, 2005, 2014),   # Mid-modern
    (3, 2015, 2019),   # Post-pass-inflation, pre-Mahomes-era saturation
    (4, 2020, 2099),   # Current
)


def era_for_season(season: int) -> int:
    """Return the era bucket (1-4) for a given NFL season."""
    for era, lo, hi in ERA_BOUNDS:
        if lo <= season <= hi:
            return era
    # Anything before 1980 lumps into era 1.
    if season < 1980:
        return 1
    return 4


# Fallback / documented multipliers — used when corpus-derived values are
# missing for a (position, stat, era_from) cell. Multipliers are "era_from→4".
# I.e. multiply a retired player's raw per-season stat by FALLBACK_MULTIPLIERS
# [pos][stat][era_from] to project what it would have looked like in era 4.
#
# These are documented in V1-METHODOLOGY.md and tested in test_engine_v1.py.
FALLBACK_MULTIPLIERS: Dict[str, Dict[str, Dict[int, float]]] = {
    "QB": {
        "passing_yards":   {1: 1.25, 2: 1.18, 3: 1.08, 4: 1.00},
        "passing_tds":     {1: 1.30, 2: 1.20, 3: 1.10, 4: 1.00},
        "rushing_yards":   {1: 1.40, 2: 1.30, 3: 1.15, 4: 1.00},
        "rushing_tds":     {1: 1.35, 2: 1.25, 3: 1.10, 4: 1.00},
        "interceptions":   {1: 0.85, 2: 0.92, 3: 0.97, 4: 1.00},
    },
    "RB": {
        "rushing_yards":   {1: 1.00, 2: 1.02, 3: 1.03, 4: 1.00},
        "rushing_tds":     {1: 1.00, 2: 1.02, 3: 1.03, 4: 1.00},
        "receptions":      {1: 1.20, 2: 1.15, 3: 1.10, 4: 1.00},
        "receiving_yards": {1: 1.20, 2: 1.15, 3: 1.10, 4: 1.00},
        "receiving_tds":   {1: 1.20, 2: 1.15, 3: 1.10, 4: 1.00},
    },
    "WR": {
        "receptions":      {1: 1.18, 2: 1.13, 3: 1.06, 4: 1.00},
        "receiving_yards": {1: 1.20, 2: 1.15, 3: 1.07, 4: 1.00},
        "receiving_tds":   {1: 1.18, 2: 1.13, 3: 1.06, 4: 1.00},
        "rushing_yards":   {1: 1.00, 2: 1.00, 3: 1.00, 4: 1.00},
    },
    "TE": {
        "receptions":      {1: 1.40, 2: 1.25, 3: 1.10, 4: 1.00},
        "receiving_yards": {1: 1.40, 2: 1.25, 3: 1.10, 4: 1.00},
        "receiving_tds":   {1: 1.35, 2: 1.22, 3: 1.08, 4: 1.00},
    },
}


@dataclass
class EraPaceTable:
    """Carries either corpus-derived or fallback multipliers."""

    multipliers: Dict[str, Dict[str, Dict[int, float]]]
    source: str  # "corpus" | "fallback" | "hybrid"

    def get(self, position: str, stat: str, era_from: int) -> float:
        try:
            return float(self.multipliers[position][stat][era_from])
        except (KeyError, TypeError):
            try:
                return float(FALLBACK_MULTIPLIERS[position][stat][era_from])
            except (KeyError, TypeError):
                return 1.0


def fallback_table() -> EraPaceTable:
    return EraPaceTable(multipliers=FALLBACK_MULTIPLIERS, source="fallback")
