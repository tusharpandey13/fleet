#!/usr/bin/env bats
# test-e2e.bats — end-to-end fleet orchestration (IT-E1..IT-E7).
# Walks the full lifecycle over the real CLI against a seeded manifest + real
# git DAG: status -> reconcile -> cascade -> merge/publish previews. The `init`
# command is a stub, so the manifest is seeded directly (sanctioned pattern).

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  build_linear_dag e2e
  seed_manifest e2e <<YAML
feature: e2e
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/e2e/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/e2e/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML
}

teardown() { cleanup_sandbox; }

@test "IT-E1: init runs (stub) without error" {
  run fleet_cli init --feature e2e
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Initialization complete" ]]
}

@test "IT-E7: status renders the DAG with all nodes" {
  run fleet_cli status --feature e2e
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Feature: e2e" ]]
  [[ "$output" =~ "a1" ]]
  [[ "$output" =~ "a2" ]]
}

@test "status --json emits structured node data" {
  run fleet_cli status --feature e2e --json
  [ "$status" -eq 0 ]
  [[ "$output" =~ "\"feature\": \"e2e\"" ]]
  [[ "$output" =~ "a1" ]]
  [[ "$output" =~ "a2" ]]
}

@test "reconcile recomputes live tip SHAs into the manifest" {
  run fleet_cli reconcile --feature e2e
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Reconciliation complete" ]]
  # Live tip SHAs persisted back to the manifest.
  run cat "$(manifest_file e2e)"
  [[ "$output" =~ "$A1_SHA" ]]
  [[ "$output" =~ "$A2_SHA" ]]
}

@test "IT-E3: full flow status -> reconcile -> cascade preserves the stack" {
  # Status (observe)
  run fleet_cli status --feature e2e
  [ "$status" -eq 0 ]
  # Reconcile (sync derivables)
  run fleet_cli reconcile --feature e2e
  [ "$status" -eq 0 ]
  # Cascade (real rebase, confirmed via "yes")
  cd "$REPO_ROOT"
  git checkout -q ghost/e2e/a1
  run bash -c 'echo yes | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" cascade --feature e2e --node a1'
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Cascade complete" ]]
  # Stack preserved: a2 sits atop a1.
  run git -C "$REPO_ROOT" log ghost/e2e/a2 --oneline
  [[ "$output" =~ "a2 work" ]]
  [[ "$output" =~ "a1 work" ]]
}

@test "IT-E4/E5: publish + merge previews complete for the root node" {
  run fleet_cli publish --feature e2e --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Presentation branch: present/a1" ]]

  run fleet_cli merge --feature e2e --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Children to re-parent: a2" ]]
}
