# Fleet — Examples

## Overview

Fleet enables you to orchestrate complex multi-node git workflows: stacked features, parallel branches, decision points, and build artifacts. Instead of manually rebasing when bases change, Fleet provides:

- **Cascade:** Automatically rebase all descendants when a base node changes.
- **Squash Cleanup (Merge-Back):** Drop a node's commits onto main, re-parenting children with a single deterministic operation.
- **Presentation:** Curate and generate logical commit groupings from technical branches, with PR integration.
- **Decision Variants:** Fork multiple implementation branches at a decision point; resolve to one and automatically re-parent others.
- **Status Tracking:** Real-time view of your DAG with node states, blocked nodes, and staleness flags.

All destructive operations are gated by dry-run preview and user confirmation. Fleet maintains a manifest (canonical source of truth) and a write-ahead log (WAL) for crash recovery.

---

## Basic Usage

### Initialize a Feature

Initialize Fleet for a new feature effort:

```bash
fleet init --feature payments
```

This creates:
- `~/.fleet/payments/manifest.yaml` (DAG definition)
- `~/.fleet/payments/.fleet.lock` (operation lock)
- `~/.fleet/payments/wt/` (worktree pool)

---

### Plan the DAG

Describe your feature as nodes with dependencies:

```bash
fleet plan --feature payments << 'EOF'
a1: Database schema migration
  base: null
  
a2: ORM layer
  base: a1
  
a3: API endpoint
  base: a2
EOF
```

This creates a `FLEET-PLAN.md` proposal. Review and confirm before materializing.

---

### Materialize Branches

Build the ghost branches and worktree pool:

```bash
fleet materialize --feature payments
```

Fleet creates:
- `ghost/payments/a1`, `ghost/payments/a2`, `ghost/payments/a3` (local working branches)
- Worktrees in `~/.fleet/payments/wt/` (up to 6 by default)

---

### Work on a Node

Allocate a worktree and start development:

```bash
fleet work --feature payments --node a1
cd ~/.fleet/payments/wt/wt-1
# Make commits, run tests...
```

---

### Cascade Descendants

After committing to `a1`, cascade the changes to all descendants:

```bash
# Preview impact
fleet cascade --feature payments --node a1 --dry-run

# Execute (requires confirmation)
fleet cascade --feature payments --node a1
```

Output:
```
DRY-RUN: Cascade rebase
  Root node: a1
  Descendants: a2, a3
  Parked worktrees: 1
    - ~/.fleet/payments/wt/wt-2 (ghost/payments/a2)
  (Conflicts possible if base has conflicting changes)

Proceed? (yes/no): yes
✓ Cascade complete
```

---

### Publish Presentation Branches

Generate a clean, curated presentation branch and open a PR:

```bash
# Preview
fleet publish --feature payments --node a1 --dry-run

# Execute
fleet publish --feature payments --node a1
```

On first publish, Fleet prompts you to curate logical commits:

```
Curate logical commits for presentation of 'a1':
  Ghost range: abc1234...def5678

Enter logical commit plan (format: 'name:sha..sha' per line, empty line to finish):
  Example: docs:abc123..def456
           impl:def456..ghi789

  > schema:abc123..def456
  > tests:def456..ghi789
  >
✓ Publish complete
  Branch: present/payments/a1
  PR: #123 (https://github.com/.../pull/123)
```

---

### Merge-Back (Squash Cleanup)

After PR #123 merges, drop `a1` and re-parent `a2` and `a3`:

```bash
# Preview
fleet merge --feature payments --node a1 --dry-run

# Execute
fleet merge --feature payments --node a1
```

Output:
```
DRY-RUN: Merge-back (squash cleanup via --onto)
  Node: a1
  Commits to drop: 5
  Children to re-parent: a2
  New base: main

Proceed? (yes/no): yes
✓ Merge complete
  a1 → archived
  a2 → re-parented to main (5 commits dropped)
```

Now `a2` is stacked directly on `main` with `a3` on top of `a2`.

---

### Check Status

View your feature DAG and node states:

```bash
fleet status --feature payments

# Output:
# Feature: payments
# Nodes:
#   a1 (feature) [archived]
#     ghost: ghost/payments/a1
#     desc: Database schema migration
#   a2 (feature) [in-progress]
#     ghost: ghost/payments/a2
#     desc: ORM layer
#   a3 (feature) [draft]
#     ghost: ghost/payments/a3
#     desc: API endpoint
```

Get structured JSON output:

```bash
fleet status --feature payments --json
```

---

## Configuration Options

### Manifest Fields

The manifest (`~/.fleet/<feature>/manifest.yaml`) controls Fleet behavior:

```yaml
feature: payments

# Node definitions
nodes:
  a1:
    kind: feature  # or 'decision' / 'artifact'
    desc: Database schema migration
    ghost: ghost/payments/a1
    base: null  # null = root node (bases on main)
    range:
      base_sha: abc1234...
      tip_sha: def5678...
    status: archived  # draft | in-progress | blocked | archived
    present:
      branch: present/payments/a1
      plan:
        - name: schema
          sha_range: abc123..def456
        - name: tests
          sha_range: def456..ghi789
      stale: false

  a2:
    kind: feature
    desc: ORM layer
    ghost: ghost/payments/a2
    base: a1  # base node (stacking)
    range:
      base_sha: def5678...
      tip_sha: xyz9012...
    status: in-progress

# Merge strategy
merge_mode: individual  # or 'batch'

# Worktree pool size
max_worktrees: 6

# Decision-variant experimental feature
experimental: false  # Set true to enable FR-17/FR-18
```

---

## Advanced Usage

### Stacked Features

Work with deep stacks efficiently. Fleet handles cascade and cleanup automatically:

```bash
# Stack 5 nodes
fleet plan --feature search << 'EOF'
index:    Indexing engine
  ↓
ranking:  Ranking algorithm (depends on index)
  ↓
filter:   Result filtering (depends on ranking)
  ↓
ui:       UI integration (depends on filter)
  ↓
docs:     Documentation (depends on UI)
EOF

fleet materialize --feature search

# Work on each node; cascade propagates changes
fleet work --feature search --node index
# ... make commits ...
fleet cascade --feature search --node index

fleet work --feature search --node ranking
# ... make commits ...
fleet cascade --feature search --node ranking

# Publish and merge each in order
fleet publish --feature search --node index
# PR #100 merges → auto-cleanup via merge-back
fleet merge --feature search --node index

fleet publish --feature search --node ranking
fleet merge --feature search --node ranking
# ... repeat for filter, ui, docs ...
```

---

### Decision Variants (Experimental)

Fork multiple implementations at a decision point; resolve to one later:

```bash
# Define decision node (requires experimental: true in manifest)
fleet plan --feature db << 'EOF'
init:      Database initialization
  ↓ (decision: orm_choice)
    ├─ optA: Use SQLAlchemy ORM
    └─ optB: Use raw SQL query builder
EOF

fleet materialize --feature db

# Both options exist as branches; work on both in parallel
fleet work --feature db --node optA
# ... implement with SQLAlchemy ...

fleet work --feature db --node optB
# ... implement with raw SQL ...

# Later, resolve to optA
fleet resolve --feature db --decision orm_choice --chosen optA --dry-run

# Preview (shows archived optB and rebuild prompts for assuming nodes)
fleet resolve --feature db --decision orm_choice --chosen optA

# Output:
# DRY-RUN: Resolve decision node
#   Decision: orm_choice
#   Chosen: optA (Archive: optB)
#   Re-parent descendants
#   Rebuild prompts for assuming nodes: [a1]
#
# ✓ Decision resolved
#   Promoted: optA
#   Archived: optB
#   Assuming node a1 marked needs-rework (lost assumption on optA)
```

Any node that assumed `optB` is flagged `needs-rework` with a rebuild contract prompt.

---

### Handle Conflicts

If a cascade hits a conflict, Fleet marks the affected node `blocked` and guides resolution:

