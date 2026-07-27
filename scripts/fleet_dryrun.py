#!/usr/bin/env python3
"""
Dry-run planner for fleet operations.

Generates readable, predictive summaries for destructive operations (CASCADE, MERGE,
PUBLISH, etc.) without executing them. Helps users understand impact before confirming.
"""

import subprocess
from typing import Dict, List, Optional, Tuple
from fleet_manifest import manifest_read, ManifestError
from fleet_cascade import topo_sort_descendants, find_node_by_ghost


class DryrunError(Exception):
    """Base exception for dryrun errors."""
    pass


def dryrun_plan(intent: str, args: Dict, manifest: Dict) -> str:
    """
    Generate dry-run summary for an operation.

    Args:
        intent: Operation intent (CASCADE, MERGE, PUBLISH, etc.)
        args: Operation arguments (feature, nodes, dry_run, etc.)
        manifest: Manifest dict

    Returns:
        Readable summary string
    """
    output = ""

    if intent == "CASCADE":
        output = _dryrun_cascade(args, manifest)

    elif intent == "MERGE":
        output = _dryrun_merge(args, manifest)

    elif intent == "PUBLISH":
        output = _dryrun_publish(args, manifest)

    elif intent == "COMMIT":
        output = _dryrun_commit(args, manifest)

    elif intent == "RESOLVE_DECISION":
        output = _dryrun_decide(args, manifest)

    elif intent == "ARTIFACT_REFRESH":
        output = _dryrun_artifact_refresh(args, manifest)

    else:
        output = f"DRY-RUN: {intent}\n  (No preview available)\n"

    return output


def _dryrun_cascade(args: Dict, manifest: Dict) -> str:
    """Generate cascade dry-run summary."""
    nodes = args.get("nodes", [])
    if not nodes:
        return "DRY-RUN: CASCADE\n  No nodes specified.\n"

    root_node = nodes[0]
    descendants = topo_sort_descendants(manifest, root_node)

    summary = "DRY-RUN: Cascade rebase\n"
    summary += f"  Root node: {root_node}\n"
    summary += f"  Descendants to rebase: {', '.join(descendants) if descendants else '(none)'}\n"

    # Predict worktrees to park
    parked_wts = _find_parked_wts(descendants, manifest)
    if parked_wts:
        summary += f"  Parked worktrees: {len(parked_wts)}\n"
        for wt_path, branch in parked_wts:
            summary += f"    - {wt_path} ({branch})\n"

    # Warn about possible conflicts
    summary += "  Note: Conflicts possible if base has conflicting changes.\n"

    return summary


def _dryrun_merge(args: Dict, manifest: Dict) -> str:
    """Generate merge-back dry-run summary."""
    nodes = args.get("nodes", [])
    if not nodes:
        return "DRY-RUN: MERGE\n  No nodes specified.\n"

    node = nodes[0]
    node_obj = manifest.get("nodes", {}).get(node, {})

    children = topo_sort_descendants(manifest, node)
    commits_to_drop = _count_commits(node_obj)

    summary = "DRY-RUN: Merge-back (squash cleanup via --onto)\n"
    summary += f"  Node: {node}\n"
    summary += f"  Commits to drop: {commits_to_drop}\n"
    summary += f"  Children to re-parent: {', '.join(children) if children else '(none)'}\n"
    summary += f"  New base: main\n"

    # Warn about presentation push
    if node_obj.get("present"):
        summary += "  Presentation branch will be force-pushed (--force-with-lease).\n"

    return summary


def _dryrun_publish(args: Dict, manifest: Dict) -> str:
    """Generate publish (presentation generation) dry-run summary."""
    nodes = args.get("nodes", [])
    if not nodes:
        return "DRY-RUN: PUBLISH\n  No nodes specified.\n"

    node = nodes[0]
    node_obj = manifest.get("nodes", {}).get(node, {})

    present = node_obj.get("present", {})
    branch = present.get("branch", f"present/{node}")
    pr = present.get("pr")

    summary = "DRY-RUN: Publish (generate presentation)\n"
    summary += f"  Node: {node}\n"
    summary += f"  Presentation branch: {branch}\n"
    summary += f"  Force-push to: origin/{branch}\n"

    if pr:
        summary += f"  Existing PR: #{pr}\n"
    else:
        summary += f"  New PR will be created.\n"

    return summary


