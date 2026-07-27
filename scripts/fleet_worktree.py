#!/usr/bin/env python3
"""
Worktree pool management — allocation, eviction, parking.

Implements bounded pool with LRU idle-node eviction and dirty-worktree blocking.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError


class WorktreeError(Exception):
    """Base exception for worktree errors."""
    pass


class WorktreeDirtyError(WorktreeError):
    """Worktree has uncommitted changes; cannot operate."""
    pass


class WorktreePoolFullError(WorktreeError):
    """Worktree pool full; no evictable worktrees."""
    pass


def worktree_alloc(feature: str, node: Dict) -> str:
    """
    Allocate a worktree for a node from the bounded pool.

    If pool is full, attempts to evict LRU idle worktree (if clean).
    Checks dirty-worktree blocking (raises WorktreeDirtyError if dirty).

    Args:
        feature: Feature name
        node: Node dict with 'name' and 'ghost' fields

    Returns:
        Worktree path (e.g., ~/.fleet/feature/wt/wt-1)

    Raises:
        WorktreeDirtyError: Cannot evict; all worktrees dirty or in use
        WorktreePoolFullError: Pool full; no LRU evictable
        WorktreeError: Worktree allocation failed
    """
    try:
        manifest = manifest_read(feature)
    except Exception as e:
        raise WorktreeError(f"Failed to read manifest: {e}")

    feature_path = os.path.expanduser(f"~/.fleet/{feature}")
    pool_root = os.path.join(feature_path, "wt")
    node_name = node.get("name")
    ghost_branch = node.get("ghost")

    if not node_name or not ghost_branch:
        raise WorktreeError(f"Invalid node: missing name or ghost")

    # Ensure pool root exists
    os.makedirs(pool_root, exist_ok=True)

    # Check pool capacity
    max_worktrees = manifest.get("max_worktrees", 6)
    active_wts = _count_active_worktrees(pool_root)

    if active_wts >= max_worktrees:
        # Find LRU idle worktree to evict
        idle_wts = _find_idle_worktrees(feature, pool_root)

        if idle_wts:
            # Sort by last-access time (oldest first)
            lru_wt_path = min(idle_wts, key=lambda p: _last_access_time(p))

            # Check if dirty before evicting
            if worktree_is_dirty(lru_wt_path):
                raise WorktreeDirtyError(
                    f"FLEET_040: Cannot evict worktree {lru_wt_path}; has uncommitted changes"
                )

            # Evict (remove worktree and branch reference)
            try:
                subprocess.run(
                    ["git", "worktree", "remove", lru_wt_path],
                    check=True,
                    capture_output=True,
                    timeout=10
                )
            except subprocess.CalledProcessError as e:
                raise WorktreeError(f"Failed to remove worktree {lru_wt_path}: {e}")
        else:
            raise WorktreePoolFullError(
                f"FLEET_041: Pool full ({max_worktrees} worktrees); no idle worktree to evict"
            )

    # Allocate new worktree
    wt_id = _next_available_id(pool_root)
    wt_path = os.path.join(pool_root, f"wt-{wt_id}")

    try:
        subprocess.run(
            ["git", "worktree", "add", wt_path, ghost_branch],
            check=True,
            capture_output=True,
            timeout=10
        )
    except subprocess.CalledProcessError as e:
        raise WorktreeError(f"Failed to add worktree {wt_path}: {e}")

    # Update manifest
    node["worktree"] = f"wt-{wt_id}"
    try:
        manifest_write_atomic(feature, manifest)
    except ManifestError as e:
        # Cleanup worktree on manifest write failure
        try:
            subprocess.run(
                ["git", "worktree", "remove", wt_path],
                timeout=10
            )
        except Exception:
            pass
        raise WorktreeError(f"Failed to update manifest: {e}")

    return wt_path


def worktree_detach(feature: str, wt_id: str) -> None:
    """
    Park a worktree (detach HEAD, but keep branch ref).

    Blocks if worktree has uncommitted changes (WorktreeDirtyError).

    Args:
        feature: Feature name
        wt_id: Worktree ID (e.g., "wt-1")

    Raises:
        WorktreeDirtyError: Worktree has uncommitted changes
        WorktreeError: Detach failed
    """
    feature_path = os.path.expanduser(f"~/.fleet/{feature}")
    pool_root = os.path.join(feature_path, "wt")
    wt_path = os.path.join(pool_root, wt_id)

    if not os.path.exists(wt_path):
        raise WorktreeError(f"Worktree not found: {wt_path}")

    # Check for uncommitted changes
    if worktree_is_dirty(wt_path):
        raise WorktreeDirtyError(
            f"FLEET_040: Worktree {wt_path} has uncommitted changes; cannot detach"
        )

    # Detach HEAD
    try:
        subprocess.run(
            ["git", "-C", wt_path, "switch", "--detach", "HEAD"],
            check=True,
            capture_output=True,
            timeout=5
        )
    except subprocess.CalledProcessError as e:
        raise WorktreeError(f"Failed to detach worktree {wt_path}: {e}")


def worktree_is_dirty(wt_path: str) -> bool:
    """
    Check if a worktree has uncommitted changes.

    Args:
        wt_path: Path to worktree

    Returns:
        True if worktree has uncommitted changes, False otherwise
    """
    try:
        result = subprocess.run(
            ["git", "-C", wt_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5
        )
        # Non-empty porcelain output means dirty
        return len(result.stdout.strip()) > 0
    except Exception:
        # If status check fails, assume clean (conservative)
        return False


def _count_active_worktrees(pool_root: str) -> int:
    """
    Count active (non-detached) worktrees in pool.

    Args:
        pool_root: Worktree pool root directory

    Returns:
        Count of active worktrees
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list"],
            capture_output=True,
            text=True,
            timeout=5
        )
        # Parse output; count non-detached entries
        # Format: /path/to/wt SHA (branch/ref)
        # Detached: /path/to/wt SHA (detached)
        count = 0
        for line in result.stdout.strip().split("\n"):
            if not line or "(detached)" in line:
                continue
            if pool_root in line:
                count += 1
        return count
    except Exception:
        return 0


