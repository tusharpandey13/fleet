# Fleet — Multi-Node Branch Orchestration Skill

---

## Metadata

**name:** Fleet  
**description:** Orchestrate complex multi-node git feature branches with deterministic cascade, squash cleanup, and presentation generation. Works with stacked features, decision variants, and artifacts.  
**version:** 1.0 (Tier 4 implementation)  
**triggers:** fleet, branch orchestration, cascade, merge-back, publish, decision resolve  
**integration:** Git, GitHub CLI (gh), local manifest YAML, write-ahead log (WAL)  

---

## Overview

Fleet enables engineers to work with complex multi-node git workflows: stacked features, parallel branches, decision points, and build artifacts. Instead of manually rebasing when bases change, Fleet provides:

- **Cascade:** Automatically rebase all descendants when a base node changes. On conflict, the affected node is marked `blocked` and the rebase is aborted for manual resolution (not silently merged).
- **Squash Cleanup (Merge-Back):** Drop a node's commits onto main, re-parenting children with a single deterministic operation.
- **Presentation:** Curate and generate logical commit groupings from technical branches, with PR integration.
- **Decision Variants:** Fork multiple implementation branches at a decision point; resolve to one and automatically re-parent others.
- **Artifacts:** Track build outputs or derived branches; mark stale when dependencies change.

Fleet maintains a **manifest** (canonical source of truth) and a **write-ahead log (WAL)** for crash recovery. All destructive operations are gated by dry-run preview and user confirmation.

---

## When to Use Fleet

✅ **Fleet is ideal for:**
- Stacked feature development (a1 → a2 → a3, all on different branches)
- Long-lived feature branches with frequent base updates
- Experimental implementations with decision branches
- Teams needing deterministic, auditable branch operations
- Complex PR workflows with curated commit presentation

❌ **Fleet may not be ideal for:**
- Simple single-branch PRs (use native git)
- Fully collaborative branches (too much external velocity)
- Temporary throwaway branches

---

## Intent Classification

Fleet understands 11 high-level intents, automatically inferred from natural language. Each intent is mapped to one or more Tier 1-3 operations.

### 1. **PLAN** — Specify DAG structure
Keywords: "plan", "spec", "design", "outline"  
Example: *"Plan the authentication refactor with 3 nodes"*

**Action:** Create or update `FLEET-PLAN.md` with node definitions, edges (base/deps), and descriptions. Dry-run shows proposed DAG.

**Safety:** Non-destructive; user can iterate.

---

### 2. **MATERIALIZE** — Build branches from manifest
Keywords: "materialize", "create", "init", "setup", "build"  
Example: *"Materialize the plan"*

**Action:** Create ghost branches for all nodes in manifest, starting from main. Set up worktree pool. Wire up edges (feature stacking, artifact deps).

**Safety:** Non-destructive if branches already exist (idempotent).

---

### 3. **WORK** — Allocate worktree for a node
Keywords: "work", "start", "begin", "allocate", "checkout"  
Example: *"Start work on node a2"*

**Action:** Allocate worktree from pool (LRU eviction if full), attach to node's ghost branch. Print worktree path for `cd`.

**Safety:** Safe. Worktree-pool bounded at 6 by default; dirty worktrees never evicted.

---

### 4. **COMMIT** — Mark node as done + cascade
Keywords: "commit", "done", "finish", "complete", "mark"  
Example: *"Mark a1 done"*

**Action:** Run cascade on the node — rebase each descendant onto its parent's
**new tip** via per-node `git rebase --update-refs --onto <new_base> <old_base>
<ghost>`, in topological order (parents before children). Artifacts are not
rebased (only marked stale). Conflicts mark the node `blocked` and abort the
rebase (FLEET_010).

**Real command:** `fleet cascade --feature <f> --node <n> [--dry-run]` (there is
no `fleet commit` subcommand).

**Verify:** cascade prints `✓ Cascade complete` + a `rebased` list, but confirm
refs actually moved (`git merge-base --is-ancestor <root_ghost> <descendant_ghost>`
or a follow-up `reconcile` showing no divergence) — a success message alone is
not proof the DAG advanced.

