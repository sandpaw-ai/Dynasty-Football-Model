"""v1.2.0 tests — SUPERSEDED BY v2.0.

v1.2.0 introduced fantasy-point-weighted z-score vectors + style cohorts.
v2.0 replaces ALL of that with a fantasy-point-arc similarity engine
that operates directly in fp/g space (no z-scoring, no style cohorts).

The v1.2 test contract no longer applies:
  * ``EraZNorm`` / ``FANTASY_FEATURES`` / ``player_career_vector`` are
    removed from similarity_v1 in v2.0.
  * ``style_cohort.py`` is deleted in v2.0 (the brief explicitly says
    "fantasy arc methodology allows Allen → Brady if their fp curves
    match" and "style cohort can go").

The behavioural invariants that v1.2 was trying to enforce are now
re-pinned (in stronger form) by ``tests/test_v2_fantasy_arc.py``:
  - Allen / Hurts / Lamar / Daniels in elite-QB cluster
  - pocket QBs no longer top 5
  - aging Rodgers deep
  - elite-fp comp pool for top QBs (regardless of style)
  - Nacua / Bijan / Bowers retired-position comps

This file is kept as a placeholder so legacy references resolve. It
emits a module-level skip so pytest doesn't try to import the deleted
symbols.
"""
import pytest

pytest.skip(
    "v1.2 module-level skip: methodology entirely replaced by v2.0 "
    "fantasy-point-arc engine. See tests/test_v2_fantasy_arc.py.",
    allow_module_level=True,
)
