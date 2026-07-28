# Fleet — Examples

## Overview

Fleet orchestrates multi-node git workflows: stacked features, decision forks, and build artifacts. Instead of manually rebasing when a base branch changes, Fleet holds the graph and moves everything in one deterministic pass:

- **Cascade:** Rebase all descendants when a base node changes.
- **Squash Cleanup (Merge-Back):** Drop a node's commits onto `main`, re-parenting children in a single `git rebase --onto`.
- **Presentation:** Replay curated logical commits onto `main` for a clean PR.
- **Decision Variants:** Fork alternative implementations at a decision point; resolve to one and re-parent the rest (experimental).
- **Status Tracking:** Reconciled view of the DAG with node states and staleness flags.

Every destructive operation is gated by a dry-run preview and confirmation. Fleet keeps the graph in a YAML manifest and journals every mutation to a write-ahead log (WAL) for crash recovery.

---

## CLI surface

Fleet exposes 13 subcommands. Invoke via `python scripts/fleet.py <command>` (shown below as `fleet` for brevity):

| Command | Args | Purpose |
|---------|------|---------|
| `init` | `--feature` `[--json]` | Scaffold the feature directory and write manifest |
| `plan` | `--feature` `--nodes <specs>` `\|` `--from-spec-file <file>` `[--dry-run]` `[--json]` | Create/update manifest DAG |
| `materialize` | `--feature` `[--dry-run]` `[--json]` | Create ghost branches for all nodes |
| `work` | `--feature` `--node` `[--json]` | Allocate worktree for a node |
| `cascade` | `--feature` `--node` `[--dry-run]` | Rebase descendants of a node |
| `merge` | `--feature` `--node` `[--mode individual\|batch]` `[--dry-run]` | Squash-cleanup a node onto `main` |
| `publish` | `--feature` `--node` `[--dry-run]` | Build a presentation branch and open/update a PR |
| `resolve` | `--feature` `--decision` `--option` `[--dry-run]` `[--json]` | Resolve decision node (experimental; requires FLEET_EXPERIMENTAL=1) |
| `refresh` | `--feature` `--artifact` `[--json]` | Refresh artifact by running hook |
| `status` | `--feature` `[--json]` | Render the DAG and node states (reconciles first) |
| `reconcile` | `--feature` `[--json]` | Recompute manifest derivables from git |
| `classify` | `<utterance>` `[--feature]` `[--json]` | Map a natural-language phrase to an intent |
| `dryrun` | `--feature` `--intent` `[--nodes ...]` `[--json]` | Preview an operation's impact |

---

## Basic Usage

### 1. Initialize a feature

```bash
fleet init --feature payments
```

Scaffolds the feature workspace under `~/.fleet/payments/` and writes a minimal manifest with an empty node graph.

### 2. Plan the DAG

Define a three-node stack (`a1 → a2 → a3`) using the `plan` subcommand:

```bash
fleet plan --feature payments \
  --nodes "a1:feature" "a2:feature(a1)" "a3:feature(a2)"
```

Or author `~/.fleet/payments/manifest.yaml` directly (schema in [references/MANIFEST-FORMAT.md](./references/MANIFEST-FORMAT.md)):

```yaml
feature: payments
canonical: main
merge_mode: individual
max_worktrees: 6

nodes:
  a1:
    kind: feature
    desc: "Database schema migration"
    base: null              # root node, stacks on main
    ghost: "ghost/payments/a1"
    status: draft

  a2:
    kind: feature
    desc: "ORM layer"
    base: a1                # stacked on a1
    ghost: "ghost/payments/a2"
    status: draft

  a3:
    kind: feature
    desc: "API endpoint"
    base: a2                # stacked on a2
    ghost: "ghost/payments/a3"
    status: draft
```

The `range.base_sha` / `range.tip_sha` fields are derivable — `reconcile` and `status` fill them from live git refs.

### 3. Materialize the ghost branches

Create ghost branches from `main` with a single command:

```bash
fleet materialize --feature payments
```

Or create them manually with git:

```bash
git branch ghost/payments/a1 main
git branch ghost/payments/a2 ghost/payments/a1
git branch ghost/payments/a3 ghost/payments/a2
```

### 4. Check status

```bash
fleet status --feature payments
```

```
Feature: payments
Nodes:
  a1 (feature) [draft]
    ghost: ghost/payments/a1
    desc: Database schema migration
  a2 (feature) [draft]
    ghost: ghost/payments/a2
    desc: ORM layer
  a3 (feature) [draft]
    ghost: ghost/payments/a3
    desc: API endpoint
```

Structured output for tooling:

```bash
fleet status --feature payments --json
```

### 5. Cascade descendants

After committing to `ghost/payments/a1`, rebase everything above it. Preview first:

```bash
fleet cascade --feature payments --node a1 --dry-run
```

