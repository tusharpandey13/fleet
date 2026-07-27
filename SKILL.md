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

- **Cascade:** Automatically rebase all descendants when a base node changes, handling conflicts gracefully.
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

**Action:** Change node status from `draft` → `in-progress`. Run cascade on node (rebase descendants). Cascade may conflict; surface to user.

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
- Rebase children onto main via `git rebase --onto` (deterministic, single pass for all children).
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

#### **FLEET_001: Manifest not found**
**Cause:** Feature not initialized.  
**Recovery:** Run `fleet init --feature <name>` to create manifest structure.

#### **FLEET_002: DAG divergence detected**
**Cause:** Git parent ≠ manifest base (manual rebase behind fleet's back).  
**Recovery:** Reconcile and inspect. If divergence is valid (e.g., manual cherry-pick), update manifest `assumes` field to document. Do NOT auto-fix.

#### **FLEET_003: Schema validation failed**
**Cause:** Manifest corrupted or invalid JSON/YAML.  
**Recovery:** Restore from version control or WAL backup. Check manifest syntax.

#### **FLEET_010: Merge conflict during cascade**
**Cause:** Base node changes conflict with descendant branches.  
**Recovery:** 
1. Run `git rebase --continue` in conflicted worktree or main repo.
2. Mark conflict resolved.
3. Retry cascade (idempotent; will finish remaining descendants).

#### **FLEET_011: Merge conflict during cleanup**
**Cause:** Children cannot re-parent to main due to conflicts.  
**Recovery:** Same as FLEET_010.

#### **FLEET_020: Force-push lease failed**
**Cause:** Remote presentation branch updated concurrently (reviewer pushed).  
**Recovery:** Lease-fail handler (see above): incorporate or discard reviewer commits.

#### **FLEET_030: Lock timeout**
**Cause:** Another fleet process holding lock >30min (crashed process).  
**Recovery:** Check `ps aux | grep fleet` for stale processes. Kill manually if needed. Lock will auto-reclaim after 30min.

#### **FLEET_040: Worktree has uncommitted changes**
**Cause:** Cascade needs to park worktree but has dirty state.  
**Recovery:** Commit or stash changes in worktree, then retry cascade.

#### **FLEET_050: Out-of-order merge blocked**
**Cause:** Attempted merge-back before ancestor nodes archived.  
**Recovery:** Merge ancestors first (topological order enforced for data safety).

#### **FLEET_070: Low confidence; manual confirmation required**
**Cause:** NLI classification ambiguous or low confidence.  
**Recovery:** Confirm choice explicitly or rephrase utterance more clearly.

#### **FLEET_071: Fuzzy match ambiguous**
**Cause:** Multiple nodes matched keyword.  
**Recovery:** Disambiguate interactively (pick from list) or reference node by ghost branch name.

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
- `git rebase --update-refs` — cascading rebase
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
- Cascade: if descendant already rebased, skip (WAL pre-image check).
- Merge-back: if child already re-parented, skip.
- Publish: same plan + ghost → byte-identical commits.
- Reconcile: safe to call repeatedly; no side effects.

**On crash:**
- WAL intent unpaired → replay (idempotent re-execution) or rollback (restore pre-image refs).
- No data corruption; system always recovers to valid state.

---

## Examples

### Scenario 1: Stack 3 nodes, cascade when base changes

```bash
# Define plan
fleet plan --feature auth << 'EOF'
a1: Password reset endpoint
  ↓
a2: Reset email template
  ↓
a3: Send reset URL
EOF

# Materialize
fleet materialize --feature auth

# Work on a1
fleet work --feature auth --node a1
cd ~/.fleet/auth/wt/wt-1
# ... make changes, commit ...

# Mark done (cascade to a2, a3)
fleet commit --feature auth --node a1 --dry-run
# DRY-RUN: Cascade a1
#   Descendants: [a2, a3]
#   Rebase range: base^..max_tip (handles both serial + parallel)

fleet commit --feature auth --node a1
# ✓ Cascade complete (a2, a3 rebased)

# Publish each node
fleet publish --feature auth --node a1
# → present/auth/a1 created, PR #123 opened

fleet publish --feature auth --node a2
# → present/auth/a2 created, PR #124 opened
```

### Scenario 2: Decision branching

```bash
# Define decision node
fleet plan --feature db << 'EOF'
init: Database migration
  ↓ (decision: use_orm)
    ├─ optA: Use SQLAlchemy ORM
    └─ optB: Use raw SQL with query builder
EOF

# Materialize (both options exist)
fleet materialize --feature db

# Assume a1 depends on optA
fleet assume --feature db --node a1 --decision use_orm --option optA

# Work on both...
# Later, resolve to optB
fleet resolve --feature db --decision use_orm --chosen optB --dry-run
# DRY-RUN: Resolve decision node
#   Chosen: optB (Archive: optA)
#   Re-parent descendants

fleet resolve --feature db --decision use_orm --chosen optB
# ✓ Decision resolved; a1 marked needs-rework (assumption lost)
```

### Scenario 3: Merge-back sequence

```bash
# Merge a1 (no children)
fleet merge --feature auth --node a1 --dry-run
# DRY-RUN: Merge-back
#   Commits to drop: 5
#   Children to re-parent: [a2]
#   New base: main

fleet merge --feature auth --node a1
# ✓ a1 archived, a2 re-parented onto main

# Now a2 ready to merge
fleet merge --feature auth --node a2
# ✓ a2 archived, a3 re-parented onto main
# (Batch mode would auto-cascade remaining)
```

---

## Troubleshooting

### "Manifest not found"
→ Run `fleet init --feature <name>`

### "DAG divergence detected"
→ Manual edit diverged git state. Reconcile won't auto-fix. Update manifest or re-sync with git.

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
