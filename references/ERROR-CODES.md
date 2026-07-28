# Fleet Error Codes Reference

**Purpose:** Centralized registry of fleet error codes (FLEET_NNN). Each code documents the error condition, recovery steps, and associated tests.

---

## Error Code Registry

### Manifest & Schema (FLEET_001–FLEET_009)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_001** | Manifest not found; fleet not initialized for feature | Run `fleet init <feature>` to initialize fleet for this feature | T-R1 |
| **FLEET_002** | DAG has cycle; manifest edges form a loop | Realign manifest or git refs to fix cycle; reconcile and retry | T-R2, T-C1 |
| **FLEET_003** | Schema validation failed; manifest corrupted or invalid | Inspect manifest YAML; fix schema violations or restore from backup | T-R1, T-R5 |

### Operations & Git (FLEET_010–FLEET_029)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_010** | Merge conflict during cascade | Resolve conflict manually in worktree; `git rebase --continue`; retry cascade | T-C4 |
| **FLEET_011** | Merge conflict during cleanup (merge-back --onto) | Resolve conflict manually; retry merge-back | T-M2 |
| **FLEET_020** | Force-push lease failed; remote tip changed (reviewer pushed) | Fetch remote; decide: (a) incorporate reviewer commits, (b) discard, (c) manual | T-P5, T-E1 |

### Locking (FLEET_030–FLEET_039)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_030** | Lock timeout; another process holding lock >30min | Wait for other process to finish, or kill stale holder and retry | T-W2 |

### Worktree Pool (FLEET_040–FLEET_049)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_040** | Worktree has uncommitted changes; cannot park/evict | Commit or stash changes in worktree; retry operation | T-C6 |
| **FLEET_041** | Worktree pool full; no LRU evictable worktree | Commit changes in an idle worktree to free capacity, or increase `max_worktrees` | T-E4 |

### Merge Operations (FLEET_050–FLEET_059)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_050** | Merge-back out of topological order; blocked | Merge nodes in topological order; run `fleet status` to see DAG | T-M3 |

### Node & DAG (FLEET_060–FLEET_069)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_060** | Node not found or invalid kind for operation | Verify node name is correct; check manifest nodes | Implicit (unit tests) |

### Experimental (FLEET_099)

| Code | Description | Recovery | Tests |
|------|-------------|----------|-------|
| **FLEET_099** | Experimental feature not enabled; `fleet resolve` (decision-variant resolution) requires explicit opt-in | Enable either (1) `export FLEET_EXPERIMENTAL=1` before running `fleet resolve`, OR (2) set `experimental: true` in the manifest (top-level field). Then retry the command. | T-Res-2 |

---

## Error Handling Patterns

### Manifest Not Found (FLEET_001)

**When:**
- User runs fleet command for a feature without initialized manifest

**Handling:**
```python
try:
    manifest = manifest_read(feature)
except FileNotFoundError:
    print("FLEET_001: Manifest not found for feature '{feature}'")
    print("Initialize with: fleet init {feature}")
    sys.exit(1)
```

### Schema Validation (FLEET_003)

**When:**
- Manifest read/write fails schema validation

**Handling:**
```python
errors = manifest_validate(manifest)
if errors:
    raise SchemaError(f"FLEET_003: {'; '.join(errors)}")
```

### Merge Conflict (FLEET_010, FLEET_011)

**When:**
- Git rebase hits conflict during cascade or merge-back

**Handling:**
```python
try:
    git rebase --update-refs ...
except ConflictError:
    node.status = 'blocked'
    print("FLEET_010: Merge conflict detected")
    print("Resolve in worktree; git rebase --continue; retry cascade")
    return {error: "Conflict; node marked blocked"}
```

### Force-Push Lease Fail (FLEET_020)

**When:**
- `git push --force-with-lease` fails because remote tip changed

**Handling:**
```python
try:
    git push --force-with-lease origin <branch>
except LeaseFailError:
    print("FLEET_020: Lease failed (reviewer pushed)")
    choice = prompt_user("Choices: (a) incorporate, (b) discard, (c) manual: ")
    if choice == 'a':
        git merge origin/<branch>
        present_gen(feature, node)
        git push --force-with-lease origin <branch>
    elif choice == 'b':
        git push --force origin <branch>
    else:
        print("Manual resolution required on GitHub")
```

### Lock Timeout (FLEET_030)

**When:**
- Lock held >30min (stale lock)

**Handling:**
```python
try:
    lock = acquire_lock(lock_path, timeout=1800)
except LockTimeoutError:
    print("FLEET_030: Lock timeout")
    print("Another process may be stalled. Kill it or wait.")
    sys.exit(1)
```

### Dirty Worktree (FLEET_040)

**When:**
- Cascade needs to park worktree, but it has uncommitted changes

**Handling:**
```python
if worktree_is_dirty(wt_path):
    print("FLEET_040: Worktree has uncommitted changes")
    print("Commit or stash in:", wt_path)
    return {error: "Cannot park dirty worktree"}
```

### Out-of-Order Merge (FLEET_050)

**When:**
- User merges a2 before a1 (violates topological order)

**Handling:**
```python
prior_nodes = topo_ancestors(node)
if any(n.status != 'archived' for n in prior_nodes):
    print("FLEET_050: Out-of-order merge blocked")
    print("Merge nodes in topological order:", prior_nodes)
    return {error: "Cannot merge before dependencies"}
```

---

## Testing Strategy

Each error code has associated test case(s) in the test matrix:

- **FLEET_001:** T-R1 (manifest not found)
- **FLEET_002:** T-R2 (cycle detection), T-C1 (DAG validation)
- **FLEET_003:** T-R1, T-R5 (schema validation)
- **FLEET_010:** T-C4 (conflict during cascade)
- **FLEET_011:** T-M2 (conflict during merge-back)
- **FLEET_020:** T-P5, T-E1 (lease fail handling)
- **FLEET_030:** T-W2 (lock timeout during WAL recovery)
- **FLEET_040:** T-C6 (dirty worktree during cascade)
- **FLEET_041:** T-E4 (worktree pool full)
- **FLEET_050:** T-M3 (out-of-order merge)
- **FLEET_060:** Implicit (node validation)
- **FLEET_099:** T-Res-2 (experimental gate for `fleet resolve`)

---

## Reserved Ranges

- **FLEET_001–FLEET_009:** Manifest & schema
- **FLEET_010–FLEET_029:** Operations & git
- **FLEET_030–FLEET_039:** Locking
- **FLEET_040–FLEET_049:** Worktree pool
- **FLEET_050–FLEET_059:** Merge operations
- **FLEET_060–FLEET_069:** Node & DAG
- **FLEET_070–FLEET_098:** Reserved for future use
- **FLEET_099:** Experimental-feature gate

---

## Notes

- Every error code is prefixed with `FLEET_NNN` for easy identification.
- Errors are always surfaced to user with code + human-readable message.
- Logging includes error code for debugging and issue tracking.
- Test matrix covers all critical error paths.