**Safety:** **Destructive.** Requires dry-run preview + confirmation. Confidence gate: <0.7 or ambiguous nodes require explicit confirmation.

---

### 5. **PUBLISH** — Generate presentation branch
Keywords: "publish", "pr", "present", "push", "generate"  
Example: *"Publish a2"*

**Action:** 
- If plan absent, prompt user to curate logical commits.
- Replay plan onto main → presentation branch (deterministic).
- Force-push with `--force-with-lease` (safe against concurrent reviewer edits).
- Create or update PR.
- Mark presentation not-stale.

**Safety:** **Destructive (force-push).** Lease-fail handler: if reviewer pushed, offer (incorporate/discard/manual).

---

### 6. **REVIEW_FIX** — Resolve conflicts or fix code
Keywords: "fix", "review", "resolve", "address"  
Example: *"Fix the cascade conflict"*

**Action:** Surface conflict state (git rebase --continue status), guide user through manual conflict resolution or automatic recovery strategies.

**Safety:** **Destructive.** Confidence gate enforced. Guides manual resolution.

---

### 7. **RESOLVE_DECISION** — Pick winning option at decision node
Keywords: "decide", "resolve", "choose", "pick", "select"  
Example: *"Resolve the database design decision on optA"*

**Action:** (Experimental, gated by manifest flag.)
- Promote chosen option (archive losing branches).
- Re-parent descendants onto chosen ghost.
- For each assuming node: emit rebuild contract prompt if assumption lost.
- Cascade to propagate re-parenting.

**Safety:** **Destructive.** Experimental (T-D1..T-D7). Requires decision node + clear option choice.

---

### 8. **ARTIFACT_REFRESH** — Mark artifact staleness cleared
Keywords: "refresh", "regenerate", "update", "artifact"  
Example: *"Refresh the build artifact"*

**Action:** Run artifact build/regeneration logic (user-defined hook), mark stale=False, update deps_sha snapshot.

**Safety:** Non-destructive (depends on artifact hook implementation).

---

### 9. **MERGE** — Squash cleanup via --onto
Keywords: "merge", "cleanup", "squash", "integrate"  
Example: *"Merge a1 back to main"*

**Action:**
- Validate topological order (individual mode): all ancestors must be archived.
- Show commits to drop, children to re-parent.
- Re-parent children onto main via **per-child** `git rebase --onto main <node_tip> <child_ghost>` (multi-child adds `--update-refs`; zero-child = archive only, no rebase). NOT a single `A..B` range pass.
- Mark node archived, clear presentation.
- Update children's base_sha.
- (Batch mode: auto-cascade remaining nodes.)

**Safety:** **Destructive.** Dry-run shows exact commits dropped. Topological order enforced in individual mode.

---

### 10. **STATUS** — Show feature DAG + node states
Keywords: "status", "show", "list", "blocked", "state"  
Example: *"Show status"*

**Action:** Reconcile (recompute derivables from git), render DAG with node statuses, highlight blocked nodes, show staleness, performance <500ms.

**Safety:** Read-only.

---

### 11. **DRYRUN** — Preview operation impact
Keywords: "dry", "preview", "simulate", "dry-run"  
Example: *"Preview merging a1"*

**Action:** Show impact summary (commits dropped, worktrees parked, conflicts predicted) without executing.

**Safety:** Read-only preview.

---

## Safety Gates & Confirmation

### Confidence Threshold
Destructive intents (COMMIT, MERGE, REVIEW_FIX, RESOLVE_DECISION) require **confidence ≥ 0.7** or explicit confirmation.

**Scenarios triggering confirmation:**
- Low confidence classification (<0.7)
- Ambiguous fuzzy node match (multiple candidates)
- No nodes matched but destructive intent inferred

**Confirmation flow:**
```
⚠ Low confidence (0.65). Manual confirmation required.
  Intended intent: MERGE
  Matched nodes: [a1, a2] (ambiguous)
  
Choices:
  (1) a1
  (2) a2
  (c) Cancel
  
Select: 1
Confirm MERGE on a1? (yes/no): yes
→ Execute merge_back(feature, "a1")
```

