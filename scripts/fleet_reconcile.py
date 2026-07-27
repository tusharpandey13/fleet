#!/usr/bin/env python3
"""
Reconciliation — recompute derivable fields from git refs.

Detects unobserved writes, DAG divergence, and staleness indicators.
Updates manifest via WAL intent/commit pairs under lock.
"""

import os
import subprocess
from typing import Dict, List, Tuple, Optional, IO
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError
from fleet_wal import wal_write, WALError
from fleet_lock import acquire_lock, release_lock, LockTimeoutError


class ReconcileError(Exception):
    """Base exception for reconciliation errors."""
    pass


def reconcile(
    feature: str,
    manifest: Optional[Dict] = None,
    lock_obj: Optional[IO] = None
) -> Tuple[Dict, List[str], List[Dict]]:
    """
    Reconcile manifest with live git state.

    Recomputes derivable fields (range.tip_sha, range.base_sha, staleness flags).
    Detects unobserved writes and DAG divergences. Wraps manifest write in WAL.

    Args:
        feature: Feature name
        manifest: Optional pre-loaded manifest (if None, read from disk)
        lock_obj: Optional lock object (if provided, use it; else acquire internally)

    Returns:
        Tuple of (updated_manifest, unobserved_writes_list, divergences_list)
        where:
          - unobserved_writes: list of node names with moved tips
          - divergences: list of dicts with {node, reason, git_parent, manifest_base}

    Raises:
        ReconcileError: Reconciliation failed
        ManifestError: Manifest read/write failed
    """
    # Read manifest if not provided
    if manifest is None:
        try:
            manifest = manifest_read(feature)
        except Exception as e:
            raise ReconcileError(f"Failed to read manifest: {e}")

    unobserved_writes = []
    divergences = []

    # Acquire lock if not provided (caller responsibility in normal operation)
    if lock_obj is None:
        feature_path = os.path.expanduser(f"~/.fleet/{feature}")
        lock_path = os.path.join(feature_path, ".fleet.lock")
        try:
            lock_obj = acquire_lock(lock_path)
            should_release = True
        except LockTimeoutError as e:
            raise ReconcileError(f"Failed to acquire lock: {e}")
    else:
        should_release = False

    try:
        # Log WAL intent
        try:
            wal_write(
                feature,
                "reconcile",
                "intent",
                {"feature": feature},
                lock_obj=lock_obj
            )
        except WALError as e:
            raise ReconcileError(f"Failed to write WAL intent: {e}")

        # Step 1: Recompute range.tip_sha and range.base_sha for each node
        nodes = manifest.get("nodes", {})
        for node_name, node in nodes.items():
            if not isinstance(node, dict):
                continue

            # Skip if not materialized
            ghost_branch = node.get("ghost")
            if not ghost_branch:
                continue

            try:
                # Get live tip SHA
                live_tip_sha = _git_rev_parse(ghost_branch)
            except Exception as e:
                # Ghost branch not found; mark as unobserved (moved or deleted)
                unobserved_writes.append(node_name)
                continue

            # Detect unobserved write
            cached_tip_sha = node.get("range", {}).get("tip_sha")
            if live_tip_sha != cached_tip_sha:
                unobserved_writes.append(node_name)

            # Update cached tip
            if "range" not in node:
                node["range"] = {}
            node["range"]["tip_sha"] = live_tip_sha

            # Compute live base (merge-base with base_branch)
            base_branch = node.get("base")
            if base_branch:
                # For stacked features, base_branch is another node name; resolve to its ghost
                base_node = nodes.get(base_branch, {})
                base_ghost = base_node.get("ghost")
                if base_ghost:
                    try:
                        live_base_sha = _git_merge_base(ghost_branch, base_ghost)
                    except Exception:
                        live_base_sha = None
                else:
                    live_base_sha = None
            else:
                # Root node; base is main
                try:
                    live_base_sha = _git_merge_base(ghost_branch, "main")
                except Exception:
                    live_base_sha = None

            node["range"]["base_sha"] = live_base_sha

            # Step 2: Check effective base (DAG consistency)
            # Get parent commit of ghost tip: ~1 is parent commit
            try:
                git_parent_sha = _git_rev_parse(f"{ghost_branch}~1")
            except Exception:
                git_parent_sha = None

            # For divergence check: if base_branch is another node, resolve to its tip
            if base_branch and base_branch in nodes:
                base_node = nodes.get(base_branch, {})
                manifest_base_sha = base_node.get("range", {}).get("tip_sha")
            else:
                # Root node; base is main
                manifest_base_sha = node["range"]["base_sha"]

            if git_parent_sha and manifest_base_sha and git_parent_sha != manifest_base_sha:
                divergences.append({
                    "node": node_name,
                    "reason": "git parent ≠ manifest base",
                    "git_parent": git_parent_sha,
                    "manifest_base": manifest_base_sha
                })

        # Step 3: Recompute artifact staleness
        for node_name, node in nodes.items():
            if not isinstance(node, dict):
                continue

            if node.get("kind") != "artifact":
                continue

            # Check if any dep has changed
            stale = False
            for dep in node.get("deps", []):
                live_dep_sha = nodes.get(dep, {}).get("range", {}).get("tip_sha")
                cached_dep_sha = node.get("deps_sha", {}).get(dep)
                if live_dep_sha and live_dep_sha != cached_dep_sha:
                    stale = True
                    break

            node["stale"] = stale

            # Update deps_sha snapshot
            deps_sha = {}
            for dep in node.get("deps", []):
                dep_sha = nodes.get(dep, {}).get("range", {}).get("tip_sha")
                if dep_sha:
                    deps_sha[dep] = dep_sha
            node["deps_sha"] = deps_sha

        # Step 4: Recompute presentation staleness
        for node_name, node in nodes.items():
            if not isinstance(node, dict):
                continue

            present = node.get("present")
            if not present:
                continue

            source_tip_sha = present.get("source_tip_sha")
            live_tip_sha = node.get("range", {}).get("tip_sha")

            if source_tip_sha and live_tip_sha and source_tip_sha != live_tip_sha:
                present["stale"] = True
            else:
                present["stale"] = False

        # Step 5: Write manifest via WAL
        try:
            manifest_write_atomic(feature, manifest)
        except ManifestError as e:
            raise ReconcileError(f"Failed to write manifest: {e}")

        # Log WAL commit
        try:
            wal_write(
                feature,
                "reconcile",
                "commit",
                {
                    "feature": feature,
                    "unobserved_writes": unobserved_writes,
                    "divergences": divergences
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise ReconcileError(f"Failed to write WAL commit: {e}")

        return (manifest, unobserved_writes, divergences)

    finally:
        if should_release and lock_obj:
            release_lock(lock_obj)


def _git_rev_parse(ref: str) -> str:
    """
    Get SHA for a git ref.

    Args:
        ref: Git reference (branch, tag, SHA)

    Returns:
        SHA string

    Raises:
        Exception: Ref not found or git error
    """
    result = subprocess.run(
        ["git", "rev-parse", ref],
        capture_output=True,
        text=True,
        timeout=5
    )
    if result.returncode != 0:
        raise Exception(f"git rev-parse {ref} failed: {result.stderr}")
    return result.stdout.strip()


def _git_merge_base(ref1: str, ref2: str) -> str:
    """
    Get merge-base between two git refs.

    Args:
        ref1: First git reference
        ref2: Second git reference

    Returns:
        Merge-base SHA

    Raises:
        Exception: Merge-base not found or git error
    """
    result = subprocess.run(
        ["git", "merge-base", ref1, ref2],
        capture_output=True,
        text=True,
        timeout=5
    )
    if result.returncode != 0:
        raise Exception(f"git merge-base {ref1} {ref2} failed: {result.stderr}")
    return result.stdout.strip()


if __name__ == "__main__":
    # Simple test
    import sys

    if len(sys.argv) > 1:
        feature_name = sys.argv[1]
        try:
            manifest, unobserved, divergences = reconcile(feature_name)
            print(f"✓ Reconcile complete for '{feature_name}'")
            if unobserved:
                print(f"  Unobserved writes: {unobserved}")
            if divergences:
                print(f"  Divergences: {divergences}")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
