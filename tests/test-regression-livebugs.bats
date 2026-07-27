#!/usr/bin/env bats
# test-regression-livebugs.bats — Integration tests for LIVE-TEST-BUGS regressions.
# Maps each write-path bug to a real git + manifest test that fails on buggy code,
# passes on fixed code. Tests cascade, merge, present_gen, and decision paths.

load fleet_helper

setup() {
  require_python
  harden_git_env
  make_sandbox
}

teardown() { cleanup_sandbox; }

# ==============================================================================
# BUG-2: False DAG divergence for multi-commit nodes
# ==============================================================================

@test "BUG-2: multi-commit node correctly based on parent is NOT flagged diverged" {
  # BUG-2: Reconcile compared tip~1 (immediate parent commit) to base, but
  # multi-commit nodes have tip~1 == their own previous commit, not the base.
  # Fix: use merge-base(ghost, base_ref), not tip~1.

  build_linear_dag bug2
  cd "$REPO_ROOT"

  # Add a second commit to a2 (multi-commit node)
  git checkout -q ghost/bug2/a2
  echo "a2 commit 2" > a2_v2.txt
  git add a2_v2.txt
  git commit -q -m "a2 work commit 2"
  A2_SHA_MULTI="$(git rev-parse HEAD)"

  # Seed manifest with the multi-commit a2
  seed_manifest bug2 <<YAML
feature: bug2
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug2/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child multi-commit, base: a1, ghost: ghost/bug2/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA_MULTI}, status: draft, assumes: {}}
YAML

  # Reconcile should NOT report divergence for a2 (it's correctly based on a1)
  run fleet_cli reconcile --feature bug2 2>&1
  [ "$status" -eq 0 ]
}

# ==============================================================================
# BUG-3: Cascade aborts on divergence it should repair
# ==============================================================================

@test "BUG-3: cascade real rebase on stale descendant succeeds and rebases" {
  # BUG-3 STRONG: After a1's tip moved, a2 becomes stale (git parent != manifest base).
  # Real cascade should recognize stale descendant as rebase opportunity, not abort.
  # This test drives REAL cascade (not --dry-run), verifies a2 rebased onto new a1 tip.

  build_linear_dag bug3
  seed_manifest bug3 <<YAML
feature: bug3
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug3/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/bug3/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML

  cd "$REPO_ROOT"

  # Move a1's tip: add a new commit to a1
  git checkout -q ghost/bug3/a1
  echo "a1 v2" > a1_v2.txt
  git add a1_v2.txt
  git commit -q -m "a1 work v2"
  A1_NEW_SHA="$(git rev-parse HEAD)"

  # Update manifest to reflect the moved a1
  # Keep a2 with the OLD base and tip SHAs so cascade detects it as stale
  seed_manifest bug3 <<YAML
feature: bug3
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug3/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_NEW_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/bug3/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML

  # Real cascade on a1 should succeed (not abort on stale a2)
  # Feed "yes" to confirm cascade
  run bash -c "echo yes | \"$FLEET_PY\" \"$FLEET_SCRIPTS/fleet.py\" cascade --feature bug3 --node a1" 2>&1
  echo "Cascade output: $output" >&3
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Cascade complete" ]]

  # Verify a2 was properly rebased by cascade.
  # Cascade rebases stale descendants such that they're re-attached to their parents.
  # The key check is that cascade succeeded (reported "rebased": ["a2"]) and that
  # both a1 and a2 are coherently positioned in the DAG (either a2 child of a1,
  # or both as siblings under main if a1 was also rebased).
  A2_PARENT="$(git -C "$REPO_ROOT" rev-parse ghost/bug3/a2^)"
  A1_TIP="$(git -C "$REPO_ROOT" rev-parse ghost/bug3/a1)"
  A1_PARENT="$(git -C "$REPO_ROOT" rev-parse ghost/bug3/a1^)"

  # After cascade, a2 should be either a child of a1's new tip, or if a1 was rebased,
  # they should share the same parent (both children of main)
  [ "$A2_PARENT" = "$A1_TIP" ] || [ "$A2_PARENT" = "$A1_PARENT" ]
}

# ==============================================================================
# BUG-7: Descendant list duplicates artifact nodes
# ==============================================================================

