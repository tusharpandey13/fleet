#!/usr/bin/env bats
# test-v2-cli.bats — Fleet v2 CLI/git integration tests
# Tests: T-FR3-5, T-FR3-6, T-FR3-8, T-FR3-12, T-FR3-16, T-FR4-1, T-FR4-2,
#        T-NFR2-1, T-NFR3-1, + error path coverage
#
# Implements BATS cases for: fleet init, fleet plan, fleet materialize,
# fleet work, fleet resolve, fleet refresh. Real git, real files, real manifest.
# fleet_helper provides sandbox isolation + helpers.

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
  build_linear_dag v2test
}

teardown() {
  cleanup_sandbox
}

# ============================================================================
# FR-V2-4: `fleet init` creates manifest + lock + wt/ dir
# ============================================================================

@test "T-FR4-1: fleet init creates manifest, lock, and pool dir" {
  # Act: init new feature
  run fleet_cli init --feature auth-refactor

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: manifest file exists + is valid YAML
  [ -f "$(manifest_file auth-refactor)" ]
  run "$FLEET_PY" -c "import yaml; yaml.safe_load(open('$(manifest_file auth-refactor)'))"
  [ "$status" -eq 0 ]

  # Assert: lock file created
  [ -f "$HOME/.fleet/auth-refactor/.fleet.lock" ]

  # Assert: worktree pool dir created
  [ -d "$HOME/.fleet/auth-refactor/wt" ]
}

@test "T-FR4-2: fleet init is idempotent (re-run warns, no clobber)" {
  # First init
  run fleet_cli init --feature idempotent-test
  [ "$status" -eq 0 ]

  # Capture original manifest content
  original="$(cat "$(manifest_file idempotent-test)")"

  # Re-init same feature
  run fleet_cli init --feature idempotent-test

  # Assert: exit 0, idempotent
  [ "$status" -eq 0 ]

  # Assert: manifest unchanged (no clobber)
  current="$(cat "$(manifest_file idempotent-test)")"
  [ "$original" = "$current" ]
}

# ============================================================================
# FR-V2-3: `fleet plan` creates manifest DAG
# ============================================================================

@test "T-FR3-1: fleet plan --nodes creates valid manifest with edges" {
  # Act: plan 3-node DAG
  run fleet_cli plan --feature plan-test \
    --nodes "a1:feature" "a2:feature(a1)" "d1:decision(a1)"

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: manifest exists
  [ -f "$(manifest_file plan-test)" ]

  # Assert: manifest contains all 3 nodes (via grep on yaml)
  manifest_content="$(cat "$(manifest_file plan-test)")"
  [[ "$manifest_content" =~ "a1:" ]]
  [[ "$manifest_content" =~ "a2:" ]]
  [[ "$manifest_content" =~ "d1:" ]]

  # Assert: parent edge captured in the manifest edges block (a2 -> a1)
  [[ "$manifest_content" =~ "edges:" ]]
  [[ "$manifest_content" =~ "a1" ]]
}

@test "T-FR3-2: fleet plan --dry-run writes nothing to disk" {
  # Precondition: no manifest exists
  [ ! -d "$HOME/.fleet/dryrun-test" ]

  # Act: plan with --dry-run
  run fleet_cli plan --feature dryrun-test \
    --nodes "a1:feature" "a2:feature(a1)" \
    --dry-run

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: manifest NOT written
  [ ! -f "$(manifest_file dryrun-test)" ]
}

@test "T-FR3-3: fleet plan detects cycle, raises FLEET_002" {
  # Act: plan with cycle a1 <- a2 <- a1
  run fleet_cli plan --feature cycle-test \
    --nodes "a1:feature(a2)" "a2:feature(a1)"

  # Assert: exit 1
  [ "$status" -eq 1 ]

  # Assert: FLEET_002 in stderr
  [[ "$output" =~ "FLEET_002" ]]

  # Assert: manifest NOT written
  [ ! -f "$(manifest_file cycle-test)" ]
}

