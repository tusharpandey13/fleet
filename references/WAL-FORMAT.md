# WAL Format Reference

**Location:** `~/.fleet/{feature}/wal.jsonl`

**Purpose:** Append-only write-ahead log for crash recovery. Records operation intents (pre-state) and commits (post-state) to enable idempotent replay or rollback on startup.

---

## Record Structure

Each line is a JSON object:

```json
{
  "timestamp": "2026-07-27T14:32:15Z",
  "op_type": "<operation_type>",
  "status": "intent|commit",
  "payload": { ... operation-specific data ... }
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 UTC timestamp (e.g., "2026-07-27T14:32:15Z") |
| `op_type` | string | Operation type (cascade, merge-back-intent, present-gen-intent, etc.) |
| `status` | enum | "intent" (pre-op snapshot) or "commit" (post-op completion marker) |
| `payload` | object | Operation-specific data (see below) |

---

## Operation Payloads

### Cascade Intent

Logged before cascading rebase of descendants.

```json
{
  "op_type": "cascade",
  "status": "intent",
  "payload": {
    "feature": "payments",
    "root": "a1",
    "descendants": ["a2", "a3"],
    "pre_image_refs": {
      "ghost/pay/a2": "abc123...",
      "ghost/pay/a3": "def456..."
    },
    "post_expected_refs": {
      "ghost/pay/a2": "ghi789...",
      "ghost/pay/a3": "jkl012..."
    }
  }
}
```

**Fields:**
- `feature`: Feature name
- `root`: Root node being rebased
- `descendants`: List of descendant nodes to re-parent (topologically sorted)
- `pre_image_refs`: Pre-rebase SHAs (all branches in range)
- `post_expected_refs`: Expected post-rebase SHAs (for idempotency check)

### Cascade Commit

Logged after cascading rebase succeeds.

```json
{
  "op_type": "cascade",
  "status": "commit",
  "payload": {
    "feature": "payments",
    "status": "success"
  }
}
```

### Merge-Back-Intent

Logged before squash cleanup via `--onto main`.

```json
{
  "op_type": "merge-back-intent",
  "status": "intent",
  "payload": {
    "feature": "payments",
    "node": "a1",
    "merge_mode": "individual",
    "pre_image_refs": {
      "ghost/pay/a2": "abc123...",
      "ghost/pay/a3": "def456..."
    },
    "target_main_sha": "main_tip_abc...",
    "post_expected_refs": {
      "ghost/pay/a2": "new_a2_sha...",
      "ghost/pay/a3": "new_a3_sha..."
    }
  }
}
```

**Fields:**
- `feature`: Feature name
- `node`: Node being merged
- `merge_mode`: "individual" (staggered) or "batch"
- `pre_image_refs`: Pre-cleanup children SHAs
- `target_main_sha`: Main branch tip at merge time (for idempotency)
- `post_expected_refs`: Expected children SHAs after --onto

### Merge-Back-Intent Commit

```json
{
  "op_type": "merge-back-intent",
  "status": "commit",
  "payload": {
    "feature": "payments",
    "status": "success"
  }
}
```

### Present-Gen-Intent

Logged before presentation generation.

```json
{
  "op_type": "present-gen-intent",
  "status": "intent",
  "payload": {
    "feature": "payments",
    "node": "a1",
    "plan": [
      {
        "name": "docs",
        "sha_range": "abc123..def456"
      },
      {
        "name": "impl",
        "sha_range": "def456..ghi789"
      }
    ],
    "source_ghost_tip_sha": "ghi789...",
    "expected_presentation_tree_sha": "tree_hash_xyz..."
  }
}
```

**Fields:**
- `feature`: Feature name
- `node`: Node being published
- `plan`: Pinned logical commit grouping
- `source_ghost_tip_sha`: Ghost tip at generation time (for staleness check)
- `expected_presentation_tree_sha`: Tree hash of final presentation (for idempotency)

### Present-Gen-Intent Commit

```json
{
  "op_type": "present-gen-intent",
  "status": "commit",
  "payload": {
    "feature": "payments",
    "status": "success"
  }
}
```

### WAL Recovery (Cleanup Record)

Logged after replay or rollback to acknowledge handling.

```json
{
  "op_type": "wal-recovery",
  "status": "commit",
  "payload": {
    "original_op": "cascade",
    "action": "replay",
    "timestamp": "2026-07-27T14:35:10Z"
  }
}
```

---

## Replay Algorithm

On startup, fleet scans the WAL for incomplete entries (intent without matching commit).

### Pseudocode

```
for each line in wal.jsonl:
  record = json.loads(line)
  
  if record.status == 'intent':
    intent_records[record.op_type] = record
  elif record.status == 'commit':
    # Pair complete; remove from incomplete map
    del intent_records[record.op_type]

# Remaining unpaired intents are incomplete
incomplete = list(intent_records.values())

for entry in incomplete:
  print(f"Incomplete: {entry['op_type']}")
  action = prompt_user("Replay or Rollback? [r/b]: ")
  
  if action == 'r':  # Replay
    replay_operation(entry)  # Idempotent re-execution
  elif action == 'b':  # Rollback
    for (ref, sha) in entry['payload']['pre_image_refs'].items():
      git update-ref <ref> <sha>
    reconcile(entry['payload']['feature'])
  
  # Mark as handled
  wal_write('wal-recovery', {
    original_op: entry['op_type'],
    action: action
  })
```

### Idempotence

Each operation (cascade, merge-back, present-gen) is idempotent. Replay checks if operation already applied:

- **Cascade:** Verify descendants already rebased (compare post-rebase parent SHAs). Skip if applied.
- **Merge-back:** Verify children already re-parented onto main. Skip if applied.
- **Present-gen:** Verify presentation tree matches expected. Skip if applied; regenerate if stale.

---

## Example WAL File

```jsonl
{"timestamp": "2026-07-27T14:32:15Z", "op_type": "cascade", "status": "intent", "payload": {"feature": "payments", "root": "a1", "descendants": ["a2"], "pre_image_refs": {"ghost/pay/a2": "abc123..."}, "post_expected_refs": {"ghost/pay/a2": "ghi789..."}}}
{"timestamp": "2026-07-27T14:32:20Z", "op_type": "cascade", "status": "commit", "payload": {"feature": "payments", "status": "success"}}
{"timestamp": "2026-07-27T14:35:10Z", "op_type": "wal-recovery", "status": "commit", "payload": {"original_op": "cascade", "action": "replay", "timestamp": "2026-07-27T14:35:10Z"}}
```

---

## Notes

- **Append-only:** New records appended to end; never truncated mid-operation.
- **Immediate flush:** Each write includes `f.flush()` and `os.fsync()` for durability.
- **Lock-protected:** All WAL writes occur under process-level lock (single-active-operation invariant).
- **Corruption handling:** Corrupted JSON lines are skipped during replay with warning logged.
- **Recovery prompt:** On incomplete entry, user chooses replay (resume) or rollback (undo).
