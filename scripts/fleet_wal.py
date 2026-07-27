#!/usr/bin/env python3
"""
Write-Ahead Log (WAL) for fleet operations.

Provides append-only journaling of operation intents/commits, with replay
and rollback support for crash recovery. Lock object passthrough pattern.
"""

import os
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, IO


class WALError(Exception):
    """Base exception for WAL errors."""
    pass


def wal_write(
    feature: str,
    op_type: str,
    status: str,
    payload: Dict,
    lock_obj: Optional[IO] = None
) -> None:
    """
    Append a record to the WAL.

    Args:
        feature: Feature name
        op_type: Operation type (e.g., "cascade", "merge-back-intent")
        status: "intent" or "commit"
        payload: Operation-specific payload dict
        lock_obj: Optional lock object (if provided, skip lock acquire/release)

    Raises:
        WALError: Write failed
    """
    wal_path = _wal_path(feature)

    # Ensure directory exists
    wal_path.parent.mkdir(parents=True, exist_ok=True)

    # If lock_obj provided, use it; otherwise acquire internally (deprecated pattern)
    if lock_obj is None:
        lock_path = wal_path.parent / '.fleet.lock'
        try:
            lock_obj = _acquire_lock_internal(str(lock_path))
            should_release = True
        except Exception as e:
            raise WALError(f"Failed to acquire lock: {e}")
    else:
        should_release = False

    try:
        record = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'op_type': op_type,
            'status': status,
            'payload': payload
        }

        # Atomic append (single line)
        with open(str(wal_path), 'a') as f:
            f.write(json.dumps(record) + '\n')
            f.flush()
            os.fsync(f.fileno())

    finally:
        if should_release and lock_obj:
            _release_lock_internal(lock_obj)


def wal_read_incomplete(feature: str) -> List[Dict]:
    """
    Read incomplete (unpaired) WAL entries.

    Scans WAL for intent records without matching commit records.

    Args:
        feature: Feature name

    Returns:
        List of incomplete intent records
    """
    wal_path = _wal_path(feature)

    if not wal_path.exists():
        return []

    incomplete = []
    intent_map = {}  # op_type -> intent record

    try:
        with open(str(wal_path), 'r') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    record = json.loads(line.strip())
                except json.JSONDecodeError:
                    # Skip corrupted lines
                    continue

                if record.get('status') == 'intent':
                    key = record.get('op_type')
                    intent_map[key] = record
                elif record.get('status') == 'commit':
                    # Pair complete; remove from incomplete map
                    key = record.get('op_type')
                    if key in intent_map:
                        del intent_map[key]

        # Remaining unpaired intents
        incomplete = list(intent_map.values())
    except IOError as e:
        raise WALError(f"Failed to read WAL: {e}")

    return incomplete


def wal_replay(
    feature: str,
    entry: Dict,
    action: str
) -> Dict:
    """
    Replay or rollback a WAL entry.

    For replay: idempotent re-execution of the operation.
    For rollback: restore pre-image SHAs via git update-ref.

    Args:
        feature: Feature name
        entry: WAL intent record to replay/rollback
        action: "replay" or "rollback"

    Returns:
        Dict with {status, reason, ...}
    """
    payload = entry.get('payload', {})

    if action == 'rollback':
        # Restore pre-image SHAs
        pre_image_refs = payload.get('pre_image_refs', {})
        for ref, sha in pre_image_refs.items():
            try:
                os.system(f"git update-ref '{ref}' '{sha}'")
            except Exception as e:
                raise WALError(f"Failed to restore ref {ref}: {e}")

        return {
            'status': 'rolled_back',
            'entry_op': entry.get('op_type')
        }

    elif action == 'replay':
        # Idempotent replay (operation-specific logic handled by caller)
        # This function returns a generic replay indicator; caller dispatches to cascade/merge/present
        return {
            'status': 'ready_for_replay',
            'op_type': entry.get('op_type'),
            'entry': entry
        }

    else:
        raise WALError(f"Invalid action: {action}")


def _wal_path(feature: str) -> Path:
    """Get WAL file path for a feature."""
    return Path(os.path.expanduser(f"~/.fleet/{feature}/wal.jsonl"))


def _acquire_lock_internal(lock_path: str, timeout: int = 1800) -> IO:
    """
    Acquire an advisory file lock.

    Stale-lock detection: if lock file age > timeout or holder PID dead, reclaim.

    Args:
        lock_path: Path to lock file
        timeout: Timeout in seconds (default 30min)

    Returns:
        Locked file object

    Raises:
        WALError: Lock timeout or other error
    """
    import fcntl

    lock_dir = os.path.dirname(lock_path)
    os.makedirs(lock_dir, exist_ok=True)

    lock_f = open(lock_path, 'a+')
    start_time = time.time()

    while True:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Lock acquired
            lock_f.write(f"{os.getpid()}:{int(time.time())}\n")
            lock_f.flush()
            return lock_f

        except IOError:
            # Lock held by another process
            if time.time() - start_time > timeout:
                # Check if lock is stale
                if _is_stale_lock(lock_path, timeout):
                    # Reclaim
                    fcntl.flock(lock_f, fcntl.LOCK_EX)
                    lock_f.truncate(0)
                    lock_f.write(f"{os.getpid()}:{int(time.time())}\n")
                    lock_f.flush()
                    return lock_f
                else:
                    lock_f.close()
                    raise WALError(f"FLEET_030: Lock timeout (held for >{timeout}s)")

            time.sleep(0.1)


def _release_lock_internal(lock_obj: IO) -> None:
    """Release an advisory file lock."""
    import fcntl

    try:
        fcntl.flock(lock_obj, fcntl.LOCK_UN)
        lock_obj.close()
    except Exception:
        pass


def _is_stale_lock(lock_path: str, timeout: int) -> bool:
    """
    Check if a lock file is stale.

    Stale if: (1) lock file age > timeout OR (2) holder process is dead.

    Args:
        lock_path: Path to lock file
        timeout: Timeout in seconds

    Returns:
        True if lock is stale
    """
    try:
        if not os.path.exists(lock_path):
            return True

        with open(lock_path, 'r') as f:
            line = f.read().strip()

        if not line:
            return True

        parts = line.split(':')
        if len(parts) < 2:
            return True

        pid = int(parts[0])
        timestamp_created = int(parts[1])
        current_time = time.time()
        lock_age = current_time - timestamp_created

        # Check lock age
        if lock_age > timeout:
            return True

        # Check if process is alive (kill -0 signal)
        try:
            os.kill(pid, 0)
            return False  # Process alive
        except ProcessLookupError:
            return True  # Process dead

    except Exception:
        return True


if __name__ == '__main__':
    # Simple test
    import sys

    if len(sys.argv) > 1:
        feature_name = sys.argv[1]
        try:
            incomplete = wal_read_incomplete(feature_name)
            if incomplete:
                print(f"✓ Found {len(incomplete)} incomplete entries:")
                for entry in incomplete:
                    print(f"  - {entry['op_type']} at {entry['timestamp']}")
            else:
                print(f"✓ No incomplete entries for '{feature_name}'")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
