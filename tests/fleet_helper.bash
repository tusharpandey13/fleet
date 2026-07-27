# fleet_helper.bash — shared helpers for fleet BATS integration suites.
#
# Sourced by every test-*.bats suite. Provides:
#   - toolchain resolution (borrowed python interp, script dir)
#   - git config hardening (disable gpg signing so commits never hang on pinentry)
#   - fixture builders: isolated HOME/.fleet, real git repo with a ghost-branch DAG
#   - a manifest seeder (fleet `init` is a stub, so suites seed manifests directly)
#
# All fleet state lives under $FLEET_TEST_HOME/.fleet/<feature>/ because every
# fleet module resolves paths via os.path.expanduser("~/.fleet/..."); exporting
# HOME to the sandbox keeps tests fully isolated from the real ~/.fleet.

# --- Toolchain ---------------------------------------------------------------

# Python interpreter with PyYAML + pytest on disk (borrowed; no install allowed
# in this environment — see .phase-10-output.json toolchain recipe).
FLEET_PY="${FLEET_PY:-/Users/tushar.pandey/.local/pipx/venvs/aurm-desktop-client/bin/python}"

# Absolute path to scripts/ dir (resolved relative to this helper).
FLEET_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"

# Export so child shells (e.g. `run bash -c ...`) inherit the toolchain paths.
export FLEET_PY FLEET_SCRIPTS

# Skip the whole suite if the borrowed interpreter or PyYAML is unavailable.
require_python() {
  [ -x "$FLEET_PY" ] || skip "python interp not found at $FLEET_PY"
  "$FLEET_PY" -c "import yaml" 2>/dev/null || skip "PyYAML unavailable in $FLEET_PY"
}

# Invoke the fleet CLI. Usage: fleet_cli <subcommand> [args...]
fleet_cli() {
  "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" "$@"
}

# Invoke a fleet python module directly (for surfaces with no CLI subcommand,
# e.g. decision resolution). Usage: fleet_mod <module_file> [args...]
fleet_mod() {
  local mod="$1"; shift
  "$FLEET_PY" "$FLEET_SCRIPTS/$mod" "$@"
}

# --- Git hardening -----------------------------------------------------------

# The global git config in this environment sets commit.gpgsign=true, which
# makes `git commit` block on a pinentry TTY inside non-interactive BATS runs.
# Force-disable signing for every git invocation in the suite via env config.
harden_git_env() {
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0=commit.gpgsign
  export GIT_CONFIG_VALUE_0=false
  export GIT_CONFIG_KEY_1=tag.gpgsign
  export GIT_CONFIG_VALUE_1=false
}

# --- Fixture: isolated fleet home -------------------------------------------

# Create an isolated sandbox and point HOME at it so ~/.fleet resolves inside.
# Sets: FLEET_TEST_ROOT, FLEET_TEST_HOME (=HOME), REPO_ROOT.
make_sandbox() {
  FLEET_TEST_ROOT="$(mktemp -d)"
  FLEET_TEST_HOME="$FLEET_TEST_ROOT/home"
  REPO_ROOT="$FLEET_TEST_ROOT/repo"
  mkdir -p "$FLEET_TEST_HOME" "$REPO_ROOT"
  export HOME="$FLEET_TEST_HOME"
  export FLEET_TEST_ROOT FLEET_TEST_HOME REPO_ROOT
}

cleanup_sandbox() {
  [ -n "$FLEET_TEST_ROOT" ] && rm -rf "$FLEET_TEST_ROOT"
}

# --- Fixture: real git DAG ---------------------------------------------------

# Build a real git repo in $REPO_ROOT with a linear ghost DAG:
#   main -> ghost/<feature>/a1 -> ghost/<feature>/a2
# Exports MAIN_SHA, A1_SHA, A2_SHA. Leaves cwd = $REPO_ROOT on main.
# Usage: build_linear_dag <feature>
build_linear_dag() {
  local feature="$1"
  cd "$REPO_ROOT"
  git init -q -b main
  git config user.email "test@fleet.local"
  git config user.name "Fleet Test"
  echo base > base.txt
  git add base.txt
  git commit -q -m "initial"
  MAIN_SHA="$(git rev-parse HEAD)"

  git checkout -q -b "ghost/$feature/a1"
  echo a1 > a1.txt
  git add a1.txt
  git commit -q -m "a1 work"
  A1_SHA="$(git rev-parse HEAD)"

  git checkout -q -b "ghost/$feature/a2" "ghost/$feature/a1"
  echo a2 > a2.txt
  git add a2.txt
  git commit -q -m "a2 work"
  A2_SHA="$(git rev-parse HEAD)"

  git checkout -q main
  export MAIN_SHA A1_SHA A2_SHA
}

# Build a DIAMOND DAG for tie-break testing (IT-DAG-1):
#   a1 -> {a2, a3} -> a4   (a4 has both a2 and a3 as ancestors via a1 base)
# Two parallel descendants (a2, a3) at the same depth beneath a1.
# Exports MAIN_SHA, A1_SHA, A2_SHA, A3_SHA. cwd = $REPO_ROOT on main.
build_diamond_dag() {
  local feature="$1"
  cd "$REPO_ROOT"
  git init -q -b main
  git config user.email "test@fleet.local"
  git config user.name "Fleet Test"
  echo base > base.txt; git add base.txt; git commit -q -m "initial"
  MAIN_SHA="$(git rev-parse HEAD)"

  git checkout -q -b "ghost/$feature/a1"
  echo a1 > a1.txt; git add a1.txt; git commit -q -m "a1 work"
  A1_SHA="$(git rev-parse HEAD)"

  git checkout -q -b "ghost/$feature/a2" "ghost/$feature/a1"
  echo a2 > a2.txt; git add a2.txt; git commit -q -m "a2 work"
  A2_SHA="$(git rev-parse HEAD)"

  git checkout -q -b "ghost/$feature/a3" "ghost/$feature/a1"
  echo a3 > a3.txt; git add a3.txt; git commit -q -m "a3 work"
  A3_SHA="$(git rev-parse HEAD)"

  git checkout -q main
  export MAIN_SHA A1_SHA A2_SHA A3_SHA
}

# --- Fixture: manifest seeder ------------------------------------------------

# Write a manifest for <feature> to $HOME/.fleet/<feature>/manifest.yaml and
# create an empty WAL + lock dir. `fleet init` is a no-op stub, so integration
# suites seed the manifest on disk directly (same sanctioned pattern used by
# the existing test-manifest.bats suite).
#
# Usage: seed_manifest <feature> <<'YAML'
#   ...full manifest yaml...
# YAML
seed_manifest() {
  local feature="$1"
  local dir="$HOME/.fleet/$feature"
  mkdir -p "$dir"
  cat > "$dir/manifest.yaml"
  : > "$dir/wal.jsonl"
}

# Path to the seeded manifest / WAL for assertions.
manifest_file() { echo "$HOME/.fleet/$1/manifest.yaml"; }
wal_file()      { echo "$HOME/.fleet/$1/wal.jsonl"; }