```bash
fleet cascade --feature payments --node a1

# Output:
# FLEET_010: Merge conflict during cascade.
# Nodes marked blocked.
# Resolve in worktree via 'git rebase --continue', then retry cascade.

# Manually resolve
cd ~/.fleet/payments/wt/wt-2
git status
# (fix conflicts in editor)
git add .
git rebase --continue

# Retry cascade (idempotent; will finish remaining descendants)
fleet cascade --feature payments --node a1
# ✓ Cascade complete
```

---

### Force-Push Lease-Fail Recovery

If a reviewer pushes to a presentation branch while you're trying to re-push:

```bash
fleet publish --feature payments --node a1

# Output:
# ! Lease failed on force-push (remote ref updated)
#   Reviewer pushed commits to present/payments/a1
#
# Choices:
#   (a) Incorporate reviewer commits → merge into ghost + regenerate
#   (b) Discard reviewer work (force-push anyway)
#   (c) Manual (user resolves on GitHub)
#
# Choice: a
# → git merge origin/present/payments/a1
# → regenerate presentation
# ✓ Publish complete
```

---

### Dry-Run for Planning

Preview any operation without executing:

```bash
fleet dryrun --feature payments --intent CASCADE --nodes a1

# Output:
# DRY-RUN: Cascade rebase
#   Root node: a1
#   Descendants: a2, a3
#   Parked worktrees: 1
#   (Conflicts possible...)
```

---

## Error Handling

Fleet uses error codes (FLEET_NNN) for deterministic recovery. See `references/ERROR-CODES.md` for the full registry.

### Common Errors & Recovery

#### **Manifest not found (FLEET_001)**
```bash
fleet status --feature payments
# ✗ Error: FLEET_001: Manifest not found for feature 'payments'
# Recovery: Run `fleet init --feature payments`
```

#### **DAG divergence detected (FLEET_061)**
```bash
fleet cascade --feature payments --node a1
# ✗ Error: FLEET_061: DAG divergence detected: a2: git parent ≠ manifest base
# Recovery: Manual edit diverged git state. Reconcile won't auto-fix.
#           Update manifest or re-sync with git.
```

#### **Out-of-order merge blocked (FLEET_050)**
```bash
fleet merge --feature payments --node a2
# ✗ Error: FLEET_050: Out-of-order merge blocked
#   Merge nodes in topological order: [a1]
# Recovery: Merge ancestors first (a1), then retry.
```

#### **Worktree has uncommitted changes (FLEET_040)**
```bash
fleet cascade --feature payments --node a1
# ✗ Error: FLEET_040: Worktree has uncommitted changes; cannot cascade.
#   Commit or stash changes in: ~/.fleet/payments/wt/wt-2
# Recovery: Commit or stash in the worktree, then retry cascade.
```

---

## Reconciliation

Fleet automatically reconciles git state with manifest before destructive operations. To manually reconcile:

```bash
fleet reconcile --feature payments

# Output:
# ✓ Reconciliation complete for payments
#   Unobserved writes: a1
#   Divergences: 0
```

This recomputes:
- Node tip SHAs from git refs
- Staleness flags (artifact and presentation)
- DAG divergence checks

---

## Reference: Subcommands

| Subcommand | Purpose |
|-----------|---------|
| `init` | Initialize fleet for a feature |
| `status` | Show feature DAG and node states |
| `cascade` | Cascade rebase descendants |
| `merge` | Merge-back (squash cleanup) |
| `publish` | Publish presentation branch |
| `reconcile` | Reconcile manifest with git |
| `classify` | Classify natural language intent (internal) |
| `dryrun` | Preview operation impact |

---

## Tips & Best Practices

1. **Always use dry-run first:** Get familiar with the impact before confirming destructive ops.
2. **Keep manifests clean:** Regularly archive or delete old features to avoid clutter.
3. **One feature at a time:** Fleet is single-writer; don't run multiple fleet processes on the same feature concurrently.
4. **Commit often:** Frequent commits in worktrees make cascade and cleanup easier to understand.
5. **Review PRs before merge-back:** Cascade doesn't run after PR merge; manually trigger merge-back when ready.

---

**Version:** 1.0 (Tier 4 implementation)  
**Last Updated:** 2026-07-27
