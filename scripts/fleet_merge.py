#!/usr/bin/env python3
"""
Merge-back via git rebase --onto with squash cleanup.

Rebase children onto main to drop node's commits, handle topological order
enforcement, lease-fail recovery, and manifest updates.

Wraps entire operation in lock + WAL intent/commit for crash recovery.
"""

import os
import sys
import subprocess
from typing import Dict, List, Optional, IO, Tuple
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError
from fleet_wal import wal_write, WALError
from fleet_lock import acquire_lock, release_lock, git_depth_in_dag, get_max_descendant, LockTimeoutError
from fleet_reconcile import reconcile, ReconcileError
from fleet_cascade import cascade, topo_sort_descendants, _find_parked_worktrees
from fleet_worktree import worktree_is_dirty, WorktreeDirtyError


class MergeError(Exception):
    """Base exception for merge errors."""
    pass


class ConflictError(MergeError):
    """Merge conflict during rebase --onto."""
    pass


class LeaseFailError(MergeError):
    """Force-push lease failed (remote changed)."""
    pass


def merge_back(
    feature: str,
    node: str,
    merge_mode: str = "individual",
    lease_action: Optional[str] = None
) -> Dict:
    """
    Merge-back (squash cleanup via --onto).

    Re-parents children onto main to drop node's commits. Enforces topological
    order in individual mode. Handles lease-fail on force-push. Updates manifest.

    Args:
        feature: Feature name
        node: Node name to merge-back
        merge_mode: "individual" (enforce order) or "batch" (merge all)
        lease_action: Optional action for lease conflicts ("incorporate", "discard", "manual")

    Returns:
        Dict with {status, node, action, error?}

    Raises:
        MergeError: Merge failed (conflict, out-of-order, etc.)
    """
    feature_path = os.path.expanduser(f"~/.fleet/{feature}")
    lock_path = os.path.join(feature_path, ".fleet.lock")

    # Step 1: Read manifest and reconcile
    try:
        manifest = manifest_read(feature)
    except Exception as e:
        raise MergeError(f"Failed to read manifest: {e}")

    try:
        manifest, unobserved_writes, divergences = reconcile(feature, manifest)
    except ReconcileError as e:
        raise MergeError(f"Reconciliation failed: {e}")

    # Step 2: Validate topological order (individual mode only)
    if merge_mode == "individual":
        try:
            _validate_topo_order(manifest, node)
        except MergeError:
            raise

    # Step 3: Dry-run
    children = topo_sort_descendants(manifest, node)
    commits_to_drop = _count_commits_in_range(manifest, node)

    dry_summary = _dryrun_merge(manifest, node, children, commits_to_drop)
    print(dry_summary)

    response = input("Proceed? (yes/no): ").strip()
    if response != "yes":
        return {"status": "cancelled"}

    # Step 4: Push presentation (if exists)
    node_obj = manifest.get("nodes", {}).get(node, {})
    if node_obj and node_obj.get("present"):
        try:
            _push_presentation(manifest, node)
        except LeaseFailError:
            # Lease failed; prompt user (incorporate/discard/manual)
            return handle_lease_fail(feature, node, lease_action)

    # Step 4b: Preflight worktree guard (BUG-16/FR-V2-8)
    # Before lock acquire, mirror cascade's preflight: find parked worktrees for
    # children/descendants in merge path. If any dirty → raise FLEET_040 and abort
    # BEFORE any git rewrite. Park (detach) clean ones before rebase to prevent
    # ref conflicts during --onto rewriting.
    try:
        parked_wts = _find_parked_worktrees(children)
    except Exception as e:
        raise MergeError(f"Failed to find parked worktrees: {e}")

    for wt_path, branch in parked_wts:
        if worktree_is_dirty(wt_path):
            raise MergeError(
                f"FLEET_040: Worktree {wt_path} has uncommitted changes; "
                f"cannot merge-back. Commit or stash changes and retry."
            )

    # Park (detach) clean worktrees before rebase
    for wt_path, branch in parked_wts:
        try:
            subprocess.run(
                ["git", "-C", wt_path, "switch", "--detach", "HEAD"],
                check=True,
                capture_output=True,
                timeout=10
            )
        except subprocess.CalledProcessError as e:
            raise MergeError(f"Failed to park worktree {wt_path}: {e}")

    # Step 5: Acquire lock (wraps entire operation)
    try:
        lock_obj = acquire_lock(lock_path)
    except LockTimeoutError as e:
        raise MergeError(f"Failed to acquire lock: {e}")

    try:
        # Step 6: WAL intent (pre-merge state)
        target_main_sha = subprocess.check_output(
            ["git", "rev-parse", "main"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()

        pre_image_refs = {}
        for child in children:
            try:
                child_ghost = _get_node_ghost(manifest, child)
                sha = subprocess.check_output(
                    ["git", "rev-parse", child_ghost],
                    stderr=subprocess.DEVNULL,
                    text=True
                ).strip()
                pre_image_refs[child] = sha
            except Exception:
                pass

        try:
            wal_write(
                feature,
                "merge-back-intent",
                "intent",
                {
                    "feature": feature,
                    "node": node,
                    "merge_mode": merge_mode,
                    "pre_image_refs": pre_image_refs,
                    "target_main_sha": target_main_sha
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise MergeError(f"Failed to write WAL intent: {e}")

        # Step 7: Execute --onto cleanup
        try:
            # Ensure rebase.updateRefs is enabled
            subprocess.run(
                ["git", "config", "rebase.updateRefs", "true"],
                check=True,
                capture_output=True,
                timeout=5
            )

            node_obj = manifest.get("nodes", {}).get(node, {})
            node_tip_sha = node_obj.get("range", {}).get("tip_sha")
            if not node_tip_sha:
                raise MergeError(f"Node {node} has no tip_sha")

            # Filter out artifacts from rebase set (BUG-15/FR-V2-8):
            # Artifacts have no ghost branches → rebase fatal. Mark stale, do not rebase.
            rebaseable_children = [
                c for c in children
                if manifest.get("nodes", {}).get(c, {}).get("kind") != "artifact"
            ]

            # Handle three cases based on rebaseable child count
            if len(rebaseable_children) == 0:
                # Zero rebaseable children (leaf node or all artifacts): archive only, no rebase needed
                # Drop node's commits by not re-parenting anything
                pass

            elif len(rebaseable_children) == 1:
                # Single rebaseable child: simple --onto (existing working behavior)
                child = rebaseable_children[0]
                child_ghost = _get_node_ghost(manifest, child)

                result = subprocess.run(
                    ["git", "rebase", "--onto", "main", node_tip_sha, child_ghost],
                    capture_output=True,
                    text=True,
                    timeout=300
                )

                if result.returncode != 0:
                    # Inspect stderr to distinguish merge conflicts from other errors
                    if _is_merge_conflict(result.stderr):
                        raise ConflictError(result.stderr)
                    else:
                        raise MergeError(
                            f"Git rebase failed: {result.stderr}"
                        )

            else:
                # Multiple rebaseable children: rebase each child individually with --update-refs
                # WARNING: Incorrect endpoint selection would miss parallel children
                # (C3 regression) or rewrite unintended commits. Verify git_depth_in_dag
                # metric matches manifest DAG topology (not git history). Test with
                # parallel-branch scenarios (T-M8) before deployment.
                for child in rebaseable_children:
                    child_ghost = _get_node_ghost(manifest, child)

                    result = subprocess.run(
                        ["git", "rebase", "--update-refs", "--onto", "main",
                         node_tip_sha, child_ghost],
                        capture_output=True,
                        text=True,
                        timeout=300
                    )

                    if result.returncode != 0:
                        # Inspect stderr to distinguish merge conflicts from other errors
                        if _is_merge_conflict(result.stderr):
                            raise ConflictError(result.stderr)
                        else:
                            raise MergeError(
                                f"Git rebase failed for child {child}: {result.stderr}"
                            )

        except ConflictError:
            # True merge conflict detected; mark node blocked
            node_obj["status"] = "blocked"

            try:
                wal_write(
                    feature,
                    "merge-back-conflict",
                    "commit",
                    {
                        "feature": feature,
                        "node": node,
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

            raise MergeError(
                f"FLEET_011: Merge conflict during cleanup. Node marked blocked. "
                f"Resolve in worktree via 'git rebase --continue', then retry merge."
            )

        # Step 8: Mark node archived, clear presentation
        node_obj["status"] = "archived"
        node_obj["present"] = None

        # Step 9: Update rebaseable children's base_sha (now points to main)
        for child in rebaseable_children:
            child_obj = manifest.get("nodes", {}).get(child, {})
            if child_obj:
                child_ghost = child_obj.get("ghost")
                if child_ghost:
                    try:
                        live_base = subprocess.check_output(
                            ["git", "merge-base", child_ghost, "main"],
                            stderr=subprocess.DEVNULL,
                            text=True
                        ).strip()
                        if "range" not in child_obj:
                            child_obj["range"] = {}
                        child_obj["range"]["base_sha"] = live_base
                    except Exception:
                        pass

        # Step 9b: Mark artifact descendants stale (BUG-15)
        # Artifacts have no ghost branches; they are skipped in rebase above.
        # Mark them stale and update deps_sha snapshot.
        for node_name, node in manifest.get("nodes", {}).items():
            if not isinstance(node, dict):
                continue

            if node.get("kind") != "artifact":
                continue

            # Check if artifact depends on the merged node or any rebased child
            affected = False
            if node.get("base") == node:
                affected = True
            else:
                for dep in node.get("deps", []):
                    if dep == node or dep in rebaseable_children:
                        affected = True
                        break

            if affected:
                node["stale"] = True
                # Update deps_sha snapshot
                deps_sha = {}
                for dep in node.get("deps", []):
                    dep_node = manifest.get("nodes", {}).get(dep, {})
                    dep_tip = dep_node.get("range", {}).get("tip_sha")
                    if dep_tip:
                        deps_sha[dep] = dep_tip
                node["deps_sha"] = deps_sha

        # Step 9c: Re-attach parked worktrees (BUG-16)
        # Mirror cascade's re-attach: switch worktrees back to their branches.
        for wt_path, branch in parked_wts:
            try:
                subprocess.run(
                    ["git", "-C", wt_path, "switch", branch],
                    check=True,
                    capture_output=True,
                    timeout=10
                )
            except subprocess.CalledProcessError as e:
                raise MergeError(f"Failed to re-attach worktree {wt_path}: {e}")

        # Step 10: WAL commit
        try:
            wal_write(
                feature,
                "merge-back-intent",
                "commit",
                {
                    "feature": feature,
                    "node": node,
                    "status": "success"
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise MergeError(f"Failed to write WAL commit: {e}")

        # Step 11: Manifest write
        try:
            manifest_write_atomic(feature, manifest)
        except ManifestError as e:
            raise MergeError(f"Failed to write manifest: {e}")

        # Step 12: Batch mode special case (cascade remaining)
        if merge_mode == "batch":
            remaining_nodes = [
                n for n in manifest.get("nodes", {}).keys()
                if manifest.get("nodes", {}).get(n, {}).get("status") != "archived"
                and manifest.get("nodes", {}).get(n, {}).get("kind") == "feature"
            ]
            if remaining_nodes:
                # Find topological root of remaining nodes
                try:
                    root_of_remaining = min(
                        remaining_nodes,
                        key=lambda n: _topo_order(manifest, n)
                    )
                    # Cascade acquires its own lock
                    cascade(feature, root_of_remaining)
                except Exception:
                    pass  # Cascade errors logged separately

        return {
            "status": "merged",
            "node": node,
            "action": "merge-back-complete"
        }

    finally:
        release_lock(lock_obj)


def handle_lease_fail(feature: str, node: str, lease_action: Optional[str] = None) -> Dict:
    """
    Handle force-push lease failure (reviewer pushed to PR).

    Fetches remote presentation, shows reviewer commits, prompts user
    (incorporate/discard/manual). For incorporate: merges reviewer commits
    into ghost and regenerates presentation.

    Args:
        feature: Feature name
        node: Node name with presentation
        lease_action: Optional action to bypass prompt ("incorporate", "discard", "manual").
                      If not provided and stdin is not a TTY, defaults to "manual" (safe).

    Returns:
        Dict with {status, action, error?}

    Raises:
        MergeError: Lease-fail resolution failed
    """
    try:
        manifest = manifest_read(feature)
    except Exception as e:
        raise MergeError(f"Failed to read manifest: {e}")

    node_obj = manifest.get("nodes", {}).get(node, {})
    if not node_obj or not node_obj.get("present"):
        raise MergeError(f"Node {node} has no presentation")

    present_branch = node_obj.get("present", {}).get("branch")
    ghost_branch = node_obj.get("ghost")

    if not present_branch or not ghost_branch:
        raise MergeError(f"Node {node} missing presentation or ghost branch")

    # Fetch remote presentation
    try:
        subprocess.run(
            ["git", "fetch", "origin", present_branch],
            check=True,
            capture_output=True,
            timeout=30
        )
    except subprocess.CalledProcessError as e:
        raise MergeError(f"Failed to fetch remote {present_branch}: {e}")

    # Show reviewer commits (diff ghost vs remote presentation)
    try:
        result = subprocess.run(
            ["git", "log", f"{ghost_branch}..origin/{present_branch}", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5
        )
        reviewer_commits = result.stdout.strip()
    except Exception:
        reviewer_commits = "(unable to compute)"

    # Prompt user (or use provided action, with TTY/non-TTY fallback)
    print(f"Reviewer pushed commits to {present_branch}:")
    if reviewer_commits:
        print(reviewer_commits)
    print("\nChoices:")
    print("  (a) incorporate into ghost → regenerate")
    print("  (b) discard (force-push)")
    print("  (c) manual (user resolves)")

    # Determine action: explicit flag, TTY prompt, or safe default
    choice = None
    if lease_action:
        if lease_action == "incorporate":
            choice = "a"
        elif lease_action == "discard":
            choice = "b"
        elif lease_action == "manual":
            choice = "c"
    elif sys.stdin.isatty():
        choice = input("Choice (a/b/c): ").strip().lower()
    else:
        # Non-interactive mode: default to safe no-op (manual)
        choice = "c"
        print("FLEET_LRF: Lease conflict found; rerun with --lease-action=incorporate|discard|manual to proceed")

    if choice == "a":
        # Merge reviewer commits into ghost
        try:
            subprocess.run(
                ["git", "checkout", ghost_branch],
                check=True,
                capture_output=True,
                timeout=10
            )
            subprocess.run(
                ["git", "merge", f"origin/{present_branch}", "--no-edit"],
                check=True,
                capture_output=True,
                timeout=60
            )
        except subprocess.CalledProcessError as e:
            raise MergeError(f"Failed to incorporate reviewer commits: {e}")

        # Update manifest ghost tip_sha
        try:
            new_ghost_sha = subprocess.check_output(
                ["git", "rev-parse", ghost_branch],
                stderr=subprocess.DEVNULL,
                text=True
            ).strip()
            if "range" not in node_obj:
                node_obj["range"] = {}
            node_obj["range"]["tip_sha"] = new_ghost_sha
            manifest_write_atomic(feature, manifest)
        except Exception as e:
            raise MergeError(f"Failed to update ghost tip: {e}")

        # Regenerate presentation from plan (via fleet_present_gen if available)
        # For now, force-push with same plan
        try:
            subprocess.run(
                ["git", "push", "--force-with-lease", "origin", f"{ghost_branch}:{present_branch}"],
                check=True,
                capture_output=True,
                timeout=30
            )
        except subprocess.CalledProcessError as e:
            raise MergeError(f"Failed to push presentation: {e}")

        return {
            "status": "resolved",
            "action": "incorporated",
            "message": "Reviewer commits incorporated; presentation regenerated and pushed."
        }

    elif choice == "b":
        # Force-push (clobber)
        print("WARNING: Force-push will discard reviewer work.")

        # Determine confirmation: check TTY, require explicit opt-in in non-interactive
        confirm = None
        if lease_action == "discard":
            # Explicit opt-in via flag
            confirm = "yes"
        elif sys.stdin.isatty():
            confirm = input("Are you sure? (yes/no): ").strip().lower()
        else:
            # Non-interactive mode: require explicit flag
            confirm = "no"
            print("FLEET_LFP: Force-push requires explicit --lease-action=discard flag in non-interactive mode")

        if confirm != "yes":
            return {"status": "cancelled", "action": "user cancelled"}

        try:
            subprocess.run(
                ["git", "push", "--force", "origin", f"{ghost_branch}:{present_branch}"],
                check=True,
                capture_output=True,
                timeout=30
            )
        except subprocess.CalledProcessError as e:
            raise MergeError(f"Failed to force-push: {e}")

        return {
            "status": "resolved",
            "action": "discarded",
            "message": "Reviewer commits discarded; presentation force-pushed."
        }

    else:
        # Manual (user resolves)
        return {
            "status": "manual",
            "action": "user_resolves",
            "message": f"User must manually resolve {present_branch} on GitHub."
        }


def _is_merge_conflict(stderr: str) -> bool:
    """
    Detect if git rebase stderr indicates a true merge conflict (private helper).

    Distinguishes actual merge conflicts from other git rebase errors (invalid refs,
    syntax errors, etc.). Used to correctly classify error as FLEET_011 (merge
    conflict) vs. generic MergeError.

    Args:
        stderr: Git stderr output from rebase command

    Returns:
        bool: True if stderr indicates merge conflict, False for other errors
    """
    conflict_indicators = [
        "CONFLICT",
        "conflict",
        "Merge conflict",
        "merge conflict",
    ]
    return any(indicator in stderr for indicator in conflict_indicators)


def _validate_topo_order(manifest: Dict, node: str) -> None:
    """
    Validate topological order for individual merge mode (private helper).

    Enforces FLEET_050: all ancestor nodes must be archived before merge.
    Prevents corruption where child re-parents to main before parent merged.

    Args:
        manifest: Manifest dict with nodes and base edges
        node: Node name to merge

    Raises:
        MergeError: Out-of-order merge detected (FLEET_050)
    """
    nodes = manifest.get("nodes", {})
    node_obj = nodes.get(node, {})

    # Find all ancestor nodes (follow base links backward)
    ancestors = []
    current = node_obj.get("base")
    while current and current in nodes:
        ancestors.append(current)
        current = nodes[current].get("base")

    # Check if all ancestors are archived
    for ancestor in ancestors:
        ancestor_status = nodes.get(ancestor, {}).get("status")
        if ancestor_status != "archived":
            raise MergeError(
                f"FLEET_050: Out-of-order merge blocked. "
                f"Ancestor {ancestor} (status={ancestor_status}) must be merged first."
            )


def _count_commits_in_range(manifest: Dict, node: str) -> int:
    """
    Count commits in node's range (base_sha..tip_sha) (private helper).

    Used for dry-run preview. Runs `git rev-list --count` on reconciled range.

    Args:
        manifest: Manifest dict with reconciled range SHAs
        node: Node name to count

    Returns:
        Approximate commit count (int), or 0 if range invalid
    """
    node_obj = manifest.get("nodes", {}).get(node, {})
    base_sha = node_obj.get("range", {}).get("base_sha")
    tip_sha = node_obj.get("range", {}).get("tip_sha")

    if not base_sha or not tip_sha:
        return 0

    try:
        result = subprocess.run(
            ["git", "rev-list", f"{base_sha}..{tip_sha}", "--count"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        return 0


def _dryrun_merge(
    manifest: Dict,
    node: str,
    children: List[str],
    commits_to_drop: int
) -> str:
    """
    Generate dry-run summary for merge-back operation (private helper).

    Formats impact summary (node, commits to drop, children to re-parent, new base).
    Printed before confirmation; helps identify data-safety risks.

    Args:
        manifest: Manifest dict
        node: Node name to merge-back
        children: List of child node names to be re-parented onto main
        commits_to_drop: Approximate commit count from node's range

    Returns:
        Formatted dry-run summary string (human-readable)
    """
    summary = "DRY-RUN: Merge-back (squash cleanup via --onto)\n"
    summary += f"  Node: {node}\n"
    summary += f"  Commits to drop: {commits_to_drop}\n"
    summary += f"  Children to re-parent: {', '.join(children) if children else '(none)'}\n"
    summary += f"  New base: main\n"
    return summary


def _push_presentation(manifest: Dict, node: str) -> None:
    """
    Push presentation branch to origin with --force-with-lease (private helper).

    Safely force-pushes presentation branch. If lease fails (remote updated),
    raises LeaseFailError to trigger handler (incorporate/discard/manual).

    Args:
        manifest: Manifest dict with node presentation metadata
        node: Node name with presentation branch to push

    Raises:
        LeaseFailError: Force-push lease failed (FLEET_020 recovery path)
        MergeError: Push failed (other reasons)
    """
    node_obj = manifest.get("nodes", {}).get(node, {})
    present_branch = node_obj.get("present", {}).get("branch")
    ghost_branch = node_obj.get("ghost")

    if not present_branch or not ghost_branch:
        return

    try:
        result = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", f"{ghost_branch}:{present_branch}"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            # Check if lease failed (remote ref updated)
            if "rejected" in result.stderr.lower() or "fast-forward" in result.stderr.lower():
                raise LeaseFailError(f"Lease failed on force-push: {result.stderr}")
            else:
                raise MergeError(f"Push failed: {result.stderr}")

    except subprocess.TimeoutExpired:
        raise MergeError("Push timed out")


def _get_node_ghost(manifest: Dict, node_name: str) -> str:
    """
    Get ghost branch name for a node (private helper).

    Returns manifest-stored ghost branch, or synthesizes fallback if missing.

    Args:
        manifest: Manifest dict with nodes
        node_name: Node name to resolve

    Returns:
        Ghost branch name (e.g., "ghost/feature/node_name")
    """
    node = manifest.get("nodes", {}).get(node_name, {})
    return node.get("ghost", f"ghost/{node_name}")


def _topo_order(manifest: Dict, node: str) -> int:
    """
    Compute topological sort order for a node (private helper, batch mode).

    Follows base edges backward; returns depth (lower = earlier in merge order).
    Used in batch merge to auto-cascade remaining nodes in correct order.

    Args:
        manifest: Manifest dict with base edges
        node: Node name to rank

    Returns:
        Sort order (int): edge count from root (lower = closer to root)
    """
    nodes = manifest.get("nodes", {})
    order = 0
    current = node

    while current and current in nodes:
        order += 1
        current = nodes[current].get("base")

    return order


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) > 2:
        feature_name = sys.argv[1]
        node_name = sys.argv[2]
        merge_mode = sys.argv[3] if len(sys.argv) > 3 else "individual"

        try:
            result = merge_back(feature_name, node_name, merge_mode)
            print(f"✓ Merge-back complete")
            print(json.dumps(result, indent=2))
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: fleet_merge.py <feature> <node> [individual|batch]", file=sys.stderr)
        sys.exit(1)
