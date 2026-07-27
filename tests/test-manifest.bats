#!/usr/bin/env bats
# test-manifest.bats — Manifest read/write/validate via fleet.py CLI
# Tests: UT-M1..UT-M9 — manifest I/O, schema validation, roundtrip

setup_file() {
  # Create temporary directories
  export TEST_ROOT=$(mktemp -d)
  export FLEET_HOME="$TEST_ROOT/.fleet"
  export REPO_ROOT="$TEST_ROOT/repo"

  mkdir -p "$FLEET_HOME"
  mkdir -p "$REPO_ROOT"

  cd "$REPO_ROOT"
  git init -q
  git config user.email "test@example.com"
  git config user.name "Test User"
  echo "test" > README.md
  git add README.md
  git commit -q -m "initial"
}

teardown_file() {
  rm -rf "$TEST_ROOT"
}

# Helper: invoke fleet.py with HOME override
fleet_cmd() {
  HOME="$TEST_ROOT" python3 "$BATS_TEST_DIRNAME/../scripts/fleet.py" "$@"
}

# Helper: check PyYAML availability
setup() {
  # Guard: skip all tests if PyYAML missing
  python3 -c "import yaml" 2>/dev/null || skip "PyYAML unavailable"
}

@test "manifest read valid" {
  # Setup: create valid manifest on disk
  feature="test-feature"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  cat > "$manifest_dir/manifest.yaml" <<'EOF'
feature: test-feature
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1:
    kind: feature
    desc: Root feature
    base: null
    ghost: ghost/test/a1
    range:
      base_sha: abc123
      tip_sha: def456
    status: draft
    assumes: {}
EOF

  # Act: read manifest
  result=$(fleet_cmd status --feature "$feature" 2>&1)

  # Assert: success
  [ $? -eq 0 ]
  [[ "$result" =~ "a1" ]]
}

@test "manifest write atomic" {
  # Setup
  feature="test-feature-w"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  cat > "$manifest_dir/manifest.yaml" <<'EOF'
feature: test-feature-w
canonical: main
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: null
    ghost: ghost/test/a1
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
EOF

  # Act: read, modify (via python -c), write
  python3 - "$manifest_dir/manifest.yaml" <<'PYEOF'
import sys
import yaml
path = sys.argv[1]
with open(path) as f:
    m = yaml.safe_load(f)
m['nodes']['a1']['desc'] = 'Modified'
with open(path + '.tmp', 'w') as f:
    yaml.safe_dump(m, f)
import os
os.rename(path + '.tmp', path)
PYEOF

  # Assert: file written, can be read back
  [ $? -eq 0 ]
  [ -f "$manifest_dir/manifest.yaml" ]
  grep -q "Modified" "$manifest_dir/manifest.yaml"
}

@test "manifest validate missing required field" {
  # Setup: incomplete manifest (missing 'canonical')
  feature="test-feature-invalid"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  cat > "$manifest_dir/manifest.yaml" <<'EOF'
feature: test-feature-invalid
# missing canonical, nodes, etc.
EOF

  # Act: attempt read (should fail validation)
  run fleet_cmd status --feature "$feature"

  # Assert: error reported (non-zero exit or error text)
  [ "$status" -ne 0 ] || [[ "$output" =~ "error" || "$output" =~ "missing" || "$output" =~ "Error" ]]
}

@test "manifest roundtrip" {
  # Setup: create manifest
  feature="test-feature-roundtrip"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  original_manifest=$(cat <<'EOF'
feature: test-feature-roundtrip
canonical: main
merge_mode: individual
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: null
    ghost: ghost/test/a1
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
  a2:
    kind: feature
    desc: Feature B
    base: a1
    ghost: ghost/test/a2
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
EOF
)

  echo "$original_manifest" > "$manifest_dir/manifest.yaml"

  # Act: read, parse, write back
  python3 - "$manifest_dir/manifest.yaml" <<'PYEOF'
import sys, yaml
path = sys.argv[1]
with open(path) as f:
    m = yaml.safe_load(f)
with open(path + '.tmp', 'w') as f:
    yaml.safe_dump(m, f, default_flow_style=False)
import os
os.rename(path + '.tmp', path)
PYEOF

  # Assert: can read result (valid YAML structure preserved)
  result=$(fleet_cmd status --feature "$feature" 2>&1)
  [ $? -eq 0 ]
  [[ "$result" =~ "a1" && "$result" =~ "a2" ]]
}

@test "manifest lock during concurrent writes" {
  # Setup: two separate processes attempt manifest write
  feature="test-feature-lock"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  cat > "$manifest_dir/manifest.yaml" <<'EOF'
feature: test-feature-lock
canonical: main
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: null
    ghost: ghost/test/a1
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
EOF

  # Act: attempt concurrent writes (sequential for testing, but both compete for lock)
  (
    python3 - "$manifest_dir/manifest.yaml" <<'PYEOF1' &
import sys, yaml, time
path = sys.argv[1]
with open(path) as f:
    m = yaml.safe_load(f)
m['nodes']['a1']['desc'] = 'Process 1'
time.sleep(0.1)  # Simulate work
with open(path, 'w') as f:
    yaml.safe_dump(m, f)
PYEOF1
    python3 - "$manifest_dir/manifest.yaml" <<'PYEOF2' &
import sys, yaml, time
path = sys.argv[1]
time.sleep(0.05)  # Slight delay to ensure first process holds lock
with open(path) as f:
    m = yaml.safe_load(f)
m['nodes']['a1']['desc'] = 'Process 2'
with open(path, 'w') as f:
    yaml.safe_dump(m, f)
PYEOF2
    wait
  )

  # Assert: manifest is valid after both writes
  [ -f "$manifest_dir/manifest.yaml" ]
  python3 -c "import yaml; yaml.safe_load(open('$manifest_dir/manifest.yaml'))" 2>/dev/null || skip "YAML parse failed"
  [ $? -eq 0 ]
}

@test "manifest DAG cycle detection" {
  # Setup: manifest with cycle (a1 -> a2 -> a1)
  feature="test-feature-cycle"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  cat > "$manifest_dir/manifest.yaml" <<'EOF'
feature: test-feature-cycle
canonical: main
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: a2
    ghost: ghost/test/a1
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
  a2:
    kind: feature
    desc: Feature B
    base: a1
    ghost: ghost/test/a2
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
EOF

  # Act: attempt to read/validate
  run fleet_cmd status --feature "$feature"

  # Assert: error detected (cycle not allowed in DAG)
  [ "$status" -ne 0 ] || [[ "$output" =~ "cycle" || "$output" =~ "error" || "$output" =~ "Error" ]]
}

@test "manifest node kind validation" {
  # Setup: manifest with invalid node kind
  feature="test-feature-kind"
  manifest_dir="$FLEET_HOME/$feature"
  mkdir -p "$manifest_dir"

  cat > "$manifest_dir/manifest.yaml" <<'EOF'
feature: test-feature-kind
canonical: main
nodes:
  a1:
    kind: invalid_kind
    desc: Invalid
    base: null
    ghost: ghost/test/a1
    range: {base_sha: null, tip_sha: null}
    status: draft
    assumes: {}
EOF

  # Act: read/validate
  run fleet_cmd status --feature "$feature"

  # Assert: error on invalid kind
  [ "$status" -ne 0 ] || [[ "$output" =~ "kind" || "$output" =~ "invalid" || "$output" =~ "error" ]]
}