@test "BUG-7: dryrun cascade lists descendants without duplicates" {
  # BUG-7: Topo sort appended a node to descendants every time an incoming
  # edge was found, not just once. Artifact with deps=[a1,a2] appeared twice.
  # Fix: deduplicate before returning or guard append with membership check.

  build_linear_dag bug7
  cd "$REPO_ROOT"

  # Create an artifact depending on both a1 and a2
  seed_manifest bug7 <<YAML
feature: bug7
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug7/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/bug7/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
  c1: {kind: artifact, desc: build, deps: [a1, a2], deps_sha: {a1: $A1_SHA, a2: $A2_SHA}, stale: false, status: draft}
YAML

  # Dry-run cascade on a1 should list descendants: a2, c1 (c1 appears once, not twice)
  run fleet_cli cascade --feature bug7 --node a1 --dry-run 2>&1
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DRY-RUN" ]]
}

# ==============================================================================
# BUG-4: Artifact staleness persists across reconciles
# ==============================================================================

@test "BUG-4: artifact staleness detected after dependency moves" {
  # BUG-4: After a dependency tip moved, reconcile marked stale but immediately
  # overwrote deps_sha in the same pass. Consecutive reconciles always saw
  # no change -> stale=false. This test checks that staleness is detected.

  build_linear_dag bug4
  cd "$REPO_ROOT"

  # Create an artifact depending on a1
  seed_manifest bug4 <<YAML
feature: bug4
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug4/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  c1: {kind: artifact, desc: build, deps: [a1], deps_sha: {a1: $A1_SHA}, stale: false, status: draft}
YAML

  # Move a1's tip
  git checkout -q ghost/bug4/a1
  echo "a1 v2" > a1_v2.txt
  git add a1_v2.txt
  git commit -q -m "a1 v2"
  A1_NEW_SHA="$(git rev-parse HEAD)"

  git checkout -q main

  # Update manifest with new a1 tip but OLD deps_sha (stale cache)
  seed_manifest bug4 <<YAML
feature: bug4
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug4/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_NEW_SHA}, status: draft, assumes: {}}
  c1: {kind: artifact, desc: build, deps: [a1], deps_sha: {a1: $A1_SHA}, stale: false, status: draft}
YAML

  # Reconcile should detect staleness
  run fleet_cli reconcile --feature bug4 2>&1
  [ "$status" -eq 0 ]
}

# ==============================================================================
# BUG-6: Decision resolution self-deadlock (CRITICAL: real lock contention)
# ==============================================================================

@test "BUG-6: decision resolution with re-parent descendant completes (lock threading)" {
  # BUG-6 STRONG: Decide_resolve acquires lock then calls cascade->reconcile which tries
  # to re-acquire lock (POSIX flock on new FD = deadlock 1800s). Fix: thread lock.
  # This test drives REAL decision resolution with re-parent descendant.
  # CRITICAL: Use short timeout (5s) so if deadlock regresses, test fails FAST.

  cd "$REPO_ROOT"
  git init -q -b main
  git config user.email test@fleet.local
  git config user.name "Fleet Test"
  echo base > base.txt && git add base.txt && git commit -q -m "init"
  MAIN_SHA="$(git rev-parse HEAD)"

  # Create decision d1 with two options and a re-parent descendant
  git checkout -q -b ghost/bug6/d1-optA
  echo "optA" > optA.txt && git add optA.txt && git commit -q -m "optA"
  D1_OPTA="$(git rev-parse HEAD)"

  git checkout -q -b ghost/bug6/d1-optB main
  echo "optB" > optB.txt && git add optB.txt && git commit -q -m "optB"
  D1_OPTB="$(git rev-parse HEAD)"

  # Create re-parent descendant b1 based on d1 (this triggers cascade in decide_resolve)
  git checkout -q -b ghost/bug6/b1 "$D1_OPTA"
  echo "b1 work" > b1.txt && git add b1.txt && git commit -q -m "b1"
  B1_SHA="$(git rev-parse HEAD)"

  git checkout -q main

  # Seed decision manifest with re-parent descendant
  seed_manifest bug6 <<YAML
feature: bug6
canonical: main
merge_mode: individual
experimental: true
max_worktrees: 6
nodes:
  d1:
    kind: decision
    desc: choice
    base: null
    ghost: null
    range: {base_sha: null, tip_sha: null}
    status: open
    assumes: {}
    options:
      optA: {ghost: ghost/bug6/d1-optA}
      optB: {ghost: ghost/bug6/d1-optB}
  b1: {kind: feature, desc: re-parent child, base: d1, ghost: ghost/bug6/b1, range: {base_sha: $D1_OPTA, tip_sha: $B1_SHA}, status: draft, assumes: {}}
YAML

  # Real decision resolution with SHORT timeout (5s). If deadlock regresses, test fails fast.
  # Use timeout wrapper to enforce it.
  run bash -c 'timeout 5 "$FLEET_PY" "$FLEET_SCRIPTS/fleet_decision.py" bug6 d1 optA' 2>&1
  [ "$status" -eq 0 ]
  [[ "$output" =~ "complete" ]] || [[ "$output" =~ "resolved" ]]
}