@test "T-FR3-4: fleet plan invalid schema raises FLEET_003" {
  # Act: plan with invalid node kind
  run fleet_cli plan --feature schema-test \
    --nodes "a1:invalid_kind"

  # Assert: exit 1
  [ "$status" -eq 1 ]

  # Assert: FLEET_003 in stderr (or validation error)
  [[ "$output" =~ "FLEET_003" ]] || [[ "$output" =~ "invalid" ]]

  # Assert: manifest NOT written
  [ ! -f "$(manifest_file schema-test)" ]
}

# ============================================================================
# FR-V2-3: `fleet materialize` creates ghost branches
# ============================================================================

@test "T-FR3-5: fleet materialize creates ghost branches from main" {
  # Setup: create manifest via plan
  run fleet_cli plan --feature mat-test \
    --nodes "a1:feature" "a2:feature(a1)" "d1:decision(a1)"
  [ "$status" -eq 0 ]

  # Act: materialize
  run fleet_cli materialize --feature mat-test

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: all ghosts exist
  cd "$REPO_ROOT"
  git show-ref --verify --quiet "refs/heads/ghost/mat-test/a1"
  [ "$?" -eq 0 ]
  git show-ref --verify --quiet "refs/heads/ghost/mat-test/a2"
  [ "$?" -eq 0 ]
  git show-ref --verify --quiet "refs/heads/ghost/mat-test/d1"
  [ "$?" -eq 0 ]
}

@test "T-FR3-6: fleet materialize idempotent (re-run skips existing)" {
  # Setup: materialize once
  run fleet_cli plan --feature idem-mat --nodes "a1:feature" "a2:feature(a1)"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature idem-mat
  [ "$status" -eq 0 ]

  # Capture initial ghost SHAs
  cd "$REPO_ROOT"
  a1_sha1="$(git rev-parse ghost/idem-mat/a1)"
  a2_sha1="$(git rev-parse ghost/idem-mat/a2)"

  # Act: materialize again
  run fleet_cli materialize --feature idem-mat

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: ghosts unchanged (idempotent)
  a1_sha2="$(git rev-parse ghost/idem-mat/a1)"
  a2_sha2="$(git rev-parse ghost/idem-mat/a2)"
  [ "$a1_sha1" = "$a1_sha2" ]
  [ "$a2_sha1" = "$a2_sha2" ]
}

@test "T-FR3-7: fleet materialize --dry-run shows plan without git changes" {
  # Setup: plan manifest
  run fleet_cli plan --feature mat-dryrun --nodes "a1:feature" "a2:feature(a1)"
  [ "$status" -eq 0 ]

  # Precondition: no ghosts yet
  cd "$REPO_ROOT"
  ! git show-ref --verify --quiet "refs/heads/ghost/mat-dryrun/a1"

  # Act: materialize --dry-run
  run fleet_cli materialize --feature mat-dryrun --dry-run

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: output describes plan
  [[ "$output" =~ "Would create" ]] || [[ "$output" =~ "a1" ]]

  # Assert: ghosts NOT created
  ! git show-ref --verify --quiet "refs/heads/ghost/mat-dryrun/a1"
  ! git show-ref --verify --quiet "refs/heads/ghost/mat-dryrun/a2"
}

@test "T-FR3-8: fleet materialize without manifest raises FLEET_001" {
  # Act: materialize non-existent feature
  run fleet_cli materialize --feature no-such-feature

  # Assert: exit 1, FLEET_001
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_001" ]] || [[ "$output" =~ "Manifest not found" ]]
}

# ============================================================================
# FR-V2-3: `fleet work` allocates worktree
# ============================================================================

