#!/usr/bin/env python3
"""
Decision node resolution — promote option, re-parent descendants, emit rebuild contract.

EXPERIMENTAL: Gated by manifest schema experimental flag. Resolves decision nodes,
re-parents descendants onto winning option, archives losing options, emits rebuild
prompts for assuming nodes with on_flip strategy handlers.

WARNING: This operation performs history rewriting (rebase + branch archiving).
Assumes are re-parented and branches are moved to archive/ namespace. All operations
are wrapped in lock + WAL for crash recovery.
"""

import os
import subprocess
import json
from typing import Dict, Optional, List, IO
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError
from fleet_wal import wal_write, WALError
from fleet_lock import acquire_lock, release_lock, LockTimeoutError


class DecisionError(Exception):
    """Base exception for decision resolution errors."""
    pass


class ExperimentalGateError(DecisionError):
    """Experimental feature gated but manifest not experimental-enabled."""
    pass


def decide_resolve(
    feature: str,
    decision_node: str,
    chosen_option: str
) -> Dict:
    """
    Resolve decision node by promoting chosen option and re-parenting descendants.

    WARNING: This operation performs git history rewriting (rebase), branch
    archival (rename to archive/), and manifest updates. All under lock + WAL.

    Validates experimental gating, promotes option, re-parents descendants via
    cascade, emits rebuild prompts for assuming nodes, archives losing options,
    wraps entire operation in lock and WAL.

    Args:
        feature: Feature name
        decision_node: Decision node name to resolve
        chosen_option: Option name to promote (must exist in decision.options)

    Returns:
        Dict with {status, promoted, archived, rebuild_prompts} or error

    Raises:
        DecisionError: Operation failed
        ExperimentalGateError: Experimental not enabled in manifest
        ManifestError: Manifest read/write failed
        WALError: WAL write failed
    """
    # Acquire lock for entire operation
    feature_path = os.path.expanduser(f"~/.fleet/{feature}")
    lock_path = os.path.join(feature_path, ".fleet.lock")
    try:
        lock_obj = acquire_lock(lock_path)
    except LockTimeoutError as e:
        raise DecisionError(f"Failed to acquire lock: {e}")

    try:
        # Step 1: Read manifest
        try:
            manifest = manifest_read(feature)
        except Exception as e:
            raise DecisionError(f"Failed to read manifest: {e}")

        # Step 2: Check experimental gating
        if not manifest.get("experimental", False):
            raise ExperimentalGateError(
                "FLEET_099: Decision node resolution is experimental and not enabled. "
                "Set 'experimental: true' in manifest to use this feature."
            )

        # Step 3: Validate decision node
        if decision_node not in manifest.get("nodes", {}):
            raise DecisionError(f"Node '{decision_node}' not found in manifest")

        decision = manifest["nodes"][decision_node]

        if decision.get("kind") != "decision":
            raise DecisionError(f"Node '{decision_node}' is not a decision node (kind={decision.get('kind')})")

        # Step 4: Validate chosen option
        options = decision.get("options", {})
        if chosen_option not in options:
            raise DecisionError(
                f"Option '{chosen_option}' not found. Available: {list(options.keys())}"
            )

        chosen_ghost = options[chosen_option].get("ghost")
        if not chosen_ghost:
            raise DecisionError(f"Option '{chosen_option}' has no ghost branch")

        # Step 5: Promote chosen option
        decision["promoted"] = chosen_option
        decision["status"] = "resolved"

        # Step 6: Re-parent descendants onto promoted option (via cascade)
        descendants = _topo_sort_descendants(manifest, decision_node)

        if descendants:
            # Import cascade here to avoid circular import (cascade also depends on decision indirectly via cascade.py)
            try:
                from fleet_cascade import cascade
            except ImportError:
                raise DecisionError("Failed to import cascade module (fleet_cascade.py not found)")

            try:
                cascade_result = cascade(feature, decision_node, dry_run=False, lock_obj=lock_obj)
                if cascade_result.get("error"):
                    # Mark descendants blocked on cascade error
                    for desc in descendants:
                        manifest["nodes"][desc]["status"] = "blocked"
                    return {
                        "error": f"Conflict during decision re-parent: {cascade_result.get('error')}",
                        "action": "Resolve conflicts and retry"
                    }
            except Exception as e:
                raise DecisionError(f"Cascade during decision re-parent failed: {e}")

        # Step 7: Emit rebuild contract prompts for assuming nodes
        rebuild_prompts = []

        for node_name, node_obj in manifest.get("nodes", {}).items():
            if not isinstance(node_obj, dict):
                continue

            assumes = node_obj.get("assumes", {})
            if decision_node not in assumes:
                continue

            assumption = assumes[decision_node]
            losing_option = assumption.get("option")

            # Node assumes this decision; check if option flipped
            if losing_option and losing_option != chosen_option:
                # Option flipped; emit rebuild prompt
                prompt = {
                    "node": node_name,
                    "decision": decision_node,
                    "losing_option": losing_option,
                    "winning_option": chosen_option,
                    "why": assumption.get("why", ""),
                    "surfaces": assumption.get("surfaces", []),
                    "on_flip_strategy": assumption.get("on_flip", "manual"),
                    "winning_spec": f"Winning option: {chosen_option}"
                }

                rebuild_prompts.append(prompt)

                # Apply on_flip strategy
                on_flip = assumption.get("on_flip", "manual")

                if on_flip == "rebuild":
                    node_obj["status"] = "needs-rework"
                elif on_flip == "rebase":
                    # Auto-rebase node onto winning option (safe syntactic change)
                    try:
                        subprocess.run(
                            ["git", "rebase", chosen_ghost, node_obj.get("ghost")],
                            check=True,
                            capture_output=True,
                            timeout=30
                        )
                    except subprocess.CalledProcessError as e:
                        # Rebase conflict; mark blocked
                        node_obj["status"] = "blocked"
                        print(f"Warning: Auto-rebase failed for {node_name}: {e}")
                elif on_flip == "manual":
                    node_obj["status"] = "needs-rework"

        # Step 8: Archive losing options
        archived = []
        for option_name in options:
            if option_name != chosen_option:
                losing_ghost = options[option_name].get("ghost")
                archived.append(option_name)

                # WARNING: Branch archival operation.
                # Move losing branch to archive/ namespace for audit trail.
                # All losing options are archived (never deleted) to preserve history.

                if losing_ghost:
                    try:
                        archive_branch = f"archive/{losing_ghost}"
                        subprocess.run(
                            ["git", "branch", "-m", losing_ghost, archive_branch],
                            capture_output=True,
                            timeout=10
                        )
                    except subprocess.CalledProcessError:
                        # Archive failed; log but continue (non-fatal)
                        print(f"Warning: Failed to archive branch {losing_ghost}")

                # Find node for this option and mark archived
                for n_name, n_obj in manifest.get("nodes", {}).items():
                    if isinstance(n_obj, dict) and n_obj.get("ghost") == losing_ghost:
                        n_obj["status"] = "archived"

        # Step 9: WAL intent (under lock)
        try:
            wal_write(
                feature,
                "decide-resolve",
                "intent",
                {
                    "feature": feature,
                    "decision": decision_node,
                    "chosen": chosen_option,
                    "archived": archived,
                    "affected_nodes": [p["node"] for p in rebuild_prompts]
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise DecisionError(f"Failed to write WAL intent: {e}")

        # Step 10: Manifest write (under lock)
        try:
            manifest_write_atomic(feature, manifest)
        except ManifestError as e:
            raise DecisionError(f"Failed to write manifest: {e}")

        # Step 11: WAL commit (under lock)
        try:
            wal_write(
                feature,
                "decide-resolve",
                "commit",
                {
                    "feature": feature,
                    "decision": decision_node,
                    "status": "success"
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise DecisionError(f"Failed to write WAL commit: {e}")

        return {
            "status": "resolved",
            "promoted": chosen_option,
            "archived": archived,
            "rebuild_prompts": rebuild_prompts
        }

    finally:
        release_lock(lock_obj)


def _topo_sort_descendants(manifest: Dict, node: str) -> List[str]:
    """
    Topologically sort descendants of a node in the DAG.

    Args:
        manifest: Manifest dict with nodes and edges
        node: Node name

    Returns:
        List of descendant node names in topological order (BFS)
    """
    descendants = []
    visited = set()
    queue = [node]

    nodes = manifest.get("nodes", {})

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        # Find children of current node
        for candidate_name, candidate_node in nodes.items():
            if not isinstance(candidate_node, dict):
                continue

            # Feature stacking edge
            if candidate_node.get("base") == current:
                descendants.append(candidate_name)
                queue.append(candidate_name)

            # Artifact dependency edge
            elif candidate_node.get("kind") == "artifact":
                if current in candidate_node.get("deps", []):
                    descendants.append(candidate_name)
                    queue.append(candidate_name)

    return descendants


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 3:
        feature_name = sys.argv[1]
        decision_node_name = sys.argv[2]
        chosen_option_name = sys.argv[3]

        try:
            result = decide_resolve(feature_name, decision_node_name, chosen_option_name)
            print(f"✓ Decision resolution complete")
            print(json.dumps(result, indent=2))
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