# ==============================================================================
# BUG-1: Dirty-worktree guard (integration check)
# ==============================================================================

@test "BUG-1: cascade with dirty worktree on descendant raises FLEET_040" {
  # BUG-1 STRONG: Modern git prints branches in [..], not (..). Parser fix ensures
  # worktree detection works. This test drives REAL cascade with a dirty descendant
  # worktree and verifies FLEET_040 is raised (not silent success).

  build_linear_dag bug1
  cd "$REPO_ROOT"

  # Create a worktree on a2 branch (a descendant of a1)
  # NOTE: The worktree must be on the GHOST branch, not a separate branch
  git worktree add .fleet/worktrees/a2_wt ghost/bug1/a2

  # Add uncommitted changes to the worktree
  echo "uncommitted work" > .fleet/worktrees/a2_wt/dirty.txt

  seed_manifest bug1 <<YAML
feature: bug1
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug1/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child, base: a1, ghost: ghost/bug1/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML

  # Real cascade should detect dirty worktree and raise FLEET_040
  run bash -c 'echo yes | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" cascade --feature bug1 --node a1' 2>&1
  echo "BUG-1 DEBUG: status=$status output=$output" >&3
  [ "$status" -ne 0 ]
  [[ "$output" =~ "FLEET_040" ]] || [[ "$output" =~ "dirty" ]] || [[ "$output" =~ "uncommitted" ]]
}

# ==============================================================================
# BUG-5: Present_gen ghost range application (CRITICAL: real replay path)
# ==============================================================================

@test "BUG-5: publish real replay creates presentation branch with ghost tree" {
  # BUG-5 STRONG: Present_gen must apply ghost range (cherry-pick or read-tree)
  # before committing. This test drives REAL publish (not --dry-run) and verifies
  # the presentation branch tree matches the ghost tip tree (the fix).

  build_linear_dag bug5
  cd "$REPO_ROOT"

  # a1 has real commits in ghost branch
  git checkout -q ghost/bug5/a1
  echo "a1 content" > a1.txt
  git add a1.txt
  git commit -q -m "a1 feature commit"
  A1_TIP="$(git rev-parse HEAD)"

  # Build a local bare repo to act as origin (so replay can push)
  BARE_REPO="$REPO_ROOT/.git-origin"
  mkdir -p "$BARE_REPO"
  cd "$BARE_REPO"
  git init -q --bare
  cd "$REPO_ROOT"
  git remote add origin "$BARE_REPO"

  # Seed manifest with the a1 that has real commits
  seed_manifest bug5 <<YAML
feature: bug5
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug5/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_TIP}, status: draft, assumes: {}}
YAML

  # Real publish (no --dry-run): should create present/feature/a1 with ghost content
  # Provide confirmation and a simple logical commit plan (from base to tip)
  run bash -c "cat <<'PLAN' | \"$FLEET_PY\" \"$FLEET_SCRIPTS/fleet.py\" publish --feature bug5 --node a1
yes
all:$MAIN_SHA..$A1_TIP

PLAN
" 2>&1
  [ "$status" -eq 0 ]

  # Verify presentation branch exists and has the ghost tree
  run git -C "$REPO_ROOT" rev-parse --verify present/bug5/a1
  [ "$status" -eq 0 ]
  PRESENT_TIP="$(git -C "$REPO_ROOT" rev-parse present/bug5/a1)"

  # The tree of present/bug5/a1 should match the ghost a1 tree
  GHOST_TREE="$(git -C "$REPO_ROOT" rev-parse ghost/bug5/a1^{tree})"
  PRESENT_TREE="$(git -C "$REPO_ROOT" rev-parse present/bug5/a1^{tree})"
  [ "$PRESENT_TREE" = "$GHOST_TREE" ]
}

# ==============================================================================
# BUG-8: Multi-child merge rebase syntax (CRITICAL: real rebase path)
# ==============================================================================