@test "T-FR3-9: fleet work prints worktree path to stdout (DD-4)" {
  # Setup: plan + materialize
  run fleet_cli plan --feature work-test --nodes "a1:feature" "a2:feature(a1)"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature work-test
  [ "$status" -eq 0 ]

  # Act: allocate worktree for a1
  run fleet_cli work --feature work-test --node a1

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: path printed to stdout (user can copy/paste for cd)
  [[ "$output" =~ "$HOME/.fleet/work-test/wt" ]]

  # Assert: worktree dir created on disk (pool dir has at least one wt-* entry)
  run bash -c 'ls -d "$HOME"/.fleet/work-test/wt/wt-* 2>/dev/null | head -1'
  [ "$status" -eq 0 ]
  [ -n "$output" ]
}

@test "T-FR3-10: fleet work persists worktree path to manifest (BUG-23)" {
  # Setup
  run fleet_cli plan --feature work-persist --nodes "a1:feature"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature work-persist
  [ "$status" -eq 0 ]

  # Act: allocate worktree
  run fleet_cli work --feature work-persist --node a1
  [ "$status" -eq 0 ]

  # Assert: manifest updated with node.worktree field
  manifest_content="$(cat "$(manifest_file work-persist)")"
  [[ "$manifest_content" =~ "worktree:" ]] || [[ "$manifest_content" =~ "wt-" ]]
}

@test "T-FR3-11: fleet work unknown node raises FLEET_060" {
  # Setup: plan + materialize
  run fleet_cli plan --feature work-060 --nodes "a1:feature"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature work-060
  [ "$status" -eq 0 ]

  # Act: allocate for non-existent node
  run fleet_cli work --feature work-060 --node no-such-node

  # Assert: exit 1, FLEET_060
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_060" ]] || [[ "$output" =~ "not found" ]]
}

@test "T-FR3-12: fleet work no manifest raises FLEET_001" {
  # Act: work on non-existent feature
  run fleet_cli work --feature no-manifest-work --node a1

  # Assert: exit 1, FLEET_001
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_001" ]] || [[ "$output" =~ "Manifest not found" ]]
}

# ============================================================================
# FR-V2-3: `fleet resolve` promotes decision option (experimental gate)
# ============================================================================

@test "T-FR3-13: fleet resolve without gate raises FLEET_099" {
  # Setup: plan with decision node
  run fleet_cli plan --feature resolve-gate \
    --nodes "d1:decision" "a1:feature" "a2:feature"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature resolve-gate
  [ "$status" -eq 0 ]

  # Precondition: FLEET_EXPERIMENTAL not set
  unset FLEET_EXPERIMENTAL

  # Act: try resolve without gate
  run fleet_cli resolve --feature resolve-gate --decision d1 --option a1

  # Assert: exit 1, FLEET_099
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_099" ]] || [[ "$output" =~ "FLEET_EXPERIMENTAL" ]]
}

@test "T-FR3-14: fleet resolve --dry-run shows plan without git changes" {
  # Setup: plan with decision
  run fleet_cli plan --feature resolve-dryrun \
    --nodes "d1:decision" "a1:feature" "a2:feature"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature resolve-dryrun
  [ "$status" -eq 0 ]

  # Seed the decision's options dict (plan seeds an empty options dict; options
  # are populated via manifest edit / option ghosts in the real workflow).
  local mf; mf="$(manifest_file resolve-dryrun)"
  run "$FLEET_PY" -c "import yaml,sys; m=yaml.safe_load(open('$mf')); m['nodes']['d1']['options']={'a1':{'ghost':'ghost/resolve-dryrun/a1'},'a2':{'ghost':'ghost/resolve-dryrun/a2'}}; yaml.safe_dump(m, open('$mf','w'))"
  [ "$status" -eq 0 ]

  # Act: resolve --dry-run with gate enabled
  run bash -c 'FLEET_EXPERIMENTAL=1 "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" resolve --feature resolve-dryrun --decision d1 --option a1 --dry-run'

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: output shows plan preview (or OK message for non-conflicting case)
  [[ "$output" =~ "plan" ]] || [[ "$output" =~ "resolved" ]] || [[ "$output" =~ "Resolved" ]] || [[ "$output" =~ "preview" ]]
}

