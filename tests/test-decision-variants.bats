#!/usr/bin/env bats
# test-decision-variants.bats — decision node resolution (EXPERIMENTAL, IT-D*).
# Decision resolution is gated behind manifest `experimental: true` (FLEET_099).
# There is no `fleet` CLI subcommand for it, so these tests invoke the module
# entry point (fleet_decision.py <feature> <node> <option>) directly.

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  # Build a repo with two option branches for decision b1.
  cd "$REPO_ROOT"
  git init -q -b main
  git config user.email test@fleet.local
  git config user.name "Fleet Test"
  echo r0 > r0.txt && git add r0.txt && git commit -q -m root0
  echo base > base.txt && git add base.txt && git commit -q -m init
  git checkout -q -b ghost/dv/b1-optA
  echo A > optA.txt && git add optA.txt && git commit -q -m optA
  git checkout -q -b ghost/dv/b1-optB main
  echo B > optB.txt && git add optB.txt && git commit -q -m optB
  git checkout -q main
}

teardown() { cleanup_sandbox; }

# Seed a decision-node manifest. Arg 1: experimental flag (true|false).
seed_decision() {
  local experimental="$1"
  seed_manifest dv <<YAML
feature: dv
canonical: main
merge_mode: individual
experimental: $experimental
nodes:
  b1:
    kind: decision
    desc: framework choice
    base: null
    ghost: null
    range: {base_sha: null, tip_sha: null}
    status: open
    assumes: {}
    options:
      optA: {ghost: ghost/dv/b1-optA}
      optB: {ghost: ghost/dv/b1-optB}
YAML
}

@test "decision blocked when experimental gate disabled (FLEET_099)" {
  seed_decision false
  run fleet_mod fleet_decision.py dv b1 optA
  [ "$status" -ne 0 ]
  [[ "$output" =~ "FLEET_099" ]]
  [[ "$output" =~ "experimental" ]]
}

@test "decision resolves and promotes chosen option when enabled (IT-D1)" {
  seed_decision true
  run fleet_mod fleet_decision.py dv b1 optA
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Decision resolution complete" ]]
  [[ "$output" =~ "\"promoted\": \"optA\"" ]]
  [[ "$output" =~ "\"status\": \"resolved\"" ]]
}

@test "decision archives the losing option branch (IT-D1)" {
  seed_decision true
  run fleet_mod fleet_decision.py dv b1 optA
  [ "$status" -eq 0 ]
  [[ "$output" =~ "\"archived\"" ]]
  [[ "$output" =~ "optB" ]]
  # Losing branch moved to archive/ namespace (never deleted).
  run git -C "$REPO_ROOT" rev-parse --verify archive/ghost/dv/b1-optB
  [ "$status" -eq 0 ]
}

@test "decision persists promotion to manifest (IT-D1)" {
  seed_decision true
  fleet_mod fleet_decision.py dv b1 optA
  run cat "$(manifest_file dv)"
  [[ "$output" =~ "promoted: optA" ]]
  [[ "$output" =~ "status: resolved" ]]
}

@test "decision rejects unknown option" {
  seed_decision true
  run fleet_mod fleet_decision.py dv b1 optZ
  [ "$status" -ne 0 ]
  [[ "$output" =~ "not found" || "$output" =~ "Available" ]]
}
