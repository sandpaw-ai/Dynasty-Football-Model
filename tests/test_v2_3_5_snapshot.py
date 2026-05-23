"""v2.3.5 snapshot validation: hard-assert Phil's bug-report constraint.

Reads the captured snapshot at tests/snapshots/v2.3.5_comp_shifts.json
and verifies Steve Smith (Sr.) and Santana Moss are no longer in
Johnny Wilson's top-10 comps. Also verifies the sanity-check targets
(Mitchell, Stroud, Jefferson, Allen) are unchanged.

If the snapshot file is missing, the tests are skipped \u2014 the snapshot
is captured by scripts/v2_3_5_snapshot_run.sh which needs a working
nflverse corpus and may be unavailable in some CI contexts.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest


SNAPSHOT_PATH = pathlib.Path(__file__).parent / "snapshots" / "v2.3.5_comp_shifts.json"


@pytest.fixture(scope="module")
def snapshot():
    if not SNAPSHOT_PATH.exists():
        pytest.skip(
            f"v2.3.5 snapshot not found at {SNAPSHOT_PATH}. "
            "Run: bash scripts/v2_3_5_snapshot_run.sh"
        )
    return json.loads(SNAPSHOT_PATH.read_text())


def _names(target_entry: dict, key: str, limit: int) -> list[str]:
    return [c.get("name") for c in target_entry.get(key, [])[:limit]]


# ---------------------------------------------------------------------------
# Headline: Phil's hard assertion
# ---------------------------------------------------------------------------

def test_wilson_no_steve_smith_in_top10(snapshot):
    """v2.3.5 hard assertion: Steve Smith (Sr.) must not appear in
    Johnny Wilson's post-fix top-10 comp list."""
    wilson = snapshot["targets"]["Johnny Wilson"]
    top10 = _names(wilson, "after_top25", 10)
    assert "Steve Smith" not in top10, (
        f"v2.3.5 fix incomplete: Steve Smith still in Wilson's top-10: {top10}"
    )


def test_wilson_no_santana_moss_in_top10(snapshot):
    """v2.3.5 hard assertion: Santana Moss must not appear in Johnny
    Wilson's post-fix top-10 comp list."""
    wilson = snapshot["targets"]["Johnny Wilson"]
    top10 = _names(wilson, "after_top25", 10)
    assert "Santana Moss" not in top10, (
        f"v2.3.5 fix incomplete: Santana Moss still in Wilson's top-10: {top10}"
    )


def test_wilson_top10_actually_shifted(snapshot):
    """Sanity: the BEFORE list DID contain Smith/Moss \u2014 confirming the
    snapshot is comparing the right baselines."""
    wilson = snapshot["targets"]["Johnny Wilson"]
    before10 = _names(wilson, "before_top25", 10)
    assert "Steve Smith" in before10, (
        f"Snapshot baseline wrong \u2014 Steve Smith should be in pre-fix "
        f"top-10 (was rank #1 in the bug report). before10={before10}"
    )


# ---------------------------------------------------------------------------
# Sanity checks: unchanged or only-tier-internal shuffles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target_name", [
    "Adonai Mitchell",       # age-matched control
    "C.J. Stroud",           # elite QB rookie, age-appropriate
    "Justin Jefferson",      # cumulative-engine WR vet
    "Josh Allen",            # cumulative-engine QB vet
])
def test_sanity_top10_unchanged(snapshot, target_name):
    """v2.3.5 invariant: targets that are age-appropriate or veterans
    in the cumulative engine should have UNCHANGED top-10 comp lists.
    The age fix should affect age-mismatched rookies specifically, not
    flatten everyone else's results."""
    target = snapshot["targets"][target_name]
    before10 = _names(target, "before_top25", 10)
    after10 = _names(target, "after_top25", 10)
    assert after10 == before10, (
        f"{target_name} top-10 changed unexpectedly under v2.3.5.\n"
        f"  BEFORE: {before10}\n"
        f"  AFTER : {after10}"
    )
