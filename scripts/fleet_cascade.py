#!/usr/bin/env python3
"""
Cascade rebase via --update-refs.

Topologically sorts descendants, parks conflicting worktrees, executes cascading
rebase (git rebase --update-refs) over range, handles conflicts, marks artifacts/presentations stale.

Wraps entire operation in lock + WAL intent/commit for crash recovery.
"""

import os
import subprocess
from typing import Dict, List, Optional, IO, Tuple
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError
from fleet_wal import wal_write, WALError
from fleet_lock import acquire_lock, release_lock, git_depth_in_dag, get_max_descendant, LockTimeoutError
from fleet_reconcile import reconcile, ReconcileError
from fleet_worktree import worktree_detach, worktree_is_dirty, WorktreeDirtyError, WorktreeError


class CascadeError(Exception):
    """Base exception for cascade errors."""
    pass


class ConflictError(CascadeError):
    """Merge conflict during cascade rebase."""
    pass


def cascade(
    feature: str,
    root_node: str,
    dry_run: bool = False,
    lock_obj: Optional[IO] = None,
    rebase_base_sha: Optional[str] = None
) -> Dict:
    """
    Cascade descendants via git rebase --update-refs.

    Topologically sorts descendants of root_node, parks affected worktrees,
    executes cascading rebase, handles conflicts, marks staleness, updates manifest.

    Args:
        feature: Feature name
        root_node: Root node name (changed node whose descendants rebase onto it)
        dry_run: If True, print plan without executing
        lock_obj: Optional lock object (if provided, use it; else acquire internally)
        rebase_base_sha: Optional explicit base SHA to rebase descendants onto.
            Overrides root_node.range.base_sha. Required when the root has no
            git base of its own (e.g. a decision node, whose descendants must
            re-parent onto the promoted option's ghost tip rather than the
            decision node itself).

    Returns:
        Dict with {status, rebased, conflicts, parked_worktrees, error?, action?}

    Raises:
        CascadeError: Cascade failed (conflict, dirty worktree, lock timeout, etc.)
    """
    feature_path = os.path.expanduser(f"~/.fleet/{feature}")
    lock_path = os.path.join(feature_path, ".fleet.lock")
    should_release = lock_obj is None

    # Step 1: Read manifest and reconcile
    try:
        manifest = manifest_read(feature)
    except Exception as e:
        raise CascadeError(f"Failed to read manifest: {e}")

    try:
        manifest, unobserved_writes, divergences = reconcile(feature, manifest, lock_obj=lock_obj)
    except ReconcileError as e:
        raise CascadeError(f"Reconciliation failed: {e}")

    # Step 2: Topological sort of descendants
    descendants = topo_sort_descendants(manifest, root_node)

    if divergences:
        # Filter divergences: only abort for those OUTSIDE the cascade root's descendant set.
        # Divergences of descendants are expected (stale-stack condition) and will be fixed by cascade.
        unexpected_divergences = [
            d for d in divergences if d['node'] not in descendants and d['node'] != root_node
        ]
        if unexpected_divergences:
            # Surface unexpected divergences to user; do not auto-fix
            divergence_str = "; ".join([f"{d['node']}: {d['reason']}" for d in unexpected_divergences])
            raise CascadeError(f"DAG divergence detected: {divergence_str}. Manual fix required.")
    if not descendants:
        # No descendants to cascade
        return {
            "status": "success",
            "rebased": [],
            "conflicts": [],
            "parked_worktrees": 0
        }

    # Step 3: Identify parked worktrees
    parked_wts = _find_parked_worktrees(descendants)

    # Step 4: Check for dirty worktrees (STOP if found)
    for wt_path, branch in parked_wts:
        if worktree_is_dirty(wt_path):
            raise CascadeError(
                f"FLEET_040: Worktree {wt_path} has uncommitted changes; "
                f"cannot cascade. Commit or stash changes and retry."
            )

    # Step 5: Dry-run summary
    if dry_run or os.environ.get("DRY_RUN_ALWAYS"):
        dry_summary = _dryrun_cascade(manifest, root_node, descendants, parked_wts)
        print(dry_summary)
        if not dry_run:
            response = input("Proceed? (yes/no): ").strip()
            if response != "yes":
                return {"status": "cancelled"}

    # Step 6: Acquire lock if not provided (wraps entire operation)
    if lock_obj is None:
        try:
            lock_obj = acquire_lock(lock_path)
        except LockTimeoutError as e:
            raise CascadeError(f"Failed to acquire lock: {e}")

    try:
        # Step 7: WAL intent (pre-cascade state)
        pre_image_refs = {}
        for branch in descendants:
            try:
                sha = subprocess.check_output(
                    ["git", "rev-parse", _get_node_ghost(manifest, branch)],
                    stderr=subprocess.DEVNULL,
                    text=True
                ).strip()
                pre_image_refs[branch] = sha
            except Exception:
                pass

        try:
            wal_write(
                feature,
                "cascade",
                "intent",
                {
                    "feature": feature,
                    "root": root_node,
                    "descendants": descendants,
                    "pre_image_refs": pre_image_refs
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise CascadeError(f"Failed to write WAL intent: {e}")

        # Step 8: Park worktrees
        for wt_path, branch in parked_wts:
            try:
                subprocess.run(
                    ["git", "-C", wt_path, "switch", "--detach", "HEAD"],
                    check=True,
                    capture_output=True,
                    timeout=10
                )
            except subprocess.CalledProcessError as e:
                raise CascadeError(f"Failed to park worktree {wt_path}: {e}")

        # Step 9: Execute cascade (git rebase --update-refs)
        try:
            # Ensure rebase.updateRefs is enabled
            subprocess.run(
                ["git", "config", "rebase.updateRefs", "true"],
                check=True,
                capture_output=True,
                timeout=5
            )

            # Compute rebase base. An explicit rebase_base_sha (e.g. the
            # promoted option's ghost tip during decision re-parenting) takes
            # precedence over the root node's own base. This is required for
            # decision nodes, which have no git base_sha of their own.
            if rebase_base_sha:
                root_base_sha = rebase_base_sha
            else:
                root_node_obj = manifest.get("nodes", {}).get(root_node, {})
                root_base_sha = root_node_obj.get("range", {}).get("base_sha")
                if not root_base_sha:
                    raise CascadeError(f"Root node {root_node} has no base_sha")

            # Find max descendant (deepest topologically, tie-break by SHA)
            max_desc = get_max_descendant(descendants, manifest, root_node)
            max_ghost = _get_node_ghost(manifest, max_desc)
            max_tip_sha = subprocess.check_output(
                ["git", "rev-parse", max_ghost],
                stderr=subprocess.DEVNULL,
                text=True
            ).strip()

            # WARNING: Incorrect endpoint selection would miss parallel descendants
            # (C2 regression) or rewrite unintended commits. Verify git_depth_in_dag
            # metric matches manifest DAG topology (not git history). Test with
            # parallel-branch scenarios (T-C3) before deployment.
            #
            # git rebase takes an <upstream> and a <branch> to rebase, NOT a
            # commit range ("base^..tip" is rejected with "invalid upstream").
            # Rebase the deepest descendant branch onto the root's base; the
            # --update-refs flag rewrites the intermediate ghost refs (root and
            # any parallel branches sharing the range) along the way. This is a
            # no-op when the descendants are already based on root_base_sha
            # (idempotent), and replays cleanly when the root has been rewritten.
            result = subprocess.run(
                ["git", "rebase", "--update-refs", root_base_sha, max_ghost],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                # Conflict detected
                raise ConflictError(result.stderr)

        except ConflictError:
            # Rebase halted due to conflict; mark affected nodes blocked
            for branch in descendants:
                node = find_node_by_ghost(manifest, branch)
                if node:
                    node["status"] = "blocked"

            try:
                wal_write(
                    feature,
                    "cascade-conflict",
                    "commit",
                    {
                        "feature": feature,
                        "blocked_nodes": descendants,
                        "conflict_diff": "see git rebase state"
                    },
                    lock_obj=lock_obj
                )
            except WALError:
                pass

            try:
                manifest_write_atomic(feature, manifest)
            except ManifestError:
                pass

            raise CascadeError(
                f"FLEET_010: Merge conflict during cascade. Nodes marked blocked. "
                f"Resolve in worktree via 'git rebase --continue', then retry cascade."
            )

        # Step 10: Re-attach parked worktrees
        for wt_path, branch in parked_wts:
            try:
                subprocess.run(
                    ["git", "-C", wt_path, "switch", branch],
                    check=True,
                    capture_output=True,
                    timeout=10
                )
            except subprocess.CalledProcessError as e:
                raise CascadeError(f"Failed to re-attach worktree {wt_path}: {e}")

        # Step 11: Mark artifact descendants stale
        for node_name, node in manifest.get("nodes", {}).items():
            if not isinstance(node, dict):
                continue

            if node.get("kind") != "artifact":
                continue

            # Check if artifact depends on any rebased node
            for dep in node.get("deps", []):
                if dep in descendants:
                    node["stale"] = True
                    break

            # Update deps_sha snapshot
            deps_sha = {}
            for dep in node.get("deps", []):
                dep_node = manifest.get("nodes", {}).get(dep, {})
                dep_tip = dep_node.get("range", {}).get("tip_sha")
                if dep_tip:
                    deps_sha[dep] = dep_tip
            node["deps_sha"] = deps_sha

        # Step 12: Mark presentation staleness for rebased nodes
        for node_name in descendants:
            node = manifest.get("nodes", {}).get(node_name, {})
            if node and node.get("present"):
                node["present"]["stale"] = True

        # Step 13: WAL commit
        try:
            wal_write(
                feature,
                "cascade",
                "commit",
                {
                    "feature": feature,
                    "status": "success",
                    "rebased": descendants
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise CascadeError(f"Failed to write WAL commit: {e}")

        # Step 14: Manifest write
        try:
            manifest_write_atomic(feature, manifest)
        except ManifestError as e:
            raise CascadeError(f"Failed to write manifest: {e}")

        return {
            "status": "success",
            "rebased": descendants,
            "conflicts": [],
            "parked_worktrees": len(parked_wts)
        }

    finally:
        if should_release and lock_obj:
            release_lock(lock_obj)


def topo_sort_descendants(manifest: Dict, root_node: str) -> List[str]:
    """
    Topologically sort descendants of root_node.

    Returns list of descendant node names in topological order (BFS from root).
    Deduplicates nodes that have multiple incoming edges (e.g., artifacts depending
    on multiple parents).

    Args:
        manifest: Manifest dict with nodes and edges
        root_node: Root node name

    Returns:
        List of descendant node names (topologically ordered, no duplicates)
    """
    nodes = manifest.get("nodes", {})
    descendants = []
    descendants_set = set()
    visited = set()
    queue = [root_node]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        # Find all children of current (nodes that depend on it)
        for node_name, node in nodes.items():
            if not isinstance(node, dict):
                continue

            # Feature stacking edge (base == current)
            if node.get("base") == current:
                if node_name != root_node:  # Avoid including root itself
                    if node_name not in descendants_set:
                        descendants.append(node_name)
                        descendants_set.add(node_name)
                    queue.append(node_name)

            # Artifact dependency edge (current in deps)
            elif node.get("kind") == "artifact" and current in node.get("deps", []):
                if node_name != root_node:
                    if node_name not in descendants_set:
                        descendants.append(node_name)
                        descendants_set.add(node_name)
                    queue.append(node_name)

    return descendants


def find_node_by_ghost(manifest: Dict, ghost_branch: str) -> Optional[Dict]:
    """
    Find node by ghost branch name (public helper).

    Linear search over manifest nodes; used during conflict handling to identify
    which node needs blocking. Returns node object for status update.

    Args:
        manifest: Manifest dict with nodes
        ghost_branch: Ghost branch name (e.g., "ghost/pay/a2")

    Returns:
        Node dict if found, None otherwise (raises FLEET_060 candidate)
    """
    nodes = manifest.get("nodes", {})
    for node_name, node in nodes.items():
        if isinstance(node, dict) and node.get("ghost") == ghost_branch:
            return node
    return None


def _find_parked_worktrees(descendants: List[str]) -> List[Tuple[str, str]]:
    """
    Find worktrees that need to be parked before cascade.

    Scans git worktree list (--porcelain format) for branches matching descendants.
    Parked worktrees are detached before cascade to prevent ref conflicts during
    --update-refs rewriting, then re-attached after rebase completes.

    Args:
        descendants: List of descendant node names (ghost branches)

    Returns:
        List of (wt_path, branch_name) tuples for worktrees to park
    """
    parked_wts = []
    try:
        # Use --porcelain format for reliable parsing (modern git format with [ref])
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5
        )
        current_wt_path = None
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            # Porcelain format: "worktree /path" line, followed by "branch refs/heads/..."
            if line.startswith("worktree "):
                current_wt_path = line[9:].strip()
            elif line.startswith("branch ") and current_wt_path:
                # Format: "branch refs/heads/ghost/feature/branch"
                branch_ref = line[7:].strip()  # Remove "branch "
                # Extract branch name from refs/heads/...
                if branch_ref.startswith("refs/heads/"):
                    branch = branch_ref[11:]  # Remove "refs/heads/"
                    # Check if this branch is in descendants (as a ghost)
                    for desc in descendants:
                        if branch.endswith(f"/{desc}") or branch == desc:
                            parked_wts.append((current_wt_path, branch))
                            break
    except Exception:
        # Fallback to legacy format parsing if --porcelain fails
        try:
            result = subprocess.run(
                ["git", "worktree", "list"],
                capture_output=True,
                text=True,
                timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                # Legacy format: /path/to/wt SHA (branch/ref) or /path/to/wt SHA [branch/ref]
                parts = line.split()
                if len(parts) < 2:
                    continue
                wt_path = parts[0]
                # Extract branch from square brackets (modern) or parens (legacy)
                branch = None
                if "[" in line and "]" in line:
                    branch = line[line.index("[") + 1:line.index("]")].strip()
                elif "(" in line and ")" in line:
                    branch = line[line.index("(") + 1:line.index(")")].strip()

                if branch:
                    # Check if this branch is in descendants (as a ghost)
                    for desc in descendants:
                        if branch.endswith(f"/{desc}") or branch == desc:
                            parked_wts.append((wt_path, branch))
                            break
        except Exception:
            pass
    return parked_wts


def _dryrun_cascade(
    manifest: Dict,
    root_node: str,
    descendants: List[str],
    parked_wts: List[Tuple[str, str]]
) -> str:
    """
    Generate dry-run summary for cascade operation.

    Formats impact summary (root, descendants, parked worktrees, conflict warning).
    Printed to user for confirmation before executing; helps identify potential issues.

    Args:
        manifest: Manifest dict
        root_node: Root node name whose descendants will rebase
        descendants: List of descendant node names (topologically ordered)
        parked_wts: List of (wt_path, branch) tuples to be detached

    Returns:
        Formatted dry-run summary string (human-readable)
    """
    summary = "DRY-RUN: Cascade rebase\n"
    summary += f"  Root node: {root_node}\n"
    summary += f"  Descendants: {', '.join(descendants)}\n"
    if parked_wts:
        summary += f"  Parked worktrees: {len(parked_wts)}\n"
        for wt_path, branch in parked_wts:
            summary += f"    - {wt_path} ({branch})\n"
    summary += "  (Conflicts possible if base has conflicting changes)\n"
    return summary


def _get_node_ghost(manifest: Dict, node_name: str) -> str:
    """
    Get ghost branch name for a node (private helper).

    Returns manifest-stored ghost branch name, or synthesizes fallback pattern
    if missing. Used throughout cascade for ref operations.

    Args:
        manifest: Manifest dict with nodes
        node_name: Node name to resolve

    Returns:
        Ghost branch name (e.g., "ghost/feature/node_name")
    """
    node = manifest.get("nodes", {}).get(node_name, {})
    return node.get("ghost", f"ghost/{node_name}")


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) > 2:
        feature_name = sys.argv[1]
        root_name = sys.argv[2]
        dry_run_flag = sys.argv[3:] and sys.argv[3] == "--dry-run"

        try:
            result = cascade(feature_name, root_name, dry_run=dry_run_flag)
            print(f"✓ Cascade complete")
            print(json.dumps(result, indent=2))
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: fleet_cascade.py <feature> <root_node> [--dry-run]", file=sys.stderr)
        sys.exit(1)