### Dry-Run Gates
All destructive intents trigger automatic dry-run preview:

```
DRY-RUN: Merge-back (squash cleanup via --onto)
  Node: a1
  Commits to drop: 5
  Children to re-parent: [a2]
  New base: main

Proceed? (yes/no): yes
→ Execute
```

### Force-Push Safety (Lease-Fail Handler)
When PUBLISH force-pushes presentation branch:

```
! Lease failed on force-push (remote ref updated)
  Reviewer pushed commits to present/a1

Choices:
  (a) Incorporate reviewer commits → merge into ghost + regenerate
  (b) Discard reviewer work (force-push anyway)
  (c) Manual (user resolves on GitHub)
  
Choice: a
→ git merge origin/present/a1, regenerate, retry push
```

---

## Intent → Operation Mapping

| Intent | Tier 1-3 Functions | Dry-Run | Confirmation |
|--------|-------------------|---------|--------------|
| PLAN | fleet_manifest (schema validation) | Schema check | None |
| MATERIALIZE | fleet_manifest + git branch creation | Branch preview | None |
| WORK | fleet_worktree (alloc + attach) | Worktree list | None |
| COMMIT | fleet_cascade | cascade dry-run | ✓ Required |
| PUBLISH | fleet_present (plan curation + replay) | Presentation diff | ✓ Lease-fail |
| REVIEW_FIX | Reconcile + surface conflict state | Conflict diff | ✓ Required |
| RESOLVE_DECISION | fleet_decision (promote + re-parent) | Decision preview | ✓ Required |
| ARTIFACT_REFRESH | User-defined hook + manifest update | Deps snapshot | None |
| MERGE | fleet_merge (--onto + topo order) | Merge diff | ✓ Required |
| STATUS | fleet_reconcile (derivables) + render | State snapshot | None |
| DRYRUN | fleet_dryrun (impact preview) | Operation summary | None |

---

## Error Handling & Recovery Advice

Fleet uses error codes (FLEET_NNN) for deterministic recovery guidance.

### Common Errors & Recovery

> Codes below are the ones the source actually raises. `FLEET_001`, `FLEET_070`,
> and `FLEET_071` do **not** exist — do not branch on them. Low-confidence /
> ambiguous-node handling is surfaced interactively by the NLI layer (see
> "Safety Gates"), not via an error code.

#### **FLEET_002: DAG has a cycle**
**Cause:** Manifest node edges form a cycle (e.g. a1.base=a2 and a2.base=a1); caught by cycle detection during schema validation. (This is a **cycle**, not a rebase divergence — divergence is reported by `reconcile` as a non-fatal "git parent ≠ manifest base" note, no error code.)  
**Recovery:** Fix the offending `base`/`deps` edge in the manifest so the DAG is acyclic.  
**Source:** `fleet_manifest.py` (`_has_cycle`).

#### **FLEET_003: Schema validation failed**
**Cause:** Manifest missing a required field, `nodes` not a dict, a node missing `kind`/invalid `kind`, or a decision node missing its `options` dict.  
**Recovery:** Restore from version control or WAL backup; fix the field flagged in the message.  
**Source:** `fleet_manifest.py`.

#### **FLEET_010: Merge conflict during cascade**
**Cause:** A descendant rebase conflicts while cascading onto the moved base. Affected nodes are marked `blocked` and the rebase is auto-aborted (`git rebase --abort`).  
**Recovery:**
1. Open the blocked node's ghost branch, resolve the overlap manually.
2. Re-run cascade — it is idempotent and finishes the remaining descendants.  
**Source:** `fleet_cascade.py`.

#### **FLEET_011: Merge conflict during cleanup (merge-back)**
**Cause:** A child cannot re-parent onto main during merge-back due to conflicts. Node marked `blocked`.  
**Recovery:** Same as FLEET_010 (resolve overlap, retry merge).  
**Source:** `fleet_merge.py`.

#### **FLEET_020: Force-push lease failed** *(handler-surfaced, not thrown)*
**Cause:** Remote presentation branch updated concurrently (reviewer pushed) → `--force-with-lease` refused.  
**Recovery:** The lease-fail handler offers **incorporate / discard / manual** (see "Safety Gates"). This is surfaced by the publish handler, not raised as an exception code.  
**Source:** `fleet_merge.py` / `fleet_present.py` handler path.

