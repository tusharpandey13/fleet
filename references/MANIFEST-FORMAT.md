# Manifest Format Reference

**Location:** `~/.fleet/{feature}/manifest.yaml`

**Purpose:** Canonical metadata file binding git refs (ghost/presentation branches) to logical feature nodes (feature/decision/artifact). Source of truth for DAG topology, node states, and non-derivable intent (`assumes` field).

---

## Top-Level Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `feature` | string | **required** | Feature name (e.g., "payments") |
| `repo` | string | optional | Repository root path (for reference; inferred from `.git`) |
| `canonical` | string | "main" | Canonical branch (merge target) |
| `merge_mode` | string | "individual" | Merge strategy: "individual" (staggered) or "batch" |
| `max_worktrees` | integer | 6 | Maximum worktree pool size |
| `nodes` | dict | **required** | Feature DAG nodes (see Node Fields below) |

---

## Node Fields

Every entry in `nodes` is a dictionary with the following structure:

### Common Fields (all node kinds)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | enum | **required** | "feature", "decision", or "artifact" |
| `desc` | string | optional | Human-readable label for NL fuzzy resolution |
| `status` | enum | "draft" | Node state: "draft" \| "in-review" \| "merged" \| "blocked" \| "needs-rework" \| "archived" |
| `base` | string\|null | null | Parent node (for feature stacking); null for root nodes |
| `ghost` | string\|null | null | Local working branch (e.g., "ghost/pay/a1"); null if not materialized |
| `range` | dict | `{}` | Cached commit range (derivable; recomputed by `reconcile()`) |
| `range.base_sha` | string\|null | null | Merge-base SHA with base branch; derivable |
| `range.tip_sha` | string\|null | null | Current ghost tip SHA; derivable, continuously reconciled |
| `worktree` | string\|null | null | Allocated worktree ID (e.g., "wt-1"); null if idle |
| `assumes` | dict | `{}` | Non-derivable intent: decision assumptions (human-supplied, never auto-modified) |

### Feature-Specific Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `present` | dict\|null | null | Presentation record (see Presentation Fields below); null if unpublished |

#### Presentation Fields (present.*)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `branch` | string | (derived) | Presentation branch name (e.g., "present/pay/a1") |
| `pr` | int\|null | null | GitHub PR number |
| `stale` | boolean | false | True if ghost moved since last regeneration (derivable) |
| `source_tip_sha` | string\|null | null | Ghost tip SHA at last regeneration; derivable |
| `plan` | list | `[]` | Pinned logical commit grouping: `[{name: str, sha_range: str}, ...]` |

### Decision-Specific Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `promoted` | string\|null | null | Chosen option (e.g., "optA") on resolve; null if unresolved |
| `options` | dict | **required** | Decision options: `{option_name: {ghost: str, size: str, spec_only: bool}, ...}` |
| `options[*].ghost` | string | (required) | Option branch name (e.g., "ghost/pay/b1-A") |
| `options[*].size` | enum | **required** | "small" (both option branches live) or "big" (spec-only until chosen) |
| `options[*].spec_only` | boolean | false | True if option not yet materialized as a branch |

### Artifact-Specific Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `deps` | list | **required** | Dependency node names (e.g., `["a1", "a2"]`) |
| `deps_sha` | dict | `{}` | Cached dependency SHAs: `{node_name: sha, ...}` (derivable, recomputed by `reconcile()`) |
| `stale` | boolean | false | True if any dependency has changed since last refresh (derivable) |

---

## Assumes: Non-Derivable Intent

The `assumes` field captures human-supplied decision assumptions. **Never auto-generated; never touched by `reconcile()`.** Used to emit rebuild contracts when decisions flip.

### Structure

```yaml
assumes:
  <decision_node>:
    option: <string>              # Assumed option (e.g., "optA")
    why: <string>                 # Reason for assumption (human-written)
    surfaces: [<file>, ...]       # Files affected by this assumption
    on_flip: "rebuild|rebase|manual"  # Strategy when assumption flips
```

### Example

```yaml
a2:
  assumes:
    decision_b:
      option: "optA"
      why: "Auth service handles tokens; optB requires token-to-timestamp mapping"
      surfaces: ["src/auth.py", "tests/auth_test.py"]
      on_flip: "rebuild"          # optB flip requires rework (semantic change)
```

---

## Derivable vs. Non-Derivable Fields

**Derivable** (recomputed by `reconcile()` and operations):
- `range.base_sha`, `range.tip_sha` — computed from live git refs
- `present.stale`, `present.source_tip_sha` — derived from ghost tip
- `deps_sha` (artifacts) — snapshot of live dependency SHAs
- `stale` (artifacts) — true if any `deps[*]` SHA changed

**Non-Derivable** (human-supplied, never auto-modified):
- `assumes` — decision assumptions
- `present.plan` — logical commit grouping (user-curated, explicit)
- `desc` — human label

---

## Validation Rules

1. **No cycles:** Edges (via `base` or `deps`) must not form cycles.
2. **Decision nodes:** Must have `options` dict with at least one option.
3. **Artifact nodes:** Must have `deps` list.
4. **Feature stacking:** Root nodes (no `base`) form topological roots.
5. **Range ownership:** `range.tip_sha` is fleet-owned; user must never directly edit.
6. **Assumes protection:** `assumes` fields must not be auto-generated by fleet.
7. **Plan pinning:** `present.plan` must be explicit user input; never silently rederived.

---

## Example Manifest

```yaml
feature: payments
canonical: main
merge_mode: individual
max_worktrees: 6

nodes:
  a1:
    kind: feature
    desc: "Auth service integration"
    base: null          # Root node
    ghost: "ghost/pay/a1"
    range:
      base_sha: "abc123..."
      tip_sha: "def456..."
    status: in-review
    present:
      branch: "present/pay/a1"
      pr: 123
      stale: false
      source_tip_sha: "def456..."
      plan:
        - name: "auth-service"
          sha_range: "abc123..def456"
    assumes: {}

  a2:
    kind: feature
    desc: "Token refresh endpoint"
    base: a1            # Stacked on a1
    ghost: "ghost/pay/a2"
    range:
      base_sha: "def456..."
      tip_sha: "ghi789..."
    status: draft
    worktree: "wt-1"
    assumes:
      decision_b:
        option: "optA"
        why: "Auth service provides token validation"
        surfaces: ["src/token.py"]
        on_flip: "rebuild"

  decision_b:
    kind: decision
    desc: "How to validate tokens?"
    base: null
    promoted: null
    options:
      optA:
        ghost: "ghost/pay/b1-A"
        size: small
        spec_only: false
      optB:
        ghost: null
        size: big
        spec_only: true
    status: draft

  c1:
    kind: artifact
    desc: "E2E test suite"
    deps: ["a1", "a2"]
    deps_sha:
      a1: "def456..."
      a2: "ghi789..."
    stale: false
    status: draft
```

---

## Notes

- Manifest is serialized to YAML with `default_flow_style: false` (block style for readability).
- All writes are wrapped in WAL intent/commit records for crash recovery.
- Atomic writes via temp file + rename to prevent partial updates.
- Schema validation occurs on every read and write.