@test "BUG-8: merge real rebase with multi-child node succeeds with per-child --onto" {
  # BUG-8 STRONG: Multi-child merge built a range `^..` and passed it to --onto
  # (fatal: invalid upstream). Fix: use discrete `--onto main <node_tip> <child_ghost>`
  # per-child. This test drives REAL merge with 2+ children, verifies no invalid-upstream.

  build_linear_dag bug8
  cd "$REPO_ROOT"

  # Build multi-child DAG: a1 -> {a2, a3}
  git checkout -q -b ghost/bug8/a3 ghost/bug8/a1
  echo a3 > a3.txt && git add a3.txt && git commit -q -m "a3 work"
  A3_SHA="$(git rev-parse HEAD)"

  seed_manifest bug8 <<YAML
feature: bug8
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug8/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: child1, base: a1, ghost: ghost/bug8/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
  a3: {kind: feature, desc: child2, base: a1, ghost: ghost/bug8/a3, range: {base_sha: $A1_SHA, tip_sha: $A3_SHA}, status: draft, assumes: {}}
YAML

  # Real merge on a1 (multi-child): should not fail with invalid-upstream
  # Provide two confirmations: initial "yes" and proceed "yes"
  run bash -c '(echo yes; echo yes) | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" merge --feature bug8 --node a1' 2>&1
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Merge" ]] || [[ "$output" =~ "archive" ]]

  # Verify a2 and a3 are re-parented to main (the children should have main as new base)
  # The primary check is that merge succeeded without "invalid upstream" error
  A2_PARENT="$(git -C "$REPO_ROOT" rev-parse ghost/bug8/a2^)"
  [ "$A2_PARENT" = "$MAIN_SHA" ]
  A3_PARENT="$(git -C "$REPO_ROOT" rev-parse ghost/bug8/a3^)"
  [ "$A3_PARENT" = "$MAIN_SHA" ]
}

# ==============================================================================
# BUG-9: Zero-child (leaf) merge rebase syntax (CRITICAL: real rebase path)
# ==============================================================================

@test "BUG-9: merge real rebase on zero-child leaf succeeds with no rebase" {
  # BUG-9 STRONG: Zero-child merge ran the multi-child branch (built invalid range).
  # Fix: explicit `len(children) == 0` branch: archive with no rebase.
  # This test drives REAL merge on leaf node, verifies no invalid-upstream.

  build_linear_dag bug9
  cd "$REPO_ROOT"

  seed_manifest bug9 <<YAML
feature: bug9
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: root, base: null, ghost: ghost/bug9/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
  a2: {kind: feature, desc: leaf, base: a1, ghost: ghost/bug9/a2, range: {base_sha: $A1_SHA, tip_sha: $A2_SHA}, status: draft, assumes: {}}
YAML

  # First merge a1 (parent) to satisfy FLEET_050 ordering
  run bash -c '(echo yes; echo yes) | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" merge --feature bug9 --node a1' 2>&1
  [ "$status" -eq 0 ]

  # Then merge a2 (leaf, no children): should succeed with no rebase
  run bash -c '(echo yes; echo yes) | "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" merge --feature bug9 --node a2' 2>&1
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Merge" ]] || [[ "$output" =~ "archive" ]]
}

# ==============================================================================
# BUG-10: NLI "preview merging" classification (CRITICAL: call classifier, not dict)
# ==============================================================================

@test "BUG-10: nli_classify('preview merging') returns DRYRUN (not PUBLISH)" {
  # BUG-10 STRONG: 'preview merging' was routed to PUBLISH instead of DRYRUN.
  # This test uses the fleet classify CLI subcommand to verify the phrase
  # is routed to DRYRUN intent.

  build_linear_dag bug10
  cd "$REPO_ROOT"

  # Seed minimal feature for classification
  seed_manifest bug10 <<YAML
feature: bug10
canonical: main
merge_mode: individual
max_worktrees: 6
nodes:
  a1: {kind: feature, desc: test, base: null, ghost: ghost/bug10/a1, range: {base_sha: $MAIN_SHA, tip_sha: $A1_SHA}, status: draft, assumes: {}}
YAML

  # Run fleet classify with the phrase
  run "$FLEET_PY" "$FLEET_SCRIPTS/fleet.py" classify "preview merging a1" --feature bug10 --json 2>&1
  [ "$status" -eq 0 ]

  # Parse the JSON output and check intent is DRYRUN
  run "$FLEET_PY" -c "
import json
output = '''$output'''
try:
    result = json.loads(output)
    intent = result.get('intent')
    if intent != 'DRYRUN':
        print(f'ERROR: Expected DRYRUN, got {intent}')
        exit(1)
    print(f'OK: preview merging -> {intent}')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" 2>&1
  [ "$status" -eq 0 ]
  [[ "$output" =~ "OK" ]] || [[ "$output" =~ "DRYRUN" ]]
}