#### **FLEET_030: Lock timeout**
**Cause:** Another fleet process held the feature lock beyond the timeout.  
**Recovery:** Check `ps aux | grep fleet` for a stale/crashed process; kill it. Then retry. (Note: `flock` is per-open-fd — never call a lock-acquiring op from inside another that already holds the lock; pass `lock_obj` through instead.)  
**Source:** `fleet_lock.py`, `fleet_wal.py`.

#### **FLEET_040: Worktree has uncommitted changes**
**Cause:** Cascade/detach/evict needs to park a worktree that is dirty.  
**Recovery:** Commit or stash the worktree's changes, then retry. (Cascade auto-parks and re-attaches clean worktrees; only dirty ones block.)  
**Source:** `fleet_cascade.py`, `fleet_worktree.py`.

#### **FLEET_041: Worktree pool full**
**Cause:** Pool at `max_worktrees` (default 6) and no idle worktree to evict.  
**Recovery:** Finish/commit work in an existing worktree so it becomes evictable, or raise `max_worktrees` in the manifest.  
**Source:** `fleet_worktree.py`.

#### **FLEET_050: Out-of-order merge blocked**
**Cause:** Merge-back attempted before an ancestor node is archived.  
**Recovery:** Merge ancestors first (topological order enforced for data safety).  
**Source:** `fleet_merge.py`.

#### **FLEET_099: Decision resolution not enabled**
**Cause:** `RESOLVE_DECISION` attempted without the experimental gate.  
**Recovery:** Set top-level `experimental: true` in the manifest, then retry.  
**Source:** `fleet_decision.py` (`decide_resolve`).

---

## Integration Points

### Manifest (Canonical Source of Truth)
**File:** `~/.fleet/<feature>/manifest.yaml`

```yaml
feature: payments
nodes:
  a1:
    kind: feature
    desc: "Database schema migration"
    ghost: ghost/pay/a1
    range:
      base_sha: abc1234...
      tip_sha: def5678...
    status: in-progress

  a2:
    kind: feature
    desc: "ORM layer"
    base: a1
    ghost: ghost/pay/a2
    range:
      base_sha: def5678...
      tip_sha: ghi9012...
    status: draft
```

**Operations read/write manifest:**
- `manifest_read()` for all operations
- `manifest_write_atomic()` wraps in WAL intent/commit
- Reconcile updates derivables (tip_sha, base_sha, staleness)

### Write-Ahead Log (WAL)
**File:** `~/.fleet/<feature>/wal.jsonl` (append-only)

```jsonl
{"timestamp": "2026-07-27T14:32:10Z", "op_type": "cascade", "status": "intent", "payload": {...}}
{"timestamp": "2026-07-27T14:32:35Z", "op_type": "cascade", "status": "commit", "payload": {...}}
```

**Used for:**
- Crash recovery (incomplete intent → replay or rollback on startup)
- Audit trail (who did what, when)
- Idempotence (replay checks pre-images to avoid re-executing)

**Operations log:**
- Cascade (rebase plan)
- Merge-back (--onto plan)
- Presentation generation (plan + push)
- Reconcile (derivable updates)

### Git Integration
**Commands used:**
- `git rev-parse` — get SHA for refs
- `git merge-base` — compute common base
- `git rebase --update-refs --onto <new_base> <old_base> <ghost>` — per-node cascading rebase (topological, parents first)
- `git push --force-with-lease` — safe force-push
- `git worktree` — manage isolated worktrees
- `git branch -m` — rename branches (archive old options)

**Invariants maintained:**
- Ghost branches (fleet-managed) are read-only from user perspective
- Presentation branches may be updated by reviewers (lease-fail handler)
- Main branch is never modified by fleet (only merge-base computed)

### GitHub Integration
**Commands used:**
- `gh pr create` — create PR for presentation branch
- `gh pr view` — check PR status (future: automated reviews)

