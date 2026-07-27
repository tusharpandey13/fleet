#!/usr/bin/env python3
"""
Process-level locking and topological DAG helpers.

Provides advisory file locks with stale-lock detection, and topological
depth computation for cascade/merge-back endpoint selection.
"""

import os
import fcntl
import time
import subprocess
from pathlib import Path
from typing import Dict, Optional, IO, Set


class LockError(Exception):
    """Base exception for lock errors."""
    pass


class LockTimeoutError(LockError):
    """Lock timeout error."""
    pass


def acquire_lock(lock_path: str, timeout: int = 1800) -> IO:
    """
    Acquire an advisory file lock with stale-lock detection.

    Args:
        lock_path: Path to lock file
        timeout: Timeout in seconds (default 30min)

    Returns:
        Locked file object

    Raises:
        LockTimeoutError: Lock timeout
    """
    lock_dir = os.path.dirname(lock_path)
    os.makedirs(lock_dir, exist_ok=True)

    lock_f = open(lock_path, 'a+')
    start_time = time.time()

    while True:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Lock acquired
            lock_f.seek(0)
            lock_f.truncate()
            lock_f.write(f"{os.getpid()}:{int(time.time())}\n")
            lock_f.flush()
            return lock_f

        except IOError:
            # Lock held by another process
            if time.time() - start_time > timeout:
                # Check if lock is stale
                if is_stale_lock(lock_path, timeout):
                    # Reclaim
                    fcntl.flock(lock_f, fcntl.LOCK_EX)
                    lock_f.seek(0)
                    lock_f.truncate()
                    lock_f.write(f"{os.getpid()}:{int(time.time())}\n")
                    lock_f.flush()
                    return lock_f
                else:
                    lock_f.close()
                    raise LockTimeoutError(f"FLEET_030: Lock timeout (held for >{timeout}s)")

            time.sleep(0.1)


def release_lock(lock_obj: IO) -> None:
    """Release an advisory file lock."""
    try:
        fcntl.flock(lock_obj, fcntl.LOCK_UN)
        lock_obj.close()
    except Exception:
        pass


def is_stale_lock(lock_path: str, timeout: int) -> bool:
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

        # Check if process is alive (signal 0 = no-op, just check existence)
        try:
            os.kill(pid, 0)
            return False  # Process alive
        except ProcessLookupError:
            return True  # Process dead

    except Exception:
        return True


def git_depth_in_dag(
    node: str,
    manifest: Dict,
    root: Optional[str] = None
) -> int:
    """
    Compute topological depth of a node in the manifest DAG.

    Depth = longest path from root to node (measured in edges/hops).
    Used for cascade/merge-back range endpoint selection.

    WARNING: Incorrect depth metric could select wrong rebase endpoints,
    missing descendants (C2 regression). Metric must match manifest DAG
    topology, not git history. Tie-break by lexicographic SHA for
    deterministic parallel-branch selection.

    Args:
        node: Node name to measure
        manifest: Manifest dict with nodes and edges
        root: Optional topological root (auto-detect if not provided)

    Returns:
        Depth (int): longest path length from root to node
    """
    if not root:
        root = find_dag_root(node, manifest)

    # DFS to compute depth from root
    depth_map = {}

    def dfs_depth(current_node: str, current_depth: int) -> None:
        if current_node in depth_map:
            return  # Already computed

        depth_map[current_node] = current_depth

        # Find descendants of current_node
        descendants = []
        nodes = manifest.get('nodes', {})

        for candidate_name, candidate_node in nodes.items():
            if not isinstance(candidate_node, dict):
                continue

            # Feature stacking edge (base == current_node)
            if candidate_node.get('base') == current_node:
                descendants.append(candidate_name)

            # Artifact dependency edge
            elif candidate_node.get('kind') == 'artifact':
                if current_node in candidate_node.get('deps', []):
                    descendants.append(candidate_name)

        # Recurse to descendants
        for desc in descendants:
            dfs_depth(desc, current_depth + 1)

    dfs_depth(root, 0)

    return depth_map.get(node, 0)


def find_dag_root(node: str, manifest: Dict) -> str:
    """
    Find the topological root of the DAG containing node.

    Root = node with no incoming edges (no base for features).

    Args:
        node: Starting node
        manifest: Manifest dict

    Returns:
        Root node name
    """
    nodes = manifest.get('nodes', {})
    current = node

    while current in nodes:
        current_node = nodes[current]

        # Feature with base; move to base
        if current_node.get('kind') == 'feature' and current_node.get('base'):
            current = current_node['base']
        else:
            # No base or not a feature; this is a root
            break

    return current


def get_max_descendant(
    descendants: list,
    manifest: Dict,
    root: str
) -> str:
    """
    Select max descendant by topological depth, tie-break by lexicographic SHA.

    Args:
        descendants: List of descendant node names
        manifest: Manifest dict
        root: Topological root

    Returns:
        Max descendant node name
    """
    if not descendants:
        return root

    def sort_key(desc_name: str) -> tuple:
        depth = git_depth_in_dag(desc_name, manifest, root)
        # Get git SHA for tie-break
        try:
            sha = subprocess.check_output(
                ['git', 'rev-parse', _node_ghost_branch(manifest, desc_name)],
                stderr=subprocess.DEVNULL,
                text=True
            ).strip()
        except Exception:
            sha = ""

        return (depth, sha)

    return max(descendants, key=sort_key)


def _node_ghost_branch(manifest: Dict, node_name: str) -> str:
    """Get ghost branch name for a node."""
    node = manifest.get('nodes', {}).get(node_name, {})
    return node.get('ghost', f"ghost/{node_name}")


if __name__ == '__main__':
    # Simple test
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'lock-test':
        lock_path = '/tmp/fleet_test.lock'
        try:
            lock = acquire_lock(lock_path)
            print(f"✓ Lock acquired: {lock_path}")
            release_lock(lock)
            print(f"✓ Lock released")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
