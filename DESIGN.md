# Fleet — Design Document

**Status:** Draft — revised after quorum review (2026-07-27). See §8.0 for what changed.
**Author:** Tushar Pandey (with Claude)
**Date:** 2026-07-27
**Audience:** Multi-party review (self, teammates, future maintainers)

---

## 1. Purpose & Problem Statement

### 1.1 The situation today

Development work spans **multiple features across multiple repositories**. Individual features tend to have **long timelines** — a single feature effort is rarely one change; it is a collection of many individual changes, each requiring its own branch, pull request, review, revision, and merge.

The current workflow is the conventional one:

- Code repositories live at `~/src/<repo-name>`, sometimes with git worktrees.
- For a feature, you create branches, make changes, open PRs, wait for approval, address review feedback, and merge to main.

This is fine mechanically, but it is **slow**, and the slowness is amplified by factors outside your control:

- **Review timelines** — waiting on reviewers.
- **Team communication** — coordinating, explaining, aligning.
- **Task sequencing dictated by those timelines** — you pick what to work on next based on who is available and what is unblocked, not based on what is technically optimal.

### 1.2 The shape of a real feature effort

A single feature effort is not linear. A representative example contains:

- **`a1`, `a2`, `a3`** — feature additions (the actual code changes).
- **`b1`, `b2`** — decisions about aspects of the change (architectural or product choices that are not yet settled).
- **`c1`, `c2`** — artifacts (a sample app / POC, a design document, Slack messages, PR-description docs).

Today these get done in a mostly **serial order**:

```
a1 → a2 → a3 → b1 → change(b1) → b2 → change(b2) → c1 → c2
```

…with reviews interleaved throughout. The serialization is the bottleneck. Each step waits on the previous one, and review latency compounds at every hop.

### 1.3 What we want instead

A system that lets a single large development effort be **iterated on in parallel, at high speed**, decoupled from team review timelines wherever possible:

- Implement `a1`, `a2`, `a3` **at the same time** using agents — either as stacked branches (when dependent) or independent branches (when mutually exclusive) — instead of waiting on the team's cadence.
- Maintain a **dedicated working copy** ("ghost") of a repository, scoped to a single feature effort, containing many branches, where cherry-pick / merge / rebase between the working copy and the canonical repo (both directions) is **easy and handled for the agent**.
- When a **review event** occurs (e.g., `a1`'s PR receives feedback), easily apply the required changes and **cascade** them to all related branches (`a2`, `a3`, and any dependent artifacts).
- For **decisions**, maintain a clear mapping of high-confidence directions to communicate to the team. Where changes are small, keep candidate branches ready for each option. Where changes are large, have a clear system by which the agent can modify related branches once a decision is taken — again, **cascade**.
- Make **artifacts** (sample apps, docs) available as soon as the individual features they depend on are ready, and keep them updated as part of cascading changes.
- Provide a **coordination system** that decides what to do in the working copy vs. the canonical repo.
- Maintain a **binding** between the canonical repo (the complete historical codebase) and the ghost (a single large, related development effort).

---

## 2. Core Model

The entire system reduces to **a graph plus a manifest plus an agent that keeps git consistent with the graph.**

### 2.0 Where truth lives (read this first)

Fleet has **two stores, with a strict division of authority** — it is *not* a single source of truth, and pretending otherwise causes silent corruption.

| Store | Authoritative for | Recoverable? |
|-------|-------------------|--------------|
| **Git refs / object store** | All *content*: commits, branch tips, parentage. | — (it is the ground truth for content) |
| **Manifest** | *Topology + intent metadata*: node kinds, edges, decision status, and the one field git cannot express — `assumes` (which decision-option a node was built believing). | Partially. See below. |

The critical distinction:

- **Derivable fields** — `range.base_sha`, `range.tip_sha`, `deps_sha`, `present.stale`, and a node's effective `base` — are a **cache/index over git**. They can always be recomputed by scanning refs. If they drift, `reconcile()` (§5.2) rebuilds them.
- **Non-derivable field** — `assumes: {b1: optA}` records *human/agent intent* ("a2 was written believing settlement is synchronous"). **No git operation records this.** It is primary state that lives only in the manifest and cannot be reconstructed from refs. It must be captured at write time and protected under the write-ahead log (§3.11). This one field is the reason fleet is a stateful system and not a view over git.

**Rule:** git is canonical for content; the manifest is a rebuildable index *plus* the single non-derivable `assumes` map. Every operation that mutates git must, in the same logical transaction, either update the manifest or leave a WAL record that lets `reconcile()` finish the job (§3.11, §5.2).

### 2.1 Four node types

| Node type | Meaning | Git representation |
|-----------|---------|--------------------|
| **Feature** (`a1`, `a2`, `a3`) | Actual code changes. Either independent (branched off main) or stacked (one built on another). | A ghost branch. |
| **Decision** (`b1`, `b2`) | An unsettled choice with N candidate options. Exactly one is promoted later. | A branch point with N candidate option-branches + status. |
| **Artifact** (`c1`, `c2`) | Sample apps, POCs, design docs, PR-description docs, Slack messages. Pure **downstream consumers** — they integrate whatever features are ready. | A ghost branch that sits atop its dependencies. |
| **Edge** | A "depends-on" relationship between nodes. | Encoded in the manifest; drives cascade order. |

**Cascade** — the central operation — travels along edges, from a changed node to its descendants. **Scope caveat:** cascade via `git rebase --update-refs` (§3.3) only re-parents *feature/decision* branches that lie within the rebased range. **Artifact nodes are deliberately outside the rebase primitive** — they consume multiple features (often via merge) and are handled by staleness signalling, not re-parenting (§3.9). So "cascade to all descendants" means: *re-parent the branch descendants that git can move; mark the artifact descendants stale.* Two mechanisms, one traversal.

Everything the system needs to do (parallel work, cascade on review, decision propagation, artifact freshness) is an operation on this graph.

### 2.2 The three tiers

The abstraction sits between the canonical repo and the physical worktrees:

```
┌─ REPO  (~/src/<repo>) ─────────────────────────────────────────┐
│  Canonical checkout. Tracks origin/main.                       │
│  Complete historical codebase. Shared git object store.        │
└────────────────────────────────────────────────────────────────┘
                          │  (shared objects — no clone tax)
┌─ FLEET  (per feature effort) ──────────────────────────────────┐
│  The coordination layer. Owns:                                 │
│   - the node DAG (features / decisions / artifacts)            │
│   - the manifest (binding between ghost and canonical)         │
│   - the worktree pool                                          │
│   - the ghost ↔ presentation branch mapping                   │
└────────────────────────────────────────────────────────────────┘
                          │  (allocates on demand)
┌─ WORKTREES  (physical checkouts, pooled) ──────────────────────┐
│  ghost/<feat>/a1, ghost/<feat>/a2, ghost/<feat>/b1-A, ...      │
│  All share the repo's object store → cross-branch git is local.│
│  Each may contain its own isolated .forge/ build state.        │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. Key Design Decisions

Each decision below records the option chosen, the alternatives, and the rationale — so reviewers can challenge any of them.

### 3.1 Worktrees, not a physical clone

**Decision:** Use `git worktree` off a single canonical repo, not a separate physical clone.

**Rationale:** The hard requirement is *easy* cherry-pick / merge / rebase between ghost and canonical, in both directions. A separate clone makes this **harder** — it requires constant fetch/push across two remotes. Worktrees share a single **git object store**, so every commit is instantly visible across all branches and cross-branch operations are purely local. "Ghost = one big dev effort" is therefore a **branch namespace + a manifest**, not necessarily a separate directory tree.

**Alternative rejected:** Physical clone. Only justified if you need true isolation (divergent dependencies/build state, or destructive experiments you don't want touching the main checkout). Not our case.

**Hard constraint — worktree/ref contention (must be handled, not a corner case):** git refuses to move or check out a branch that is already checked out in another worktree, and rewriting a ref whose branch is live in another worktree corrupts that worktree's index. A cascade that restacks the `a1 → a2 → a3` chain will therefore fail if `a1` (or any branch in the range) is currently checked out in an active worktree. **Mechanism:** before any restack, the agent parks every worktree holding a branch in the rebased range — `git -C <wt> switch --detach` — performs the cascade against the refs, then re-attaches (`git -C <wt> switch <branch>`) and reports which worktrees were touched. The dry-run (§3.11) lists the worktrees it will park. If a parked worktree has uncommitted changes, **STOP** and prompt (never stash silently).

### 3.2 Ghost branches are local-only; presentation branches are what gets pushed

**Decision:** Introduce two branch classes.

| Class | Branch pattern | Properties |
|-------|----------------|------------|
| **Ghost** | `ghost/<feat>/<node>` | Working copy. **Local-only, never pushed.** Messy history is fine. **Canonical for the node's content** (git is content truth; topology/intent live in the manifest, §2.0). |
| **Presentation** | `present/<feat>/<node>` | **Derived** from a ghost branch. Clean, logically-grouped commits. **Pushed to origin → becomes a PR.** Disposable; regenerated on demand; deleted when stale or merged. |

**Rule:** *Ghost is canonical, presentation is a render target.* You never edit a presentation branch directly. All work happens on ghost; presentation is regenerated from it.

**Presentation generation** discards ghost's internal history and produces **logical commits** (e.g., `docs`, `impl`, `tests`) — enough structure for a reviewer, none of the working-copy noise:

```
git checkout -B present/<feat>/<node> main
# curate ghost range into logical commits (squash/reorder), then:
git push --force-with-lease
```

Because presentation branches are disposable and regeneratable, staleness is trivial: a presentation branch is stale iff its source ghost tip has moved.

**Regeneration is judgment, not a pure mechanism — and it must be deterministic.** Two problems the doc previously glossed:

1. **Curation is a decision.** "Squash/reorder ghost history into logical commits" is not automated by Graphite/Sapling either; a human or an LLM chooses the grouping. Fleet treats the grouping as an explicit, stored artifact, not an ad-hoc per-run choice.
2. **Non-deterministic regrouping breaks reviewers.** If each regeneration reshuffles commits, a reviewer who approved an `impl` commit sees it rearranged on the next force-push — hostile, and it interacts badly with reviewer-pushed suggestion commits (§3.8). 

**Mechanism — the commit plan is pinned.** Each published node stores a `present.plan`: an ordered list of logical commits with the ghost commit ranges that map into each (e.g., `docs ← <sha..sha>`, `impl ← <sha..sha>`, `tests ← <sha..sha>`). Regeneration replays *the stored plan*, so the commit structure is stable across regenerations; only the contents of each logical commit change when ghost moves. The plan is edited explicitly (a curation step the user/agent confirms), never silently re-derived. Presentation is deterministic given (ghost range, plan).

### 3.3 Cascade is a solved git primitive — do not hand-roll it

**Decision:** Cascade = graph traversal + a known git primitive + a conflict policy. The agent must not reimplement rebase logic.

**Mechanism:**
- `git rebase --update-refs` (native) moves every branch ref that points into the rebased range. Enable with `git config rebase.updateRefs true`.
- The agent reads the manifest, computes the topological order of the changed node's descendants, invokes the restack primitive, and applies the conflict policy.

**Prior art confirming this is the right primitive:** Graphite (`gt sync` / `gt restack`), git-branchless (`git sync` / `git move` / `git restack`), and Sapling (`sl restack`) all implement exactly this pattern. We stand on the primitive, not the products.

### 3.4 Squash-merge handling — a mainline path, not a fallback

**Context:** The team squash-merges PRs on origin. Squash-merge is the industry's #1 source of stacked-branch pain: when `a1`'s PR is squash-merged, main receives **one** new commit `S` whose SHAs and content boundaries do **not** match `a1`'s individual commits. A branch stacked on `a1` then carries `a1`'s originals *plus* `S` — duplication and conflicts.

**Decision (corrected from the earlier draft):** Squash cleanup is **a mainline path, designed for and tested first — not a rare fallback.** The earlier draft demoted it on the premise "you control the PRs, so they usually merge together at release." That premise **contradicts fleet's own motivation** (§1): the whole point is long timelines with *staggered, review-latency-decoupled* merges where you ship what is unblocked. In that world PRs merge **individually and out of order**, which is exactly the squash-cleanup case. So:

- **Daily ghost→ghost cascade** (`--update-refs`, §3.3) remains the cheap common operation *during authoring*.
- **Merge-back to main** is expected to be **staggered/individual by default.** The `--onto` cleanup below is therefore first-class, must-work code, exercised on every individual merge. Batch merge (all PRs at release) is the *simpler special case* of the same path, not the norm.

**Mechanism (individual/staggered merge — the default):** re-parent, not rebase-onto-tip — replay only the node's *own* commits onto new main, dropping the merged originals:

```
git rebase --onto main <node.range.tip_sha> <child-branch>
```

`--update-refs` then carries the rest of the descendants.

**Consequence — a hard requirement:** the manifest must record each node's own commit range (`range.base_sha .. range.tip_sha`). The correctness of the entire `--onto` cleanup rests on `range.tip_sha` being **exactly** the node's pre-merge tip — a commit landing between the recorded tip and the merge makes `--onto` drop the wrong set. Therefore `range.tip_sha` is not merely "recorded"; it is **owned and continuously reconciled** (§5.1, §5.2): fleet updates it after *every* ghost write it initiates, and `reconcile()` recomputes it from refs before any merge-back to catch writes fleet did not observe (raw `git commit`, a forge run — §4.4). This field is mandatory and its freshness is a precondition, checked in the merge dry-run.

### 3.5 When to sync with main

**Decision:** Never pull main after every commit. Sync only at:

- **Event A — a PR merges to main.** Default (staggered): per-node `--onto` cleanup (§3.4) as each PR merges, in topological order (§3.10). Special case (batch at release): merge all presentation PRs together, then cascade the fleet's ghost branches onto the new main in one pass — the same `--onto` logic, applied once.
- **Event B — periodic main drift** (the team merged *other* people's work). Optional and low-risk; rebase ghost roots onto main when a fresh base is wanted. Merge-commits/rebase-merges replay cleanly; only *your own* squash-merged PRs need the `--onto` drop treatment.

### 3.6 Decision nodes (EXPERIMENTAL in v1 — see §3.6.1)

Git has no native concept of a "variant." A decision is pure manifest + convention. **This is the least mechanized part of the design and ships as experimental (§3.6.1); do not treat it as a shipped, reliable capability in v1.**

**Model:** `base → { optA, optB, ... }` candidate branches, plus `status: open|resolved` and `promoted: <option>`.

- **Small options** → keep all option-branches live simultaneously; optionally show the team each direction.
- **Large options** → do **not** build all of them. Store the *direction spec* (the high-confidence mapping you communicate to the team) in the manifest; build only when the choice is narrowed. Flag such options `spec_only: true`.
- **On resolve** → promote one option to canonical, re-parent everything downstream onto it, and **archive (never delete)** the losing options. The archive is the audit trail of "why we did not go that way."

**Interaction with cascade (the real complexity):** if a node (e.g., `a2`) was built assuming a losing option, that is **not a rebase** — it may be a rebuild. The manifest records which decision-option each node assumed (`assumes: {b1: optA}`). When a decision resolves against that assumption, the affected node is flagged and **the user is prompted** — the system never silently auto-rebuilds or auto-rebases semantic changes.

#### 3.6.1 The rebuild contract (why the bare flag is insufficient)

`assumes: {b1: optA}` alone records only *that* a node assumed an option, carrying **zero semantic content** about *how* it depended on it. Resolving `b1 → optB` then degenerates to a useless prompt: "a2 is invalid, good luck." Safety (archive-never-delete) is not capability (transforming a2). To make the rebuild actionable, the manifest captures, per `assumes` entry, a **rebuild contract**:

```yaml
assumes:
  b1:
    option: optA
    why: "a2 calls settle() synchronously and reads the result inline"   # the intent, human-written at build time
    surfaces: [src/pay/settle.ts, src/pay/idempo.ts]                     # files that encode the assumption
    on_flip: rebuild                                                     # rebuild | rebase | manual
```

- `why` — the captured intent. This is the non-derivable state (§2.0). Written when the node is built against the option; without it the rebuild prompt is empty.
- `surfaces` — the concrete blast radius the agent must revisit.
- `on_flip` — the declared strategy if the assumption loses.

**On resolve against an assumption**, fleet emits a structured prompt — *not* a bare flag: the losing `why`, the winning option's direction spec, the `surfaces`, and the proposed `on_flip` action. If `on_flip: rebuild`, fleet hands that payload to the user (or a `/forge` run seeded with it, §4) and marks the node `needs-rework`. The transform is human/agent-driven; fleet supplies the full context and never guesses the semantic change.

**v1 status:** decision-variants ship as a **research spike, not a headline capability.** Small-option "keep both branches live" works today (it is just parallel branches). `spec_only` large options and the flip-driven rebuild are experimental and gated (§8). Fleet does not over-promise semantic rebuild.

### 3.7 Conflict policy — always prompt

**Decision:** No auto-resolution, not even trivial classes. Any cascade/rebase/merge conflict → **STOP**, mark the node `blocked`, preserve state, show the diff, and prompt the user to act.

**Rationale:** Simplicity and trust. Auto-resolving conflicts risks silently eating work. Prompting keeps the human in the loop at exactly the moments that require judgement.

### 3.8 Force-push safety

**Decision:** Every cascade rewrites descendant history, requiring a force-push of any *pushed* (presentation) branch. Always use `--force-with-lease`, never a bare `--force`. If the lease fails (someone pushed to the presentation branch on origin), **STOP** — do not clobber.

**Not an edge case — reviewer-pushed commits are normal.** The earlier draft called presentation-branch clobber "very rare because you control the PRs." That premise is dropped: reviewers routinely push via GitHub's "commit suggestion" / "add a commit" on a PR, and in fleet's staggered-review world (§1) this is expected, not exotic. So lease failure is a **normal, handled event**, not a documented-but-unsolved corner:

- On lease failure fleet **STOPS** and does not clobber (unchanged safety).
- It then **fetches the remote presentation branch, shows the diff of the reviewer's commits against the ghost, and prompts:** (a) incorporate the reviewer's change back into the *ghost* branch (the canonical source, §3.2) then regenerate, or (b) discard, or (c) manual. Reviewer commits never live only on presentation — they are pulled back to ghost so the source of truth stays whole.

This keeps the design honest: nothing depends on assuming reviewers won't touch the PR.

### 3.9 Artifact staleness — signal, don't blind-rebuild

**Decision:** Artifacts (sample apps, docs) are **downstream consumers.** When any dependency moves, the artifact is marked **stale**, not automatically rebuilt (rebuilding a sample app on every cascade is expensive). The manifest snapshots each dependency's SHA (`deps_sha`); a mismatch raises the stale flag. The user triggers a refresh when ready.

### 3.10 Merge-back ordering

**Decision:** When merging ghost → origin with stacks + squash, order matters — merge `a1` first, run its `--onto` cleanup (§3.4), then `a2`. This staggered, dependency-ordered merge is the **default** path (§3.5), so ordering is enforced on every merge, not just at a batch release. The manifest's topological order yields the safe sequence automatically, and the agent hard-blocks "do not merge `a2` before `a1`." If origin's actual merge order violates the topology (a downstream PR merged first by someone else), fleet **STOPS** and prompts — it does not attempt an out-of-order `--onto`.

### 3.11 Recovery, write-ahead log & dry-run

**Decision:** Because every cascade is a potential multi-branch history rewrite *plus* a manifest mutation, three safety features are non-negotiable:

- **Dry-run** — print the planned rebase sequence, the worktrees to be parked (§3.1), and predicted conflicts *before* touching anything.
- **Write-ahead log (WAL) — the missing piece.** A cascade is a multi-step sequence: `park worktrees → rebase branch₁ → rebase branch₂ → … → update manifest`. `git reflog` recovers *git* but not the *manifest*: a crash mid-sequence leaves git partially rewritten and the manifest reflecting neither the old nor the new state. So every fleet operation that mutates git+manifest first appends an **intent record** to `~/src/.fleet/<feature>/wal.jsonl` (plan + pre-image ref SHAs), executes, then appends a **commit record**. On startup, an incomplete WAL entry triggers **replay-or-rollback**: fleet either finishes the recorded plan (idempotent — each step checks whether it already applied) or restores the pre-image SHAs from the WAL and reruns `reconcile()` (§5.2). The WAL is fleet's transaction journal; `operations.jsonl` is forge's and is not shared.
- **Reflog-based undo** — recover git content from a bad-but-completed cascade, paired with the WAL pre-image SHAs so the manifest is rolled back in lockstep.

Destructive operations always show a dry-run summary and require confirmation.

### 3.12 Namespacing & garbage collection

**Decision:** Never delete; archive for audit.

```
ghost/<feat>/<node>              working copy
present/<feat>/<node>            pushed presentation → PR
ghost/<feat>/archive/<node>      merged nodes & losing decision options
```

Archived and merged nodes remain in the manifest with `status: archived`.

### 3.13 Worktree pool — bounded, not unbounded

**Decision (corrected):** The earlier draft said "one worktree per active node, unbounded pool." For fleet's stated target (large, multi-feature efforts) that is dozens of full checkouts — disk pressure, `git worktree` metadata bloat, and IDE/indexer thrash. The pool is **bounded and managed**:

- **Cap** — a configurable `max_worktrees` (default a small number, e.g. 6). Nodes exceeding the cap are queued, not materialized.
- **On-demand allocation + idle detach** — a node gets a worktree when actively worked (`work`/forge run); an idle node's worktree is **detached and returned to the pool** (LRU eviction), its branch ref preserved. `worktree: null` in the manifest already models "idle, not allocated."
- **Cheap re-hydrate** — reallocating is `git worktree add` against the shared object store: fast, no clone tax. The branch tip is intact; only the working copy is re-materialized.
- **Never evict a dirty worktree** — a worktree with uncommitted changes is pinned; eviction of it **STOPS** and prompts (consistent with §3.1).

The pool is a cache of working copies over the durable branch refs, sized to the machine, not to the effort.

---

## 4. Relationship to `forge`

### 4.1 Why fleet is standalone, not built into forge

**Decision:** Fleet is a **separate skill**, not a forge feature.

**Rationale:**
- **forge is a *vertical* pipeline** — it takes a single feature through 12 phases (requirements → design → implementation → test → review → docs) to one deliverable.
- **fleet is a *horizontal* orchestrator** — it manages a graph of many branches / PRs across a long timeline with parallel workstreams.
- These are **different axes.** The overlap is superficial: both keep a state file and both use the word "cascade," but forge's cascade re-runs downstream *phases* on artifact change, whereas fleet's cascade re-parents downstream *branches* on commit change. Not reusable across the two.
- Coupling fleet's git-graph logic to forge's phase state machine adds friction with no benefit.

### 4.2 How they compose

Fleet is the conductor; forge is an optional per-node worker.

- `/fleet` creates the ghost branches and **seeds initial context** into each feature node's worktree (a spec excerpt / CONTEXT.md).
- You then, per node, invoke `/forge` inside that node's worktree to build the feature well. forge runs independently on the node's ghost branch; its output commits land there and flow through normal cascade.
- **forge is independent:** fleet does not wrap it. Fleet hands off context + a clean worktree; forge does the rest.

**But independence is not invisibility — the commit-observation contract (§4.4).** forge runs its own 12-phase machine and may commit, amend, or rewrite history on the ghost branch across phases. The moment it does, fleet's recorded `range.tip_sha` is stale (§3.4), and staleness/squash-cleanup correctness silently breaks. "forge output flows through normal cascade" is only true if fleet *observes* the new tip. Fleet must not assume forge only fast-forwards.

### 4.3 The `.forge/` directory across worktrees (verified)

- forge writes a `.forge/` directory at the **working-tree root, relative to cwd, one per checkout.** Therefore **each ghost worktree gets its own isolated `.forge/`** — isolation holds precisely because no two worktrees share a working-tree root (guaranteed by §3.13). No collision, no configuration. (Not "free" — it is a property of the worktree layout, stated so it is not mistaken for magic.)
- A global gitignore (`~/.gitignore_global`) already excludes `.forge/`, so it never pollutes any repo tree.
- **No shared/fleet-common `.forge/`.** forge assumes one `.forge` per checkout (it maintains its own internal `.git`, `state.json`, and per-feature directories); sharing one across worktrees would corrupt its state. Fleet keeps its **own** metadata separately (§5). If fleet needs cross-node build visibility, it **reads** each node's `.forge/state.json` and aggregates — read, never shared write. **Reads are point-in-time and may be mid-run:** forge writes `state.json` atomically (temp+rename), so fleet never reads a torn file, but it may read a state that reflects only some phases. Fleet treats an in-progress forge state as *advisory* (for the status dashboard), never as a trigger for a git mutation.

### 4.4 The commit-observation contract

Because forge (or a raw `git commit`, or any agent) can move the ghost tip without fleet in the loop, fleet **never trusts `range.tip_sha` as authoritative for content** — it is a cache (§2.0). The contract:

- **After any operation fleet initiates** on a ghost branch, fleet updates `range.tip_sha` immediately.
- **After any hand-off it does not control** (a `/forge` run, a user working directly in the worktree), fleet runs `reconcile()` (§5.2) over the affected branch *before* the next operation that depends on the range — always before a merge-back, a squash cleanup, or a staleness check.
- **Cheap detection:** fleet snapshots each ghost ref SHA it last saw (`range.tip_sha`); if the live ref differs, an unobserved write happened → reconcile and recompute derived fields. Optionally a git `post-commit` hook installed in each worktree pings fleet, but the design does **not** depend on the hook — the pre-operation reconcile is the guarantee; the hook is an optimization.

Net: forge stays fully independent and fleet stays correct, connected only by "reconcile from refs before you rely on the range." No assumption that forge fast-forwards.

```
~/src/.fleet/<feature>/manifest.yaml   ← fleet: the DAG, decisions, artifacts, mappings
<worktree-a1>/.forge/                  ← forge: a1's build state (global-gitignored)
<worktree-a2>/.forge/                  ← forge: a2's build state (global-gitignored)
```

---

## 5. The Manifest (the binding)

A single file binds ghost to origin. It is **not** the sole source of truth — git is canonical for content; the manifest is a rebuildable index over git plus the one non-derivable field, `assumes` (§2.0). It lives **outside** any repo so it stays clean and survives repo operations.

- `~/src/fleet/` — the skill implementation.
- `~/src/.fleet/<feature>/` — per-feature data, including `manifest.yaml`, `wal.jsonl` (§3.11), and the worktree pool.

### 5.0 Single-writer discipline & reconcile

Two rules keep the manifest and git from diverging:

**(1) Single writer.** All manifest mutation goes through **one code path** in the fleet skill — the reconcile/commit routine. Nothing else writes `manifest.yaml`. forge does not; agents do not; the user edits it only via fleet operations. This removes the "who updates `range.tip_sha`?" ambiguity that broke the earlier draft: fleet owns every write, wrapped in the WAL (§3.11).

**(2) `reconcile(git → manifest)` — the recompute pass.** A single function that reads git refs and rewrites all **derivable** fields from ground truth:

```
reconcile(feature):
  for each node with a ghost branch:
    range.tip_sha  = rev-parse(ghost)                       # from ref
    range.base_sha = merge-base(ghost, node.base_branch)    # from graph
    effective base = actual parent in git (flag if != manifest base)
  for each artifact:
    stale = any(dep.current_sha != deps_sha[dep])           # recomputed
  for each published node:
    present.stale = (present source ghost tip moved)
  # assumes.* is NEVER touched — non-derivable, human-supplied (§2.0)
  detect DAG divergence: if git parentage disagrees with manifest edges → report, do not auto-fix
```

`reconcile()` is called: on startup (with WAL replay), after any unobserved write (§4.4), and before every merge-back/cleanup/staleness check. It is idempotent. It **never** invents `assumes` data and **never** silently "fixes" a DAG mismatch — divergence between git parentage and manifest edges is surfaced to the user, because it usually means a manual rebase happened that fleet must understand before proceeding.

**DAG consistency check.** Because `base`/`deps` edges are recorded redundantly with git parentage, `reconcile()` validates them against the actual commit graph and flags any node whose manifest `base` no longer matches its git parent. This catches "someone rebased a2 onto main behind fleet's back" before a cascade acts on a false topology.

### 5.1 Schema

```yaml
feature: payments
repo: ~/src/pay
canonical: main
merge_mode: individual            # individual (staggered, DEFAULT → squash-cleanup path §3.4) | batch (release together, special case)
worktree_pool: ~/src/.fleet/payments/wt-*
max_worktrees: 6                  # bounded pool (§3.13); idle nodes detach LRU

nodes:
  a1:
    kind: feature
    desc: "retry logic on payment gateway"   # human label for natural-language resolution
    base: main
    ghost: ghost/pay/a1
    range: {base_sha: <sha>, tip_sha: <sha>} # DERIVABLE cache over refs; owned by fleet, recomputed by reconcile() before any merge-back (§5.0). Correctness of --onto cleanup depends on freshness.
    worktree: wt-1                           # current allocation | null (idle → detached to pool §3.13)
    assumes: {}                              # NON-DERIVABLE (§2.0); rebuild contract per decision (§3.6.1): {option, why, surfaces, on_flip}
    present: {branch: present/pay/a1, pr: 123, stale: false, plan: [{docs: <sha..sha>}, {impl: <sha..sha>}, {tests: <sha..sha>}]} # plan pinned for deterministic regeneration (§3.2)
    status: in-review                        # draft | in-review | merged | blocked | needs-rework | archived

  a2:
    kind: feature
    desc: "idempotency keys"
    base: a1                                 # stacked
    ghost: ghost/pay/a2
    range: {base_sha: <sha>, tip_sha: <sha>}
    worktree: null
    assumes: {}
    present: null                            # not published yet
    status: draft

  b1:
    kind: decision
    desc: "sync vs async settlement"
    base: main
    status: open                             # open | resolved
    promoted: null
    options:
      optA: {ghost: ghost/pay/b1-A, size: small}
      optB: {ghost: ghost/pay/b1-B, size: big, spec_only: true}

  c1:
    kind: artifact
    desc: "poc sample app"
    deps: [a1, a2, b1]
    assumes:                                 # rebuild contract (§3.6.1)
      b1: {option: optA, why: "poc wires the sync settle() path", surfaces: [app/pay.ts], on_flip: rebuild}
    deps_sha: {a1: <sha>, a2: <sha>}         # DERIVABLE; recomputed by reconcile() for staleness (§5.0)
    ghost: ghost/pay/poc
    stale: true
    status: draft
```

---

## 6. User Interface — Natural Language First

### 6.1 Principle

Fleet is a skill. The user does **not** memorize commands. The contract is **natural language**: the user describes intent, the skill classifies it, runs the internal operation, and reports back.

```
user (natural language) → INTENT CLASSIFIER → internal op → git + manifest mutation → report
```

Internal operations (`init`, `add`, `work`, `commit`, `review`, `sync`, `decide`, `artifact refresh`, `merge`, `status`, `dryrun`) exist as the skill's own vocabulary and may be invoked explicitly, but are never required of the user.

### 6.2 The flagship intent — PLAN

The **primary front door.** The user describes the whole effort at a high level, references a spec file and any useful PRs, and fleet proposes a full plan before building anything.

**Example invocation:**

> `/fleet` I want to add a feature to this repo. The spec/PRD is in `<file.md>`. It has 3 individual sub-features (…). At some point we need to decide whether to do X or Y (…). We need a POC sample app from the beginning, plus a design doc, Slack messages, and a PR-description doc. Take care of A and B. PR #123 might be useful.

**What the planner does:**

1. Reads the referenced spec file(s); parses the freeform brief.
2. Fetches referenced PRs via `gh` (e.g., `gh pr view 123`).
3. Builds a proposed node DAG.
4. Writes a **`FLEET-PLAN.md`** proposal — nodes, decisions, artifacts, dependencies, build order, references, and **open questions.**
5. **Surfaces open questions and waits for approval.** It does not guess risky calls (stacking vs. independence, build-both-options vs. spec-only). It never materializes unprompted.
6. On approval → **materializes**: manifest + branch namespace + worktree pool + per-node context seed.

**Extraction rules:**

| From the brief | → Plan element |
|----------------|----------------|
| "3 individual sub-features…" | 3 feature nodes; stacked vs. independent inferred from wording ("builds on" → stacked; "separately" → parallel) — confirmed via open question if ambiguous |
| "at some point decide X or Y" | a decision node with option stubs, `status: open` |
| "POC sample app from the beginning" | an artifact node, `deps: [features]`, flagged early-build |
| "design doc, Slack messages, PR-desc doc" | artifact nodes (docs), deps mapped |
| "PR #123 might be useful" | `gh pr view 123` → attached as a reference/base to the relevant node |
| "take care of A and B" | constraints → per-node context seed |
| the spec/PRD file | read → each requirement mapped to a node; `desc` fields populated |

### 6.3 Example `FLEET-PLAN.md` (proposal, pre-materialize)

```markdown
# FLEET PLAN — payments   (proposed, not yet built)
repo: ~/src/pay   canonical: main   merge_mode: batch

## Nodes
a1  feature  "retry logic on gateway"     base: main   → independent
a2  feature  "idempotency keys"           base: a1     → stacked (needs a1 types)
a3  feature  "webhook dedup"              base: main   → independent
b1  decision "sync vs async settlement"   base: main   → open
      optA: sync  (small)
      optB: async (big, spec-only until chosen)
c1  artifact "poc sample app"             deps:[a1,a2,a3]  → build early, per-feature-ready
c2  artifact "design doc"                 deps:[b1]        → after decision
c3  artifact "PR description doc"         deps:[a1,a2,a3]

## References
PR #123 → attached to a3 (prior webhook work; use as base)

## Build order (topological)
a1, a3 parallel → a2 → [b1 decision] → c1 tracks features live → c2 after b1 → c3 at release

## Open questions (need answers before build)
- a2 stacked on a1 — correct, or independent?
- b1 optB (async) — build both now, or spec-only until decided?
- c1 POC — depends on all 3 features, or is a subset OK early?

## Will create
worktrees: allocated on demand, bounded pool (max_worktrees, idle nodes detach §3.13)
branches:  ghost/pay/{a1,a2,a3,b1-A,b1-B}, ghost/pay/{poc,design,prdesc}
manifest:  ~/src/.fleet/payments/manifest.yaml
```

### 6.4 Intent → operation map

| The user says (examples) | Intent | Internal operation |
|--------------------------|--------|--------------------|
| "add feature X, spec in file.md, 3 subfeatures, a decision, need POC+docs, PR #123 useful…" | **PLAN** | read spec + PRs → propose `FLEET-PLAN.md` → confirm → materialize |
| "looks good, go" / "build the plan" | MATERIALIZE | create manifest + branches + worktrees + context seed |
| "start feature X on repo Y" | INIT | `init` |
| "we also need a sample app depending on a1 + a2" | ADD_NODE (kind inferred) | `add` |
| "work on a1" / "build the auth piece with forge" | WORK (+ optional forge handoff) | `work` |
| "a1 is done" / "forge finished a2" | COMMIT → CASCADE | `commit` + auto-cascade descendants |
| "a1 is ready for review" / "put up the PR for auth" | PUBLISH | `review` (generate presentation → push → PR) |
| "reviewer wants a null-check on the auth PR" | REVIEW_FIX | `sync` (confirm node → fix ghost → cascade → regenerate from pinned plan → push-lease) |
| "we're going with option A for storage" | RESOLVE_DECISION | `decide` (+ structured rebuild prompt §3.6.1 for assuming nodes) |
| "refresh the sample app" | REFRESH_ARTIFACT | `artifact refresh` |
| "everything's approved, ship it" / "merge a1 only" | MERGE (staggered default; batch = special case) | `merge` |
| "where are we?" / "what's blocked?" | STATUS | `status` (reads each node's `.forge/state.json`, renders DAG) |
| "what happens if I rebase a1?" / "preview merging" | DRYRUN | `dryrun` |

**Node references are natural too.** The user says "the auth PR," not "a1." The manifest's `desc` field enables fuzzy resolution ("the auth PR" → the node whose `present.pr` matches or whose branch/desc contains "auth"). An ambiguous match lists candidates and asks.

**Safety gate on the NL→destructive path (mandatory).** Several intents (REVIEW_FIX, RESOLVE_DECISION, MERGE) end in multi-branch history rewrites or force-pushes. A misclassified intent or a wrong fuzzy `desc` match would rewrite the *wrong* node. Therefore: **any intent that resolves to a destructive operation must (1) echo the resolved node(s) by `desc` *and* branch back to the user, (2) show the dry-run (§3.11), and (3) require explicit confirmation before touching git.** Fuzzy resolution never auto-fires a destructive op; a low-confidence or multi-candidate match always stops and asks. The dry-run summary is a named artifact (planned rebases, worktrees parked, predicted conflicts), not free-form prose, so confirmation is meaningful rather than fatigue-driven.

**Active-feature context.** Multiple fleets can coexist under `~/src/.fleet/*`. The active one is inferred from cwd (inside a worktree → that feature), else the last-touched feature, else the skill asks. The user need not name the feature in every utterance.

### 6.5 Event → action state machine

Every git-mutating row is wrapped by: `reconcile()` if an unobserved write may have happened (§4.4) → park affected worktrees (§3.1) → WAL intent record (§3.11) → execute → WAL commit → update manifest (single writer, §5.0).

| Event | Action |
|-------|--------|
| ghost commit (manual or forge) | `reconcile()` the branch to catch unobserved writes (§4.4); update `range.tip_sha`; park worktrees in range; cascade branch descendants via `rebase --update-refs`; mark dependent artifacts + presentation branches stale |
| node ready for review | curate/confirm `present.plan` (§3.2); generate presentation branch by replaying the pinned plan from ghost → push `--force-with-lease` → open PR |
| review lands on a PR (reviewer comment) | fix on the **ghost** branch → cascade ghost→ghost → regenerate presentation from pinned plan → push `--force-with-lease` |
| decision resolved | promote chosen option → re-parent downstream onto it → for any node whose `assumes` names the losing option, emit the **structured rebuild prompt** (§3.6.1: `why` + surfaces + winning spec + `on_flip`) and mark `needs-rework` → archive losers |
| artifact refresh (manual) | rebuild against current `deps_sha`; clear stale |
| merge-back — staggered/individual (DEFAULT) | in topological order, per node: `rebase --onto main <range.tip_sha> <children>` after `reconcile()` verifies the tip; drop merged commits; update ranges; mark merged. STOP if origin merge order violates topology (§3.10) |
| merge-back — batch (special case) | merge all presentation PRs → cascade fleet ghosts onto new main in one `--onto` pass |
| conflict (any operation) | **STOP** → mark node `blocked` → show diff → prompt the user |
| presentation lease fail (reviewer pushed) | **STOP** → fetch remote, diff reviewer commits vs ghost → prompt: pull into ghost / discard / manual (§3.8) — normal event, not an edge |
| crash mid-operation | on next start: WAL replay-or-rollback (§3.11) → `reconcile()` → report |
| unobserved write detected (live ref ≠ `range.tip_sha`) | `reconcile()` the branch before any dependent op (§4.4) |

### 6.6 Skill control flow

```
1. Detect intent. A freeform brief with spec/goals → PLAN (the default front door).
2. PLAN: read referenced files; gh-fetch referenced PRs; parse the brief →
   build a proposed DAG → write FLEET-PLAN.md → surface open questions →
   WAIT for approval. Never materialize unprompted.
3. MATERIALIZE on approval → manifest + git namespace + worktree pool + per-node context seed.
4. Post-build: the natural-language router (§6.4) drives all operations.
5. Any destructive operation → dry-run summary + confirmation. Any conflict → STOP + prompt.
6. forge handoff on request, per node, independent.
```

---

## 7. Prior Art (research summary)

No single tool combines all of fleet's concerns. The landscape splits into three buckets; fleet borrows the primitive from the first and the manifest idea from the second.

| Bucket | Tools | What we take / why it's insufficient alone |
|--------|-------|--------------------------------------------|
| **Stacked-PR workflows** | Graphite (`gt sync`/`restack`), git-branchless (`git sync`/`move`/`restack`), Sapling (`sl restack`), StGit | The restack/cascade primitive. All auto-detect merged branches and re-parent children; Graphite's and Sapling's squash handling is proprietary/internal. None model decision-variants or artifact cascade. |
| **Manifest-driven multi-repo** | Android `repo`, Gerrit relation chains | The idea of a declarative manifest describing a branch/PR topology. Built for extreme multi-repo scale, not for a single-feature branch DAG with variants. |
| **Monorepo coordination** | Lerna, Cargo/workspaces | Artifact/package sync via CI conventions. No branch-graph or decision concept. |

**Confirmed facts used in the design:**
- `git rebase --update-refs` is native and moves all branch refs in the rebased range (`git config rebase.updateRefs true`). It does **not** detect squash-merges — hence the recorded `range` and the mainline `--onto` path (§3.4). It also only moves refs *inside* the rebased range, so artifact branches (multi-feature consumers) are outside it by design (§2.1, §3.9).
- Squash-merge breaking stacked branches is a universally acknowledged pain (GitHub's own docs warn about it).
- AI-agent parallel development converges on **git worktree + `gh` + a custom orchestration/manifest layer** — precisely fleet's shape.

**Conclusion — scoped honestly.** Two claims must be kept separate, because "no combined tool exists" does **not** by itself prove the combination is coherent (a gap can exist because the concerns don't belong together):

1. **The parallel-authoring half is justified and buildable now** — worktrees + ghost DAG + `--update-refs` daily cascade + deterministic presentation/PR generation. No off-the-shelf tool combines these for agent-driven work, and the mechanism is sound. This is v1.
2. **The decision-variant half is a genuine gap but an unproven mechanism** — it ships experimental (§3.6.1), gated behind the consistency machinery (single-writer, WAL, reconcile). Market absence is treated as *opportunity*, not as validation; the mechanism earns its place only after the rebuild contract is exercised in practice.

Building fleet is justified for (1); (2) is a research spike carried alongside, not a shipped guarantee.

---

## 8. Resolved by review + Remaining Open Questions

### 8.0 Resolved in this revision (quorum review, 2026-07-27)

These were flaws in the first draft, now corrected in-doc:

- **Two-store truth model made explicit (§2.0)** — git canonical for content; manifest = rebuildable index + one non-derivable field (`assumes`). The false "single source of truth" claim is removed.
- **`range.tip_sha` ownership (§4.4, §5.0)** — single writer + `reconcile()` + pre-operation reconcile-from-refs. No longer unowned.
- **Squash cleanup promoted to mainline (§3.4)** — matches the staggered-review motivation; batch is the special case.
- **WAL added (§3.11)** — manifest mutations are now crash-recoverable, not just git reflog.
- **Worktree/ref contention handled (§3.1)** — park/detach before restack.
- **Decision-rebuild contract (§3.6.1)** — `why`/`surfaces`/`on_flip`; feature marked experimental.
- **"Control all PRs" premise dropped (§3.8, §3.10)** — reviewer pushes and out-of-order merges are normal, handled events.
- **Bounded worktree pool (§3.13).**
- **Deterministic presentation via pinned plan (§3.2).**
- **Ship split (§7)** — authoring half v1; decision-variant half experimental.

### 8.1 Remaining open questions for reviewers

1. **Presentation commit granularity** — logical commits (docs/impl/tests) is the default `plan` template. Right level for reviewers, or per-repo tuning?
2. **Decision archival retention** — losing options archived, never deleted. Prune point, or permanent audit trail?
3. **Cross-repo features** — one fleet per repo in v1. Multi-repo effort out of scope. Confirm acceptable.
4. **Main-drift refresh cadence** — Event B (§3.5) is manual. Prompt when main drifts beyond a threshold?
5. **forge aggregation depth** — `status` reads each node's `.forge/state.json` (advisory, may be mid-run §4.3). How much forge detail before noise?
6. **`assumes.why` capture ergonomics (new)** — the rebuild contract needs human-written intent at build time (§3.6.1). How is this prompted for without becoming friction that gets skipped (leaving the field empty and the rebuild useless)?
7. **`max_worktrees` default (new)** — §3.13 suggests 6. Right default for the target machine class, or auto-size to disk/RAM?
8. **reconcile() cost at scale (new)** — for a large DAG, reconcile-before-every-op scans many refs. Acceptable, or does it need incremental/dirty-tracking?

---

## 9. Glossary

| Term | Meaning |
|------|---------|
| **Fleet** | The coordination layer for one large feature effort; owns the DAG, manifest, worktree pool, and ghost↔presentation mapping. |
| **Ghost branch** | Local-only working branch; canonical for a node's *content* (git is content truth §2.0). `ghost/<feat>/<node>`. |
| **Presentation branch** | Derived, clean, pushed branch that becomes a PR. Disposable. `present/<feat>/<node>`. |
| **Node** | A vertex in the effort graph: a feature, a decision, or an artifact. |
| **Cascade** | Propagating a change from a node to its descendants: `rebase --update-refs` re-parents branch descendants; artifact descendants are marked stale (§2.1, §3.9). |
| **Decision node** | A branch point with N candidate options; one is promoted, the rest archived. Rebuild-on-flip is experimental (§3.6.1). |
| **Artifact node** | A downstream consumer (sample app, doc) that integrates ready features; outside the rebase primitive. |
| **Manifest** | The binding file between ghost and canonical: a rebuildable index over git plus the non-derivable `assumes` map (§2.0). Not a sole source of truth. Lives in `~/src/.fleet/<feature>/`. |
| **reconcile()** | The single pass that recomputes all derivable manifest fields from git refs and flags DAG divergence (§5.0). |
| **WAL** | Write-ahead log (`wal.jsonl`) journaling git+manifest mutations for crash replay-or-rollback (§3.11). |
| **Rebuild contract** | Per-`assumes` record (`why`/`surfaces`/`on_flip`) that makes a decision flip actionable (§3.6.1). |
| **Materialize** | Turn an approved `FLEET-PLAN.md` into a real manifest + branches + worktrees. |