**PR flow:**
- Presentation branch force-pushed to origin
- PR created/updated via gh CLI
- Reviewers comment and push review commits
- Lease-fail handler reconciles concurrent edits

---

## Manifest Format & Node Types

### Node Kinds

**Feature:** Software component on a branch stack.
```yaml
kind: feature
ghost: ghost/pay/a1
base: (ancestor feature or null)
range: {base_sha, tip_sha}
status: draft | in-progress | blocked | archived
```

**Decision:** Fork point for multiple implementations (experimental).
```yaml
kind: decision
options:
  optA: {ghost: ghost/pay/optA}
  optB: {ghost: ghost/pay/optB}
promoted: (chosen option)
status: draft | resolved | archived
```

**Artifact:** Build output or derived branch (non-interactive).
```yaml
kind: artifact
deps: [a1, a2]  # Feature dependencies
deps_sha: {a1: sha1, a2: sha2}  # Snapshot of deps when built
stale: (true if any dep changed)
```

### Node Status

- **draft:** Not yet materialized or planned.
- **in-progress:** Actively developed.
- **blocked:** Conflict or error; manual fix required.
- **archived:** Merged back to main; cleanup complete.

### Staleness Flags

**Artifact staleness:**
- Marked when any dep changes (reconcile detects).
- Cleared by ARTIFACT_REFRESH operation.

**Presentation staleness:**
- Marked when ghost tip moves (cascade rebases).
- Cleared by PUBLISH operation.

---

## Performance & Guarantees

### Performance Targets (NFR-8)

| Operation | Target | Notes |
|-----------|--------|-------|
| Cascade (10 descendants) | <10s | Parallel rebase + park/unpark |
| Merge-back | <5s | Rebase + manifest write |
| Publish | <3s | Plan replay + push |
| Status/reconcile | <500ms | Derivable recomputation |

### Idempotence Guarantees

All operations are **idempotent**:
- Cascade: skip a descendant only when its recorded base already equals the
  parent's new tip (per-node `old_base == new_base` check). "Already based on
  the root's *base*" is NOT the skip condition — that mistake produced a
  no-op false-success historically. Always verify refs moved (see COMMIT).
- Merge-back: if child already re-parented, skip.
- Publish: same plan + ghost → byte-identical commits.
- Reconcile: safe to call repeatedly; no side effects.

**On crash:**
- WAL intent unpaired → replay (idempotent re-execution) or rollback (restore pre-image refs).
- No data corruption; system always recovers to valid state.

---

## Examples

> **Real CLI surface.** `fleet` exposes exactly these subcommands:
> `init` (stub — prints only, writes nothing), `status`, `cascade`, `merge`,
> `publish`, `reconcile`, `classify`, `dryrun`. There is **no** `fleet plan`,
> `materialize`, `work`, `assume`, `resolve`, or `commit` subcommand. Those
> intents map to manifest edits + git + the module scripts below:
> - **RESOLVE_DECISION** → `python scripts/fleet_decision.py <feature> <decision> <option>` (experimental)
> - **PUBLISH** → `fleet publish …` OR `python scripts/fleet_present.py <feature> <node>`
> - **WORK / is-dirty** → `python scripts/fleet_worktree.py <feature> is-dirty <path>`
> - **PLAN / MATERIALIZE / assume / artifact-refresh** → edit the manifest + git branch, then `fleet reconcile`.
> Every `fleet` command must run with **cwd = the repo clone** (modules operate on
> the process cwd; the manifest `repo:` field is not read for main git ops).

### Scenario 1: Stack 3 nodes, cascade when base changes

```bash
# PLAN + MATERIALIZE: seed the manifest, then create ghost branches by hand.
# (init is a stub; write the manifest via manifest_write_atomic, or edit YAML.)
#   ~/.fleet/auth/manifest.yaml  with nodes a1, a2(base=a1), a3(base=a2)
git branch ghost/auth/a1 main
git branch ghost/auth/a2 ghost/auth/a1
git branch ghost/auth/a3 ghost/auth/a2
# ... commit work onto each ghost branch ...
fleet reconcile --feature auth        # fills range.base_sha / tip_sha

# COMMIT intent = cascade. Preview, then execute.
fleet cascade --feature auth --node a1 --dry-run
# DRY-RUN: Cascade rebase
#   Descendants to rebase: a2, a3

fleet cascade --feature auth --node a1     # prompts yes/no
# ✓ Cascade complete  (a2, a3 rebased onto a1's new tip)
# VERIFY (do not trust the printed status alone — see note below):
git merge-base --is-ancestor ghost/auth/a1 ghost/auth/a2 && echo "a2 rebased"

# PUBLISH each node (force-push + PR). Needs a remote + gh.
fleet publish --feature auth --node a1
fleet publish --feature auth --node a2
```

