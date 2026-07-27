#!/usr/bin/env bats
# test-wal-recovery.bats — WAL crash detection, replay, rollback (IT-W1..IT-W4).
# Exercises the real WAL append via `fleet reconcile` (intent+commit pair), the
# incomplete-entry detector (fleet_wal.py <feature>), and rollback ref-restore
# via wal_replay(action="rollback").

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  build_linear_dag wr
  seed_manifest wr <<YAML
feature: wr
canonical: main
merge_mode: individual
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/wr/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
YAML
}

teardown() { cleanup_sandbox; }

@test "reconcile writes a complete intent+commit WAL pair" {
  run fleet_cli reconcile --feature wr
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Reconciliation complete" ]]
  # Both intent and commit records present in the WAL.
  run cat "$(wal_file wr)"
  [[ "$output" =~ "\"status\": \"intent\"" ]]
  [[ "$output" =~ "\"status\": \"commit\"" ]]
}

@test "clean WAL reports no incomplete entries (IT-W2 negative)" {
  fleet_cli reconcile --feature wr
  run fleet_mod fleet_wal.py wr
  [ "$status" -eq 0 ]
  [[ "$output" =~ "No incomplete entries" ]]
}

@test "dangling intent (crash mid-op) is detected as incomplete (IT-W1/IT-W2)" {
  fleet_cli reconcile --feature wr
  # Simulate a crash: an intent with no matching commit record.
  printf '{"timestamp":"2026-07-27T00:00:00Z","op_type":"cascade","status":"intent","payload":{"feature":"wr","root":"a1"}}\n' >> "$(wal_file wr)"
  run fleet_mod fleet_wal.py wr
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Found 1 incomplete entries" ]]
  [[ "$output" =~ "cascade" ]]
}

@test "rollback restores pre-image refs via wal_replay (IT-W4)" {
  cd "$REPO_ROOT"
  # Move the ghost tip forward, then roll back to the recorded pre-image SHA.
  git checkout -q ghost/wr/a1
  echo extra > extra.txt && git add extra.txt && git commit -q -m "extra (to be rolled back)"
  local moved; moved="$(git rev-parse ghost/wr/a1)"
  [ "$moved" != "$A1_SHA" ]

  run "$FLEET_PY" - "$A1_SHA" <<'PY'
import sys, os
sys.path.insert(0, os.environ["FLEET_SCRIPTS"])
from fleet_wal import wal_replay
pre = sys.argv[1]
entry = {"op_type": "cascade",
         "payload": {"pre_image_refs": {"refs/heads/ghost/wr/a1": pre}}}
res = wal_replay("wr", entry, "rollback")
print(res["status"])
PY
  [ "$status" -eq 0 ]
  [[ "$output" =~ "rolled_back" ]]
  # Ghost ref restored to its pre-image SHA.
  [ "$(git -C "$REPO_ROOT" rev-parse ghost/wr/a1)" = "$A1_SHA" ]
}

@test "replay returns ready_for_replay indicator (IT-W3)" {
  run "$FLEET_PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["FLEET_SCRIPTS"])
from fleet_wal import wal_replay
entry = {"op_type": "cascade", "payload": {"feature": "wr", "root": "a1"}}
res = wal_replay("wr", entry, "replay")
print(res["status"], res["op_type"])
PY
  [ "$status" -eq 0 ]
  [[ "$output" =~ "ready_for_replay cascade" ]]
}