```
DRY-RUN: Cascade rebase
  Root node: a1
  Descendants to rebase: a2, a3
  Parked worktrees: 1
    - ~/.fleet/payments/wt/wt-2 (ghost/payments/a2)
  Note: Conflicts possible if base has conflicting changes.
```

Execute (Fleet reconciles, writes a WAL intent, rebases in topological order, then writes the WAL commit):

```bash
fleet cascade --feature payments --node a1
# Cascade a1 and rebase 2 descendants? (yes/no): yes
```

```
✓ Cascade complete
```

Fleet then prints the machine-readable cascade result (rebased nodes and their new SHAs) as JSON.

### 6. Publish a presentation branch

Build a curated branch and open a PR:

```bash
fleet publish --feature payments --node a1 --dry-run
fleet publish --feature payments --node a1
```

Presentation grouping comes from the node's `present.plan` in the manifest (a pinned, user-curated list — never auto-derived):

```yaml
  a1:
    kind: feature
    desc: "Database schema migration"
    base: null
    ghost: "ghost/payments/a1"
    status: in-review
    present:
      branch: "present/payments/a1"
      plan:
        - name: schema
          sha_range: "abc123..def456"
        - name: tests
          sha_range: "def456..ghi789"
```

```
✓ Publish complete
  Branch: present/payments/a1
  PR: #123
```

### 7. Merge-back (squash cleanup)

After PR #123 lands, drop `a1`'s commits onto `main` and re-parent `a2`:

```bash
fleet merge --feature payments --node a1 --dry-run
```

```
DRY-RUN: Merge-back (squash cleanup via --onto)
  Node: a1
  Commits to drop: 5
  Children to re-parent: a2
  New base: main
```

```bash
fleet merge --feature payments --node a1
```

```
✓ Merge complete
  a1 → archived
  a2 → re-parented to main (5 commits dropped)
```

`a2` now stacks directly on `main`, with `a3` on top of `a2`.

---

## Configuration Options

The manifest (`~/.fleet/<feature>/manifest.yaml`) controls Fleet behavior. Full field reference in [references/MANIFEST-FORMAT.md](./references/MANIFEST-FORMAT.md).

| Field | Default | Purpose |
|-------|---------|---------|
| `feature` | *(required)* | Feature name |
| `canonical` | `main` | Merge target branch |
| `merge_mode` | `individual` | `individual` (staggered, topo-ordered) or `batch` (auto-cascade remaining) |
| `max_worktrees` | `6` | Worktree pool size |
| `nodes` | *(required)* | The DAG (feature / decision / artifact nodes) |

Node `status` values: `draft`, `in-review`, `merged`, `blocked`, `needs-rework`, `archived`.

Decision-variant support is experimental — see below.

---

## Advanced Usage

### Deep stacks

The same commands scale to any depth. For a five-node stack (`index → ranking → filter → ui → docs`), author all five nodes in the manifest, create their ghost branches, then work bottom-up:

```bash
# Commit to ghost/search/index, then propagate
fleet cascade --feature search --node index

# Commit to ghost/search/ranking, then propagate
fleet cascade --feature search --node ranking

# Publish + merge each in topological order
fleet publish --feature search --node index
fleet merge   --feature search --node index

fleet publish --feature search --node ranking
fleet merge   --feature search --node ranking
# ...filter, ui, docs
```

With `merge_mode: batch`, merging one node auto-cascades the remaining nodes instead of requiring a `merge` per node.

### Decision variants (experimental)

Fork alternative implementations at a decision node, then resolve to one. Define a `decision` node with `options` in the manifest:

```yaml
  orm_choice:
    kind: decision
    desc: "How to access the database?"
    base: null
    promoted: null
    options:
      optA:
        ghost: "ghost/db/optA"    # SQLAlchemy ORM
        size: small
        spec_only: false
      optB:
        ghost: "ghost/db/optB"    # raw SQL query builder
        size: small
        spec_only: false
    status: draft
```

A downstream node records which option it assumes via the `assumes` field (human-supplied, never auto-modified):

```yaml
  a1:
    kind: feature
    base: orm_choice
    ghost: "ghost/db/a1"
    assumes:
      orm_choice:
        option: optA
        why: "Model layer relies on ORM session lifecycle"
        surfaces: ["src/models.py"]
        on_flip: rebuild
    status: draft
```

Preview a resolution with the dry-run planner:

```bash
fleet dryrun --feature db --intent RESOLVE_DECISION --nodes orm_choice
```

```
DRY-RUN: Resolve decision node
  Decision: orm_choice
  Available options: optA, optB
```

The `dryrun` subcommand lists the options for a decision node. When resolution executes (e.g., via `fleet resolve --feature db --decision orm_choice --option optA`), the losing option is archived, descendants re-parent onto the winner, and any node whose `assumes` option lost is flagged `needs-rework` with a rebuild contract derived from its `assumes.on_flip` strategy.

