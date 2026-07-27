#!/usr/bin/env bats
# test-nli-dispatch.bats — NLI intent dispatch via `fleet classify` CLI.
# Covers UT-N* dispatch behavior end-to-end through the real CLI + regression
# for the "always-PLAN" classifier bug (base_confidence was credited to every
# intent unconditionally; now credited only on a keyword hit).

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  seed_manifest nli <<'YAML'
feature: nli
canonical: main
merge_mode: individual
nodes:
  a1: {kind: feature, desc: auth service, base: null, ghost: ghost/nli/a1, range: {base_sha: null, tip_sha: null}, status: draft, assumes: {}}
  a2: {kind: feature, desc: billing api, base: a1, ghost: ghost/nli/a2, range: {base_sha: null, tip_sha: null}, status: draft, assumes: {}}
YAML
}

teardown() { cleanup_sandbox; }

@test "classify: merge utterance -> MERGE intent (not PLAN regression)" {
  run fleet_cli classify "merge the auth node" --feature nli
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Intent: MERGE" ]]
}

@test "classify: status utterance -> STATUS intent" {
  run fleet_cli classify "show me the status" --feature nli
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Intent: STATUS" ]]
}

@test "classify: plan utterance -> PLAN intent" {
  run fleet_cli classify "plan the feature" --feature nli
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Intent: PLAN" ]]
}

@test "classify: publish utterance -> PUBLISH intent" {
  run fleet_cli classify "publish auth for review" --feature nli
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Intent: PUBLISH" ]]
}

@test "classify: unknown utterance falls back to STATUS at low confidence" {
  run fleet_cli classify "zzz qqq wibble" --feature nli
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Intent: STATUS" ]]
  [[ "$output" =~ "50.0%" ]]
}

@test "classify: fuzzy node resolution matches ghost/desc" {
  # "auth" matches a1's description "auth service"
  run fleet_cli classify "merge auth" --feature nli
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Nodes: a1" ]]
}

@test "classify --json emits structured intent payload" {
  run fleet_cli classify "merge auth" --feature nli --json
  [ "$status" -eq 0 ]
  [[ "$output" =~ "\"intent\": \"MERGE\"" ]]
  [[ "$output" =~ "\"feature\": \"nli\"" ]]
  [[ "$output" =~ "\"confidence\":" ]]
}

@test "classify: nonexistent feature manifest -> error exit" {
  run fleet_cli classify "merge auth" --feature ghost_missing
  [ "$status" -ne 0 ]
  [[ "$output" =~ "failed" || "$output" =~ "Error" || "$output" =~ "not found" ]]
}