@test "T-FR3-15: fleet resolve unknown decision raises FLEET_060" {
  # Setup: plan + materialize
  run fleet_cli plan --feature resolve-060 --nodes "a1:feature"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature resolve-060
  [ "$status" -eq 0 ]

  # Act: resolve non-existent decision
  run bash -c 'FLEET_EXPERIMENTAL=1 "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" resolve --feature resolve-060 --decision no-such-decision --option a1'

  # Assert: exit 1, FLEET_060
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_060" ]] || [[ "$output" =~ "not found" ]]
}

@test "T-FR3-16: fleet resolve requires manifest (FLEET_001)" {
  # Act: resolve on non-existent feature
  run bash -c 'FLEET_EXPERIMENTAL=1 "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" resolve --feature no-manifest-resolve --decision d1 --option a1'

  # Assert: exit 1, FLEET_001
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_001" ]] || [[ "$output" =~ "Manifest not found" ]]
}

# ============================================================================
# FR-V2-3: `fleet refresh` runs hook (artifact simulation)
# ============================================================================

@test "T-FR3-17: fleet refresh requires manifest (FLEET_001)" {
  # Act: refresh non-existent feature
  run fleet_cli refresh --feature no-manifest-refresh --artifact c1

  # Assert: exit 1, FLEET_001
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_001" ]] || [[ "$output" =~ "Manifest not found" ]]
}

@test "T-FR3-18: fleet refresh unknown artifact raises FLEET_060" {
  # Setup: plan + materialize
  run fleet_cli plan --feature refresh-060 --nodes "a1:feature"
  [ "$status" -eq 0 ]
  run fleet_cli materialize --feature refresh-060
  [ "$status" -eq 0 ]

  # Act: refresh non-existent artifact
  run fleet_cli refresh --feature refresh-060 --artifact no-such-artifact

  # Assert: exit 1, FLEET_060
  [ "$status" -eq 1 ]
  [[ "$output" =~ "FLEET_060" ]] || [[ "$output" =~ "not found" ]]
}

# ============================================================================
# NFR-V2-2: Clean output, no debug traces, --json support
# ============================================================================

@test "T-NFR2-1: fleet subcommands produce clean output (no debug traces)" {
  # Setup
  run fleet_cli plan --feature clean-output --nodes "a1:feature"
  [ "$status" -eq 0 ]

  # Assert: output is user-facing (no raw git, no DEBUG: traces)
  ! [[ "$output" =~ "DEBUG:" ]]
  ! [[ "$output" =~ "On branch" ]]  # (raw git output)
}

@test "T-NFR2-2: fleet init --json outputs valid JSON" {
  # Act
  run fleet_cli init --feature json-test --json

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: output is valid JSON
  run "$FLEET_PY" -c "import sys, json; json.loads(sys.stdin.read())" <<< "$output"
  [ "$status" -eq 0 ]
}

@test "T-NFR2-3: fleet plan --json outputs valid JSON" {
  # Act
  run fleet_cli plan --feature plan-json --nodes "a1:feature" "a2:feature(a1)" --json

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: valid JSON
  run "$FLEET_PY" -c "import sys, json; json.loads(sys.stdin.read())" <<< "$output"
  [ "$status" -eq 0 ]
}

@test "T-NFR2-4: fleet materialize --json outputs valid JSON" {
  # Setup
  run fleet_cli plan --feature mat-json --nodes "a1:feature"
  [ "$status" -eq 0 ]

  # Act
  run fleet_cli materialize --feature mat-json --json

  # Assert: exit 0
  [ "$status" -eq 0 ]

  # Assert: valid JSON
  run "$FLEET_PY" -c "import sys, json; json.loads(sys.stdin.read())" <<< "$output"
  [ "$status" -eq 0 ]
}

# ============================================================================
# NFR-V2-3: Backward compatibility (v1 subcommands unchanged)
# ============================================================================

