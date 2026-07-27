#!/usr/bin/env bats
# test-present-gen.bats — presentation generation planning (IT-P1, IT-P3).
# The real publish path force-pushes and opens PRs via `gh`, so integration
# coverage here exercises the deterministic publish dry-run planner: branch
# naming, new-vs-existing PR detection, and idempotent (deterministic) output.

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  build_linear_dag pg
}

teardown() { cleanup_sandbox; }

seed_no_pr() {
  seed_manifest pg <<YAML
feature: pg
canonical: main
merge_mode: individual
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/pg/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
YAML
}

seed_with_pr() {
  seed_manifest pg <<YAML
feature: pg
canonical: main
merge_mode: individual
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/pg/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}, present: {branch: present/a1, pr: 42, source_tip_sha: $A1_SHA}}
YAML
}

@test "publish --dry-run names presentation branch and signals new PR (IT-P1)" {
  seed_no_pr
  run fleet_cli publish --feature pg --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DRY-RUN: Publish" ]]
  [[ "$output" =~ "Presentation branch: present/a1" ]]
  [[ "$output" =~ "New PR will be created" ]]
}

@test "publish --dry-run detects existing PR from manifest present block" {
  seed_with_pr
  run fleet_cli publish --feature pg --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Existing PR: #42" ]]
  [[ ! "$output" =~ "New PR will be created" ]]
}

@test "publish --dry-run is deterministic across repeated runs (IT-P3)" {
  seed_no_pr
  run fleet_cli publish --feature pg --node a1 --dry-run
  local first="$output"
  run fleet_cli publish --feature pg --node a1 --dry-run
  [ "$output" = "$first" ]
}

@test "publish --dry-run does not mutate git refs" {
  seed_no_pr
  local before; before="$(git -C "$REPO_ROOT" rev-parse ghost/pg/a1)"
  run fleet_cli publish --feature pg --node a1 --dry-run
  [ "$status" -eq 0 ]
  [ "$(git -C "$REPO_ROOT" rev-parse ghost/pg/a1)" = "$before" ]
  # No presentation branch materialized by a dry run.
  run git -C "$REPO_ROOT" rev-parse --verify present/a1
  [ "$status" -ne 0 ]
}
