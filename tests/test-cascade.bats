#!/usr/bin/env bats
# test-cascade.bats — cascade rebase behavior (IT-E3, IT-DAG-1).
# Exercises the deterministic dry-run planner against a seeded manifest and a
# real git DAG, plus a real (mutating) --update-refs rebase confirmed via `yes`.

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
}

teardown() { cleanup_sandbox; }

# Seed a linear-DAG manifest: a1 (root, base main) -> a2 (base a1).
seed_linear() {
  build_linear_dag cascade
  seed_manifest cascade <<YAML
feature: cascade
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/cascade/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/cascade/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML
}

@test "cascade --dry-run lists descendants without mutating git" {
  seed_linear
  local before; before="$(git -C "$REPO_ROOT" rev-parse ghost/cascade/a2)"
  run fleet_cli cascade --feature cascade --node a1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DRY-RUN" ]]
  [[ "$output" =~ "Descendants to rebase: a2" ]]
  # git unchanged
  [ "$(git -C "$REPO_ROOT" rev-parse ghost/cascade/a2)" = "$before" ]
}

@test "cascade --dry-run with leaf node reports no descendants" {
  seed_linear
  run fleet_cli cascade --feature cascade --node a2 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DRY-RUN" ]]
  # a2 is a leaf: descendant list is empty
  [[ ! "$output" =~ "Descendants to rebase: a2" ]]
}

@test "cascade real rebase --update-refs replays descendants preserving stack (IT-E3)" {
  seed_linear
  cd "$REPO_ROOT"
  git checkout -q ghost/cascade/a1
  local a1_before; a1_before="$(git rev-parse ghost/cascade/a1)"
  # Real cascade prompts "Cascade a1 and rebase N descendants? (yes/no)"; feed "yes".
  run bash -c 'echo yes | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" cascade --feature cascade --node a1'
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Cascade complete" ]]
  # a2 retains the full stack: its own commit on top of a1's commit.
  run git -C "$REPO_ROOT" log ghost/cascade/a2 --oneline
  [[ "$output" =~ "a2 work" ]]
  [[ "$output" =~ "a1 work" ]]
  # Idempotent: a1 ref unchanged when nothing diverged.
  [ "$(git -C "$REPO_ROOT" rev-parse ghost/cascade/a1)" = "$a1_before" ]
}

@test "cascade tie-break: diamond DAG includes all parallel descendants (IT-DAG-1)" {
  build_diamond_dag cascade
  seed_manifest cascade <<YAML
feature: cascade
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/cascade/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: left, base: a1, ghost: ghost/cascade/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
  a3: {kind: feature, desc: right, base: a1, ghost: ghost/cascade/a3, range: {base_sha: $A1_SHA, tip_sha: $A3_SHA}, status: draft, assumes: {}}
YAML
  run fleet_cli cascade --feature cascade --node a1 --dry-run
  [ "$status" -eq 0 ]
  # Both parallel descendants at same depth must appear in the plan.
  [[ "$output" =~ "a2" ]]
  [[ "$output" =~ "a3" ]]
}