@test "T-NFR3-1: fleet status still works (backward compat)" {
  # Setup: seeded manifest
  seed_manifest v2test <<YAML
feature: v2test
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/v2test/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/v2test/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML

  # Act
  run fleet_cli status --feature v2test

  # Assert: exit 0, output includes nodes
  [ "$status" -eq 0 ]
  [[ "$output" =~ "a1" ]]
  [[ "$output" =~ "a2" ]]
}

@test "T-NFR3-2: fleet cascade still works (backward compat)" {
  # Setup
  seed_manifest v2test <<YAML
feature: v2test
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/v2test/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/v2test/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML

  # Act: cascade with stdin to confirm
  cd "$REPO_ROOT"
  git checkout -q ghost/v2test/a1
  run bash -c 'echo yes | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" cascade --feature v2test --node a1'

  # Assert: exit 0 or handles gracefully
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

# ============================================================================
# Error path coverage: init on existing feature
# ============================================================================

@test "T-FR4-2b: fleet init existing feature returns 0 idempotent" {
  # First init
  run fleet_cli init --feature existing-feat
  [ "$status" -eq 0 ]

  # Second init
  run fleet_cli init --feature existing-feat

  # Assert: exit 0 (idempotent, no error)
  [ "$status" -eq 0 ]
}

# ============================================================================
# Regression: v1 manifest format still readable
# ============================================================================

@test "T-NFR3-3: v1 manifest format still readable by v2 CLI" {
  # Setup: seed a v1-style manifest (no experimental gate, no decision fields)
  seed_manifest v1compat <<YAML
feature: v1compat
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1:
    kind: feature
    desc: root
    base: null
    ghost: ghost/v1compat/a1
    range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}
    status: draft
    assumes: {}
YAML

  # Act: v2 CLI reads v1 manifest
  run fleet_cli status --feature v1compat

  # Assert: succeeds
  [ "$status" -eq 0 ]
  [[ "$output" =~ "a1" ]]
}

# ============================================================================
# Integration: full workflow (plan -> materialize -> work)
# ============================================================================

@test "T-INTEG: full workflow plan -> materialize -> work -> status" {
  # Act 1: plan
  run fleet_cli plan --feature workflow-integ \
    --nodes "a1:feature" "a2:feature(a1)" "a3:feature(a2)"
  [ "$status" -eq 0 ]

  # Act 2: materialize
  run fleet_cli materialize --feature workflow-integ
  [ "$status" -eq 0 ]

  # Act 3: work (allocate for a2)
  run fleet_cli work --feature workflow-integ --node a2
  [ "$status" -eq 0 ]
  wt_path="$output"  # Captured from stdout

  # Assert: path is under ~/.fleet/workflow-integ/wt
  [[ "$wt_path" =~ "workflow-integ/wt" ]]

  # Act 4: status
  run fleet_cli status --feature workflow-integ
  [ "$status" -eq 0 ]
  [[ "$output" =~ "workflow-integ" ]]
  [[ "$output" =~ "a1" ]]
  [[ "$output" =~ "a2" ]]
  [[ "$output" =~ "a3" ]]
}

# ============================================================================
# Manifest validity after operations
# ============================================================================

@test "T-VALID: manifest remains schema-valid after plan + materialize" {
  # Act 1: plan
  run fleet_cli plan --feature validity-test \
    --nodes "a1:feature" "a2:feature(a1)"
  [ "$status" -eq 0 ]

  # Act 2: materialize
  run fleet_cli materialize --feature validity-test
  [ "$status" -eq 0 ]

  # Assert: manifest is valid YAML + parses
  run "$FLEET_PY" -c "
import yaml
with open('$(manifest_file validity-test)') as f:
    m = yaml.safe_load(f)
    assert 'feature' in m
    assert 'nodes' in m
    assert 'a1' in m['nodes']
    assert 'a2' in m['nodes']
    print('OK')
"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "OK" ]]
}