def _find_idle_worktrees(feature: str, pool_root: str) -> List[str]:
    """
    Find idle (draft status, not in-review/merged) worktrees in pool.

    Heuristic: nodes with status == 'draft' are considered idle.

    Args:
        feature: Feature name
        pool_root: Worktree pool root directory

    Returns:
        List of idle worktree paths
    """
    try:
        manifest = manifest_read(feature)
    except Exception:
        return []

    idle_wts = []
    nodes = manifest.get("nodes", {})

    for node_name, node in nodes.items():
        if not isinstance(node, dict):
            continue

        # Idle heuristic: status == 'draft'
        if node.get("status") != "draft":
            continue

        wt_id = node.get("worktree")
        if not wt_id:
            continue

        wt_path = os.path.join(pool_root, wt_id)
        if os.path.exists(wt_path):
            idle_wts.append(wt_path)

    return idle_wts


def _last_access_time(wt_path: str) -> float:
    """
    Get last-access time of a worktree directory.

    Args:
        wt_path: Path to worktree

    Returns:
        Timestamp (seconds since epoch)
    """
    try:
        stat_info = os.stat(wt_path)
        return stat_info.st_atime
    except Exception:
        return 0


def _next_available_id(pool_root: str) -> int:
    """
    Find next available worktree ID.

    Scans existing wt-N directories; returns max(N) + 1.

    Args:
        pool_root: Worktree pool root directory

    Returns:
        Next available ID (e.g., 1, 2, 3, ...)
    """
    try:
        existing = []
        for entry in os.listdir(pool_root):
            if entry.startswith("wt-"):
                try:
                    num = int(entry.split("-")[1])
                    existing.append(num)
                except (IndexError, ValueError):
                    pass
        return max(existing) + 1 if existing else 1
    except Exception:
        return 1


if __name__ == "__main__":
    # Simple test
    import sys

    if len(sys.argv) > 2:
        feature_name = sys.argv[1]
        command = sys.argv[2]

        try:
            if command == "is-dirty":
                wt_path = sys.argv[3]
                is_dirty = worktree_is_dirty(wt_path)
                print(f"✓ Worktree is {'dirty' if is_dirty else 'clean'}")
            else:
                print(f"✗ Unknown command: {command}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
