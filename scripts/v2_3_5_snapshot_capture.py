"""v2.3.5 snapshot capture.

Runs ``run_engine()`` end-to-end on the current nflverse corpus and
emits a JSON snapshot of the top-25 comps for the v2.3.5 validation
targets (Phil's bug-report targets + age-matched control + sanity
checks).

Usage (called twice from a wrapper that toggles the git state to
capture BEFORE and AFTER):

    python scripts/v2_3_5_snapshot_capture.py before > /tmp/before.json
    # ... toggle git state ...
    python scripts/v2_3_5_snapshot_capture.py after > /tmp/after.json

Then the wrapper merges the two into tests/snapshots/v2.3.5_comp_shifts.json.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# Phil's bug-report targets + age-matched control + sanity checks.
# Names match the nflverse ``display_name`` column. The keys we'll lookup
# the engine's comp output by are player_id, so we resolve names \u2192 pids
# at runtime.
TARGETS_BY_NAME = [
    ("Johnny Wilson", "WR rookie age ~24 \u2014 the bug Phil reported"),
    ("Adonai Mitchell", "WR rookie age ~22 \u2014 age-matched control"),
    ("Bo Nix", "QB rookie age ~25"),
    ("Brock Purdy", "QB age ~22 rookie / age ~23 first-start \u2014 should still shift slightly"),
    ("C.J. Stroud", "QB sanity check \u2014 elite, age-appropriate"),
    ("Justin Jefferson", "WR vet sanity check (cumulative engine)"),
    ("Josh Allen", "QB vet sanity check (cumulative engine)"),
]


def main(label: str) -> None:
    from dynasty.engine.similarity_v1 import run_engine

    result = run_engine(persist=False)
    # result.comps is Dict[pid, List[comp_dict_with_name]]
    careers = result.careers

    # Resolve target names to pids.
    name_to_pid = {c.name: pid for pid, c in careers.items()}

    snapshot = {"label": label, "targets": {}}
    for name, note in TARGETS_BY_NAME:
        pid = name_to_pid.get(name)
        if pid is None:
            # Try fuzzy: case-insensitive and tolerant of suffixes.
            lname = name.lower()
            for c_name, c_pid in name_to_pid.items():
                if c_name.lower() == lname:
                    pid = c_pid
                    break
        if pid is None:
            snapshot["targets"][name] = {
                "pid": None, "note": note, "comps": [],
                "error": "pid not found",
            }
            continue
        comps_for_target = result.comps.get(pid, [])[:25]
        snapshot["targets"][name] = {
            "pid": pid,
            "note": note,
            "comps": [
                {
                    "name": c.get("name"),
                    "similarity": round(c.get("similarity", 0.0), 4),
                    "rookie_season": c.get("rookie_season"),
                }
                for c in comps_for_target
            ],
        }
    print(json.dumps(snapshot, indent=2, sort_keys=True))


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "current"
    main(label)