> Decision resolution requires the experimental gate enabled: either `export FLEET_EXPERIMENTAL=1` before running `fleet resolve`, or set `experimental: true` in the manifest. Without it, you'll see error FLEET_099.

### Handle conflicts

If a cascade hits a conflict, Fleet marks the node `blocked` and stops:

```bash
fleet cascade --feature payments --node a1
```

```
FLEET_010: Merge conflict during cascade.
Nodes marked blocked.
Resolve in worktree via 'git rebase --continue', then retry cascade.
```

Resolve, then retry — cascade is idempotent and finishes the remaining descendants:

```bash
cd ~/.fleet/payments/wt/wt-2
# fix conflicts
git add .
git rebase --continue

fleet cascade --feature payments --node a1
```

```
✓ Cascade complete
```

### Force-push lease-fail recovery

`publish` force-pushes with `--force-with-lease`. If a reviewer pushed to the presentation branch first, Fleet stops and offers a choice:

```bash
fleet publish --feature payments --node a1
```

```
! Lease failed on force-push (remote ref updated)
  Reviewer pushed commits to present/payments/a1

Choices:
  (a) Incorporate reviewer commits → merge into ghost + regenerate
  (b) Discard reviewer work (force-push anyway)
  (c) Manual (resolve on GitHub)

Choice: a
→ git merge origin/present/payments/a1
→ regenerate presentation
✓ Publish complete
```

### Dry-run planner

Preview any operation without executing:

```bash
fleet dryrun --feature payments --intent CASCADE --nodes a1
```

```
DRY-RUN: Cascade rebase
  Root node: a1
  Descendants to rebase: a2, a3
  Parked worktrees: 1
    - ~/.fleet/payments/wt/wt-2 (ghost/payments/a2)
  Note: Conflicts possible if base has conflicting changes.
```

### Natural-language classification

`classify` maps a phrase to an intent and (with `--feature`) fuzzy-matches nodes:

```bash
fleet classify "merge a1 back to main" --feature payments --json
```

```json
{
  "intent": "MERGE",
  "feature": "payments",
  "nodes": ["a1"],
  "args": {},
  "confidence": 0.82,
  "requires_confirmation": false
}
```

Phrases below 0.7 confidence, or with ambiguous node matches, fall back to `STATUS` or require explicit confirmation before any destructive intent runs.

---

## Error Handling

Fleet uses stable `FLEET_NNN` codes with recovery guidance. Full registry in [references/ERROR-CODES.md](./references/ERROR-CODES.md).

#### Manifest not found (FLEET_001)

```bash
fleet status --feature payments
# ✗ Error: FLEET_001: Manifest not found for feature 'payments'
# Recovery: Run `fleet init --feature payments`, then author the manifest.
```

#### DAG divergence detected (FLEET_061)

```bash
fleet cascade --feature payments --node a1
# ✗ Error: FLEET_061: DAG divergence detected: a2: git parent ≠ manifest base
# Recovery: A manual edit diverged git from the manifest. Reconcile will not
#           auto-fix. Update the manifest or re-sync the branch with git.
```

#### Out-of-order merge blocked (FLEET_050)

```bash
fleet merge --feature payments --node a2
# ✗ Error: FLEET_050: Out-of-order merge blocked
#   Merge nodes in topological order: [a1]
# Recovery: Merge ancestors first (a1), then retry.
```

#### Worktree has uncommitted changes (FLEET_040)

```bash
fleet cascade --feature payments --node a1
# ✗ Error: FLEET_040: Worktree has uncommitted changes; cannot cascade.
#   Commit or stash changes in: ~/.fleet/payments/wt/wt-2
# Recovery: Commit or stash in the worktree, then retry cascade.
```

---

## Reconciliation

Fleet reconciles git state with the manifest before destructive operations. To reconcile manually:

```bash
fleet reconcile --feature payments
```

```
✓ Reconciliation complete for payments
  Unobserved writes: a1
  Divergences: 0
```

Reconcile recomputes derivable fields — node tip SHAs, presentation and artifact staleness, and DAG divergence — from live git refs.

---

## Tips & Best Practices

1. **Dry-run first.** Learn the impact of `cascade`, `merge`, and `publish` before confirming.
2. **Let Fleet own derivable fields.** Never hand-edit `range.*`, `present.stale`, or `deps_sha` — reconcile overwrites them.
3. **One writer per feature.** Fleet is single-writer and lock-guarded; don't run two processes on the same feature.
4. **Commit often in worktrees.** Small commits make cascade and merge-back easier to read in dry-run.
5. **Trigger merge-back yourself.** Fleet does not watch for merged PRs; run `merge` when a PR lands.

---

**Version:** 1.0