### Scenario 2: Decision branching

```bash
# PLAN a decision node d1 with options optA/optB in the manifest; set
# top-level `experimental: true` (else FLEET_099). MATERIALIZE option ghosts.
git branch ghost/db/optA main
git branch ghost/db/optB main
# Record an assumption on a dependent node by editing the manifest:
#   nodes.a1.assumes.d1 = {option: optA, on_flip: rebuild}

# RESOLVE_DECISION is a module script (no `fleet resolve` subcommand):
python scripts/fleet_decision.py db d1 optB
# ✓ optB promoted; optA archived (renamed archive/…); descendants re-parented
#   onto optB via cascade; assuming nodes emit a rebuild-contract prompt.
```

### Scenario 3: Merge-back sequence

```bash
# Merge a1 first (topological order enforced; ancestors before descendants).
fleet merge --feature auth --node a1 --dry-run
# DRY-RUN: Merge-back (squash cleanup via --onto)
#   Commits to drop: 5
#   Children to re-parent: [a2]
#   New base: main

fleet merge --feature auth --node a1       # prompts yes/no
# ✓ a1 archived, a2 re-parented onto main

fleet merge --feature auth --node a2
# ✓ a2 archived, a3 re-parented onto main
```

---

## Troubleshooting

### "Manifest not found"
→ Seed `~/.fleet/<feature>/manifest.yaml` (via `manifest_write_atomic` or by hand). `fleet init` is a stub and does not create it. There is no FLEET_001.

### "DAG has a cycle" (FLEET_002)
→ A `base`/`deps` edge forms a loop. Fix the offending edge so the DAG is acyclic. (A rebase *divergence* is different — reconcile reports it as a non-fatal note, no code.)

### "Cascade blocked by dirty worktree"
→ Commit or stash changes, retry cascade.

### "Merge out-of-order"
→ Merge ancestors first. Topological order enforced for safety.

### "Lease failed on force-push"
→ Reviewer pushed; choose: (a) incorporate, (b) discard, (c) manual resolve on GitHub.

### "Classification ambiguous"
→ Pick from list or be more specific: *"merge the auth node"* instead of *"merge auth"*.

---

## Under the Hood

Fleet is implemented as 14 units across 4 tiers:

- **Tier 1:** Manifest I/O, WAL, lock, schema
- **Tier 2:** Reconcile, worktree pool
- **Tier 3:** Cascade, merge-back, presentation, decision (experimental)
- **Tier 4:** NLI classifier, dry-run planner, CLI orchestrator (this skill)

All operations are atomic (lock-guarded), durable (WAL-logged), and idempotent (pre-image checks).

---

## FAQ

**Q: Can I manually edit manifest?**  
A: Yes, but reconcile won't auto-sync; changes must be idempotent and schema-valid.

**Q: What happens if I force-push a ghost branch?**  
A: Reconcile detects the unobserved write, flags it, and updates manifest. No data loss.

**Q: Can I use fleet with multiple people on same feature?**  
A: No; fleet is single-writer (lock-guarded). Multiple people on same feature → manual coordination or fork separate features.

**Q: How do I recover from a crash?**  
A: Startup checks for incomplete WAL entries (unpaired intent/commit) and prompts: replay (idempotent re-execute) or rollback (restore pre-images).

**Q: Is there a GUI?**  
A: Not yet. Fleet is CLI-only. This skill provides NL interface via utterance classification.

---

**Version:** 1.0 (Phase 8, Tier 4)  
**Last Updated:** 2026-07-27  
**Author:** Fleet Engineering Team
