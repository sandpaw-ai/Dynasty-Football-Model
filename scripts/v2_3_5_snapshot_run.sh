#!/usr/bin/env bash
# v2.3.5 before/after snapshot wrapper.
# Captures top-K comps for the validation targets BEFORE and AFTER the
# fix by stashing the engine-source edits, running the engine, then
# restoring them.
#
# Usage:
#   bash scripts/v2_3_5_snapshot_run.sh
# Output:
#   tests/snapshots/v2.3.5_comp_shifts.json
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="tests/snapshots"
OUT_FILE="$OUT_DIR/v2.3.5_comp_shifts.json"
TMP_BEFORE="$(mktemp -t before-XXXX.json)"
TMP_AFTER="$(mktemp -t after-XXXX.json)"
# trap is set below once STASH_DIR is created (restores working tree).

mkdir -p "$OUT_DIR"

# Engine files we'll temporarily revert to capture the BEFORE state.
ENGINE_FILES=(
  "src/dynasty/engine/fantasy_arc_similarity.py"
  "src/dynasty/engine/rookie_nfl_fp_arc.py"
  "src/dynasty/engine/similarity_v1.py"
)

# Stash any working-tree changes to the engine files so we can restore
# them exactly as they are right now (not the HEAD-committed state —
# we might be running this with uncommitted local edits).
STASH_DIR="$(mktemp -d -t v2_3_5_snapshot-XXXX)"
for f in "${ENGINE_FILES[@]}"; do
  cp "$f" "$STASH_DIR/$(echo "$f" | tr / _).working"
done
restore_working() {
  for f in "${ENGINE_FILES[@]}"; do
    cp "$STASH_DIR/$(echo "$f" | tr / _).working" "$f"
  done
  rm -rf "$STASH_DIR"
}
trap 'restore_working; rm -f "$TMP_BEFORE" "$TMP_AFTER"' EXIT

# Capture AFTER first (current state of the working tree, including any
# uncommitted edits).
echo "[v2.3.5 snapshot] running AFTER capture (working-tree state)..." >&2
python3 scripts/v2_3_5_snapshot_capture.py after > "$TMP_AFTER"

# Revert engine files to the pre-fix state. We use the parent of the
# v2.3.5 commit chain as the BEFORE baseline.
PRE_FIX_REF="upstream/main"
echo "[v2.3.5 snapshot] reverting engine files to ${PRE_FIX_REF} for BEFORE capture..." >&2
for f in "${ENGINE_FILES[@]}"; do
  git show "${PRE_FIX_REF}:${f}" > "$f.before.tmp" 2>/dev/null && mv "$f.before.tmp" "$f" || true
done

echo "[v2.3.5 snapshot] running BEFORE capture..." >&2
python3 scripts/v2_3_5_snapshot_capture.py before > "$TMP_BEFORE"

# Restore the working-tree engine files (the trap also handles this on
# unexpected exits).
echo "[v2.3.5 snapshot] restoring working-tree engine files..." >&2
restore_working
trap 'rm -f "$TMP_BEFORE" "$TMP_AFTER"' EXIT

# Merge before + after into one snapshot file.
python3 - "$TMP_BEFORE" "$TMP_AFTER" "$OUT_FILE" <<'PY'
import json, sys
before_p, after_p, out_p = sys.argv[1], sys.argv[2], sys.argv[3]
with open(before_p) as f: before = json.load(f)
with open(after_p)  as f: after  = json.load(f)
merged = {
    "version": "v2.3.5",
    "note": (
        "Top-25 comps before/after the v2.3.5 age-aware similarity + "
        "bust-inclusive rookie corpus fix. before = upstream/main HEAD, "
        "after = ada/v2.3.5-age-comp-fix branch."
    ),
    "targets": {},
}
names = set(before.get("targets", {})) | set(after.get("targets", {}))
for name in sorted(names):
    b = before.get("targets", {}).get(name, {})
    a = after.get("targets",  {}).get(name, {})
    merged["targets"][name] = {
        "note": a.get("note") or b.get("note"),
        "pid":  a.get("pid")  or b.get("pid"),
        "before_top25": b.get("comps", []),
        "after_top25":  a.get("comps", []),
    }
with open(out_p, "w") as f:
    json.dump(merged, f, indent=2, sort_keys=True)
print(f"wrote {out_p}", file=sys.stderr)
PY

echo "[v2.3.5 snapshot] wrote $OUT_FILE" >&2
