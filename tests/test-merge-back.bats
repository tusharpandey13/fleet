#!/usr/bin/env bats
# test-merge-back.bats — merge-back (squash cleanup via --onto) planning (IT-E5).
# The real merge path force-pushes and drives `gh` PR merges, so integration
# coverage here targets the deterministic dry-run planner: it derives the
# re-parent set and new base purely from the manifest DAG.

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  build_linear_dag mb
  seed_manifest mb <<YAML
feature: mb
canonical: main
merge_mode: individual
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/mb/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/mb/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML
}

teardown() { cleanup_sandbox; }

@test "merge --dry-run reports re-parent set and new base for root node (IT-E5)" {
  run fleet_cli merge --feature mb --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DRY-RUN: Merge-back" ]]
  [[ "$output" =~ "Node: a1" ]]
  [[ "$output" =~ "Children to re-parent: a2" ]]
  [[ "$output" =~ "New base: main" ]]
}

@test "merge --dry-run leaf node has no children to re-parent" {
  run fleet_cli merge --feature mb --node a2 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Node: a2" ]]
  [[ "$output" =~ "Children to re-parent: (none)" ]]
}

@test "merge --dry-run batch mode still previews without mutating git" {
  local a1_before a2_before
  a1_before="$(git -C "$REPO_ROOT" rev-parse ghost/mb/a1)"
  a2_before="$(git -C "$REPO_ROOT" rev-parse ghost/mb/a2)"
  run fleet_cli merge --feature mb --node a1 --mode batch --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DRY-RUN: Merge-back" ]]
  # No ref changed by a dry run.
  [ "$(git -C "$REPO_ROOT" rev-parse ghost/mb/a1)" = "$a1_before" ]
  [ "$(git -C "$REPO_ROOT" rev-parse ghost/mb/a2)" = "$a2_before" ]
}

@test "merge --dry-run counts squashable commits" {
  run fleet_cli merge --feature mb --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Commits to drop:" ]]
}

@test "merge unknown feature manifest -> error exit" {
  run fleet_cli merge --feature nope --node a1 --dry-run
  [ "$status" -ne 0 ]
  [[ "$output" =~ "not found" || "$output" =~ "Error" || "$output" =~ "failed" ]]
}