def _dryrun_commit(args: Dict, manifest: Dict) -> str:
    """Generate commit (mark done) dry-run summary."""
    nodes = args.get("nodes", [])
    if not nodes:
        return "DRY-RUN: COMMIT\n  No nodes specified.\n"

    node = nodes[0]
    descendants = topo_sort_descendants(manifest, node)

    summary = "DRY-RUN: Mark node done (commit)\n"
    summary += f"  Node: {node}\n"
    summary += f"  Status will change: draft → in-progress\n"

    if descendants:
        summary += f"  Descendants will cascade: {', '.join(descendants)}\n"
    else:
        summary += "  No descendants to cascade.\n"

    return summary


def _dryrun_decide(args: Dict, manifest: Dict) -> str:
    """Generate decision resolve dry-run summary."""
    nodes = args.get("nodes", [])
    if not nodes:
        return "DRY-RUN: RESOLVE_DECISION\n  No decision nodes specified.\n"

    decision_node = nodes[0]
    decision_obj = manifest.get("nodes", {}).get(decision_node, {})

    if decision_obj.get("kind") != "decision":
        return f"DRY-RUN: RESOLVE_DECISION\n  Error: {decision_node} is not a decision node.\n"

    chosen_option = args.get("args", {}).get("chosen_option")
    options = decision_obj.get("options", {})

    summary = "DRY-RUN: Resolve decision node\n"
    summary += f"  Decision: {decision_node}\n"
    summary += f"  Available options: {', '.join(options.keys())}\n"

    if chosen_option:
        losing_options = [o for o in options.keys() if o != chosen_option]
        summary += f"  Chosen: {chosen_option}\n"
        summary += f"  Archived: {', '.join(losing_options)}\n"
        summary += "  Descendants will re-parent onto chosen option.\n"

    return summary


def _dryrun_artifact_refresh(args: Dict, manifest: Dict) -> str:
    """Generate artifact refresh dry-run summary."""
    nodes = args.get("nodes", [])
    if not nodes:
        return "DRY-RUN: ARTIFACT_REFRESH\n  No artifacts specified.\n"

    artifact = nodes[0]
    artifact_obj = manifest.get("nodes", {}).get(artifact, {})

    deps = artifact_obj.get("deps", [])
    summary = "DRY-RUN: Refresh artifact\n"
    summary += f"  Artifact: {artifact}\n"
    summary += f"  Dependencies: {', '.join(deps) if deps else '(none)'}\n"
    summary += f"  Will be marked not-stale.\n"

    return summary


def _find_parked_wts(descendants: List[str], manifest: Dict) -> List[Tuple[str, str]]:
    """
    Find worktrees that need to be parked for cascade.

    Args:
        descendants: List of descendant node names
        manifest: Manifest dict

    Returns:
        List of (wt_path, branch_name) tuples
    """
    parked_wts = []

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

            # Format: /path/to/wt SHA (branch)
            parts = line.split()
            if len(parts) < 2:
                continue

            wt_path = parts[0]
            # Extract branch from parentheses
            if "(" in line and ")" in line:
                branch = line[line.index("(") + 1:line.index(")")].strip()

                # Check if this branch is in descendants
                for desc in descendants:
                    node_ghost = manifest.get("nodes", {}).get(desc, {}).get("ghost")
                    if branch.endswith(f"/{desc}") or branch == node_ghost or branch == desc:
                        parked_wts.append((wt_path, branch))
                        break

    except Exception:
        pass

    return parked_wts


def _count_commits(node_obj: Dict) -> int:
    """
    Count commits in a node's range.

    Args:
        node_obj: Node dict from manifest

    Returns:
        Approximate commit count
    """
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


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) > 3:
        feature_name = sys.argv[1]
        intent_type = sys.argv[2]
        args_json = sys.argv[3]

        try:
            manifest = manifest_read(feature_name)
            args = json.loads(args_json)
            summary = dryrun_plan(intent_type, args, manifest)
            print(summary)
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: fleet_dryrun.py <feature> <intent> '<json_args>'", file=sys.stderr)
        sys.exit(1)
