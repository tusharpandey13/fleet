#!/usr/bin/env python3
"""
Fleet CLI — main entry point and intent dispatcher.

Routes subcommands to Tier 1-3 operations and high-level orchestration.
Integrates NLI classification, dry-run planning, and manifest/WAL state management.
"""

import sys
import os
import argparse
import json
import subprocess
from datetime import datetime
from typing import Dict, Optional, List
from fleet_manifest import manifest_read, manifest_write_atomic, manifest_validate, ManifestError
from fleet_cascade import cascade, topo_sort_descendants
from fleet_merge import merge_back, handle_lease_fail, LeaseFailError as MergeLeaseFailError
from fleet_present import present_gen, LeaseFailError as PresentLeaseFailError
from fleet_reconcile import reconcile, ReconcileError
from fleet_wal import wal_write, wal_read_incomplete, wal_replay, WALError
from fleet_nli import nli_classify, NLIError
from fleet_dryrun import dryrun_plan, DryrunError
from fleet_worktree import worktree_alloc, WorktreeDirtyError, WorktreePoolFullError, WorktreeError
from fleet_decision import decide_resolve, DecisionError


class FleetError(Exception):
    """Base exception for fleet CLI errors."""
    pass


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="fleet",
        description="Fleet — multi-node branch orchestration for git",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  fleet init --feature payments
  fleet status --feature payments
  fleet cascade --feature payments --node a1 --dry-run
  fleet merge --feature payments --node a1
  fleet publish --feature payments --node a1
  fleet classify "merge the auth node" --feature payments
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Common arguments
    def add_feature_arg(p):
        p.add_argument("--feature", "-f", required=True, help="Feature name")

    # init: Initialize fleet for a feature
    init_parser = subparsers.add_parser("init", help="Initialize fleet for a feature")
    add_feature_arg(init_parser)
    init_parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # plan: Create/update manifest DAG (Unit 1.1)
    plan_parser = subparsers.add_parser("plan", help="Create/update manifest DAG from specification")
    add_feature_arg(plan_parser)
    plan_parser.add_argument(
        "--nodes", nargs="+",
        help="Node specs: 'name:kind(parent)' e.g., 'a1:feature' 'a2:feature(a1)' 'd1:decision(a1)'"
    )
    plan_parser.add_argument(
        "--from-spec-file", type=str,
        help="Path to PLAN.md or YAML spec file"
    )
    plan_parser.add_argument("--dry-run", action="store_true", help="Preview only")
    plan_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # materialize: Create ghost branches (Unit 1.2)
    materialize_parser = subparsers.add_parser("materialize", help="Create ghost branches for manifest nodes")
    add_feature_arg(materialize_parser)
    materialize_parser.add_argument("--dry-run", action="store_true", help="Preview only")
    materialize_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # work: Allocate worktree for a node (Unit 2.1)
    work_parser = subparsers.add_parser("work", help="Allocate worktree for a node")
    add_feature_arg(work_parser)
    work_parser.add_argument("--node", "-n", required=True, help="Node name")
    work_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # resolve: Resolve decision node (Unit 2.2)
    resolve_parser = subparsers.add_parser("resolve", help="Resolve decision node by promoting option")
    add_feature_arg(resolve_parser)
    resolve_parser.add_argument("--decision", "-d", required=True, help="Decision node name")
    resolve_parser.add_argument("--option", "-o", required=True, help="Option to promote")
    resolve_parser.add_argument("--dry-run", action="store_true", help="Preview only")
    resolve_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # refresh: Refresh artifact staleness (Unit 2.3)
    refresh_parser = subparsers.add_parser("refresh", help="Refresh artifact by running hook")
    add_feature_arg(refresh_parser)
    refresh_parser.add_argument("--artifact", "-a", required=True, help="Artifact node name")
    refresh_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # status: Show feature DAG and node states
    status_parser = subparsers.add_parser("status", help="Show feature status and DAG")
    add_feature_arg(status_parser)
    status_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # cascade: Cascade rebase
    cascade_parser = subparsers.add_parser("cascade", help="Cascade rebase descendants")
    add_feature_arg(cascade_parser)
    cascade_parser.add_argument("--node", "-n", required=True, help="Root node name")
    cascade_parser.add_argument("--dry-run", action="store_true", help="Preview only")

    # merge: Merge-back via --onto
    merge_parser = subparsers.add_parser("merge", help="Merge-back (squash cleanup)")
    add_feature_arg(merge_parser)
    merge_parser.add_argument("--node", "-n", required=True, help="Node to merge")
    merge_parser.add_argument(
        "--mode", choices=["individual", "batch"], default="individual",
        help="Merge mode (default: individual)"
    )
    merge_parser.add_argument("--dry-run", action="store_true", help="Preview only")

    # publish: Generate presentation + push PR
    publish_parser = subparsers.add_parser("publish", help="Publish presentation branch")
    add_feature_arg(publish_parser)
    publish_parser.add_argument("--node", "-n", required=True, help="Node to publish")
    publish_parser.add_argument("--dry-run", action="store_true", help="Preview only")

    # reconcile: Reconcile state from git
    recon_parser = subparsers.add_parser("reconcile", help="Reconcile manifest with git")
    add_feature_arg(recon_parser)
    recon_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # classify: NLI intent classification
    classify_parser = subparsers.add_parser("classify", help="Classify natural language intent")
    classify_parser.add_argument("utterance", help="User utterance")
    classify_parser.add_argument("--feature", "-f", help="Feature name")
    classify_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # dryrun: Dry-run planner
    dryrun_parser = subparsers.add_parser("dryrun", help="Preview operation impact")
    add_feature_arg(dryrun_parser)
    dryrun_parser.add_argument("--intent", required=True, help="Operation intent")
    dryrun_parser.add_argument("--nodes", nargs="+", help="Node names")
    dryrun_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Unit 1.3: WAL preflight for destructive commands
    destructive_commands = {"cascade", "merge", "publish", "resolve", "refresh", "materialize"}
    if hasattr(args, "command") and args.command in destructive_commands and hasattr(args, "feature"):
        feature = args.feature
        try:
            orphans = wal_read_incomplete(feature)
            if orphans:
                print(f"⚠ Found {len(orphans)} orphaned WAL intents:")
                for orphan in orphans:
                    print(f"  - {orphan.get('op_type')} at {orphan.get('timestamp')}")

                choice = input("Action: (r)eplay, (b)ackup-rollback, (i)gnore? ").strip().lower()
                if choice == "r":
                    for orphan in orphans:
                        try:
                            wal_replay(feature, orphan, "replay")
                        except WALError as e:
                            print(f"Failed to replay {orphan.get('op_type')}: {e}")
                elif choice == "b":
                    for orphan in orphans:
                        try:
                            wal_replay(feature, orphan, "rollback")
                        except WALError as e:
                            print(f"Failed to rollback {orphan.get('op_type')}: {e}")
                # else choice == "i": continue without action
        except Exception:
            # Silent skip if WAL read fails (not fatal; continue with operation)
            pass

    try:
        if args.command == "init":
            return cmd_init(args)
        elif args.command == "plan":
            return cmd_plan(args)
        elif args.command == "materialize":
            return cmd_materialize(args)
        elif args.command == "work":
            return cmd_work(args)
        elif args.command == "resolve":
            return cmd_resolve(args)
        elif args.command == "refresh":
            return cmd_refresh(args)
        elif args.command == "status":
            return cmd_status(args)
        elif args.command == "cascade":
            return cmd_cascade(args)
        elif args.command == "merge":
            return cmd_merge(args)
        elif args.command == "publish":
            return cmd_publish(args)
        elif args.command == "reconcile":
            return cmd_reconcile(args)
        elif args.command == "classify":
            return cmd_classify(args)
        elif args.command == "dryrun":
            return cmd_dryrun(args)
        else:
            parser.print_help()
            return 1

    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        return 1


def cmd_init(args) -> int:
    """Initialize fleet for a feature: create manifest, lock file, and worktree pool dir.

    Writes a schema-valid empty manifest (empty nodes dict) via manifest_write_atomic
    under ~/.fleet/<feature>/. Idempotent: re-running on an existing feature does not
    clobber the manifest; it warns and exits 0.
    """
    feature = args.feature
    feature_dir = os.path.expanduser(f"~/.fleet/{feature}")
    manifest_file = os.path.join(feature_dir, "manifest.yaml")
    lock_file = os.path.join(feature_dir, ".fleet.lock")
    pool_dir = os.path.join(feature_dir, "wt")

    # Idempotency guard: never clobber an existing manifest.
    if os.path.exists(manifest_file):
        # Ensure lock file and pool dir exist (harmless to re-create).
        os.makedirs(pool_dir, exist_ok=True)
        if not os.path.exists(lock_file):
            open(lock_file, "a").close()
        if getattr(args, "json", False):
            print(json.dumps({"status": "ok", "already_initialized": True,
                              "manifest": manifest_file}, indent=2))
        else:
            print(f"Fleet already initialized for feature '{feature}' (manifest exists); nothing to do.")
        return 0

    # Build a schema-valid empty manifest (required: feature, nodes).
    manifest = {
        "feature": feature,
        "canonical": "main",
        "nodes": {},
        "edges": {},
    }

    try:
        os.makedirs(pool_dir, exist_ok=True)
        # manifest_write_atomic creates parent dirs and validates schema before writing.
        manifest_write_atomic(feature, manifest)
        # Create the lock file used for single-writer discipline.
        if not os.path.exists(lock_file):
            open(lock_file, "a").close()
    except ManifestError as e:
        print(f"FLEET_003: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Initialization failed: {e}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({"status": "ok", "already_initialized": False,
                          "manifest": manifest_file}, indent=2))
    else:
        print(f"Initialized fleet for feature '{feature}':")
        print(f"  Created: {manifest_file}")
        print(f"  Created: {lock_file}")
        print(f"  Created: {pool_dir} (worktree pool)")
    return 0


def cmd_plan(args) -> int:
    """Create/update manifest DAG from specification (Unit 1.1)."""
    feature = args.feature
    nodes_args = getattr(args, "nodes", None)
    spec_file = getattr(args, "from_spec_file", None)
    dry_run = getattr(args, "dry_run", False)

    # Parse node specs or load from spec_file
    manifest = _parse_plan_spec(feature, nodes_args or spec_file)

    # Validate DAG for cycles
    errors = _validate_dag_cycles(manifest)
    if errors:
        print(f"FLEET_002: DAG has cycle: {errors}", file=sys.stderr)
        return 1

    # Validate schema
    errors = manifest_validate(manifest)
    if errors:
        print(f"FLEET_003: Schema invalid: {', '.join(errors)}", file=sys.stderr)
        return 1

    # Dry-run: show planned nodes + edges
    if dry_run:
        _print_plan_summary(manifest)
        return 0

    # Write manifest atomically
    try:
        manifest_write_atomic(feature, manifest)
        if args.json:
            print(json.dumps({"status": "ok", "manifest": manifest}, indent=2))
        else:
            print(f"✓ Manifest written for feature '{feature}'")
            print(f"  Nodes: {list(manifest.get('nodes', {}).keys())}")
            print(f"  Edges: {manifest.get('edges', {})}")
        return 0
    except ManifestError as e:
        print(f"FLEET_001: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Plan failed: {e}", file=sys.stderr)
        return 1


def cmd_materialize(args) -> int:
    """Create ghost branches for all manifest nodes from main (Unit 1.2)."""
    feature = args.feature
    dry_run = getattr(args, "dry_run", False)

    # Read manifest
    try:
        manifest = manifest_read(feature)
    except FileNotFoundError:
        print(f"FLEET_001: Manifest not found for feature '{feature}'", file=sys.stderr)
        return 1
    except ManifestError as e:
        print(f"FLEET_003: {e}", file=sys.stderr)
        return 1

    nodes = manifest.get("nodes", {})
    if not nodes:
        print("No nodes in manifest to materialize")
        return 0

    # Collect ghosts: plan which to create, which exist
    existing_ghosts = _get_existing_ghosts(feature)
    to_create = []
    skipped = []
    for node_name, node_obj in nodes.items():
        ghost_branch = f"ghost/{feature}/{node_name}"
        if ghost_branch in existing_ghosts:
            skipped.append(ghost_branch)
        else:
            to_create.append((node_name, ghost_branch))

    # Dry-run: show plan
    if dry_run:
        print(f"Would create: {len(to_create)} ghosts")
        for node_name, ghost in to_create:
            print(f"  {ghost}")
        print(f"Would skip: {len(skipped)} existing")
        return 0

    # Create ghosts + wire edges
    wired_edges = {}
    try:
        for node_name, ghost_branch in to_create:
            _create_ghost_branch(feature, node_name, ghost_branch)

        # Wire edges: set base_branch per manifest
        edges = manifest.get("edges", {})
        for child_node, parents in edges.items():
            if parents:
                base_node = parents[0]  # first parent is base
                base_ghost = f"ghost/{feature}/{base_node}"
                try:
                    base_sha = subprocess.check_output(
                        ["git", "rev-parse", base_ghost],
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=5
                    ).strip()
                except Exception:
                    base_sha = "unknown"

                try:
                    child_ghost = f"ghost/{feature}/{child_node}"
                    child_sha = subprocess.check_output(
                        ["git", "rev-parse", child_ghost],
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=5
                    ).strip()
                except Exception:
                    child_sha = "unknown"

                manifest["nodes"][child_node]["range"] = {
                    "base_sha": base_sha,
                    "base_branch": base_ghost,
                    "tip_sha": child_sha
                }
                wired_edges[child_node] = {"base": base_node, "base_sha": base_sha}

        # Write manifest + create pool dir
        manifest_write_atomic(feature, manifest)
        _create_pool_dir(feature)

        if args.json:
            print(json.dumps({
                "status": "ok",
                "created": len(to_create),
                "skipped": len(skipped),
                "wired_edges": wired_edges
            }, indent=2))
        else:
            print(f"✓ Materialized {len(to_create)} ghosts")
        return 0
    except Exception as e:
        print(f"Materialize failed: {e}", file=sys.stderr)
        return 1


def cmd_work(args) -> int:
    """Allocate worktree for a node and print path (Unit 2.1)."""
    feature = args.feature
    node_name = args.node

    # Read manifest
    try:
        manifest = manifest_read(feature)
    except FileNotFoundError:
        print(f"FLEET_001: Manifest not found for feature '{feature}'", file=sys.stderr)
        return 1

    # Verify node exists (FLEET_060 preflight)
    nodes = manifest.get("nodes", {})
    if node_name not in nodes:
        print(f"FLEET_060: Node '{node_name}' not found in manifest", file=sys.stderr)
        return 1

    node_obj = nodes[node_name]
    ghost_branch = node_obj.get("ghost", f"ghost/{feature}/{node_name}")

    # Allocate worktree
    try:
        wt_path = worktree_alloc(feature, {
            "name": node_name,
            "ghost": ghost_branch
        })

        # BUG-23 fix: persist node.worktree to manifest
        node_obj["worktree"] = wt_path
        manifest_write_atomic(feature, manifest)

        if args.json:
            print(json.dumps({
                "status": "ok",
                "worktree_path": wt_path,
                "node": node_name,
                "message": f"cd {wt_path}"
            }, indent=2))
        else:
            print(f"Allocated worktree for '{node_name}':")
            print(f"{wt_path}")  # User can copy/paste for cd
        return 0
    except WorktreeDirtyError as e:
        print(f"FLEET_040: {e}", file=sys.stderr)
        return 1
    except WorktreePoolFullError as e:
        print(f"FLEET_041: {e}", file=sys.stderr)
        return 1
    except WorktreeError as e:
        print(f"FLEET_001: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Work allocation failed: {e}", file=sys.stderr)
        return 1


def cmd_resolve(args) -> int:
    """Resolve decision node by promoting option (Unit 2.2, experimental gate)."""
    feature = args.feature
    decision = args.decision
    option = args.option
    dry_run = getattr(args, "dry_run", False)

    # Read manifest
    try:
        manifest = manifest_read(feature)
    except FileNotFoundError:
        print(f"FLEET_001: Manifest not found for feature '{feature}'", file=sys.stderr)
        return 1

    # FLEET_060 preflight: verify decision node exists
    nodes = manifest.get("nodes", {})
    if decision not in nodes:
        print(f"FLEET_060: Decision node '{decision}' not found", file=sys.stderr)
        return 1

    decision_obj = nodes[decision]
    if decision_obj.get("kind") != "decision":
        print(f"FLEET_060: Node '{decision}' is not a decision", file=sys.stderr)
        return 1

    # Check experimental gate (FLEET_099)
    experimental = manifest.get("experimental", False)
    experimental_env = os.environ.get("FLEET_EXPERIMENTAL", "0") == "1"
    if not (experimental or experimental_env):
        print(f"FLEET_099: Decision resolve requires FLEET_EXPERIMENTAL=1", file=sys.stderr)
        print(f"  Enable with: export FLEET_EXPERIMENTAL=1", file=sys.stderr)
        return 1

    # Validate option exists
    if option not in decision_obj.get("options", {}):
        print(f"Invalid option '{option}' for decision '{decision}'", file=sys.stderr)
        return 1

    # Dry-run: show plan (call decide_resolve with dry_run=True)
    if dry_run:
        try:
            result = decide_resolve(feature, decision, option, dry_run=True)
            if result.get("status") == "plan_preview":
                print("Rebase plan:")
                print(json.dumps(result, indent=2))
                return 0
            else:
                print(f"Dry-run failed: unexpected status {result.get('status')}", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"Dry-run failed: {e}", file=sys.stderr)
            return 1

    # Execute resolve
    try:
        result = decide_resolve(feature, decision, option, dry_run=False)

        if result.get("status") == "resolved":
            print(f"✓ Resolved decision '{decision}'")
            print(f"  Promoted: {result.get('promoted')}")
            print(f"  Archived: {result.get('archived', [])}")
            if result.get("rebuild_prompts"):
                print(f"  Rebuild prompts: {result.get('rebuild_prompts', [])}")

            if args.json:
                print(json.dumps(result, indent=2))
            return 0
        else:
            print(f"Resolve failed: unexpected status {result.get('status')}", file=sys.stderr)
            return 1
    except DecisionError as e:
        print(f"FLEET_010: Resolve failed (conflict or rebase error): {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"FLEET_010: Resolve failed: {e}", file=sys.stderr)
        return 1


def cmd_refresh(args) -> int:
    """Refresh artifact staleness by running hook and updating deps_sha (Unit 2.3)."""
    feature = args.feature
    artifact = args.artifact

    # Read manifest
    try:
        manifest = manifest_read(feature)
    except FileNotFoundError:
        print(f"FLEET_001: Manifest not found for feature '{feature}'", file=sys.stderr)
        return 1

    # Verify artifact exists (FLEET_060 preflight)
    nodes = manifest.get("nodes", {})
    if artifact not in nodes:
        print(f"FLEET_060: Artifact '{artifact}' not found", file=sys.stderr)
        return 1

    artifact_obj = nodes[artifact]
    if artifact_obj.get("kind") != "artifact":
        print(f"FLEET_060: Node '{artifact}' is not an artifact", file=sys.stderr)
        return 1

    # Get hook + dependencies
    hook = artifact_obj.get("hook")
    if not hook:
        print(f"Artifact '{artifact}' has no hook defined", file=sys.stderr)
        return 1

    deps = artifact_obj.get("dependencies", [])

    # Execute hook in worktree (or feature dir)
    try:
        wt_path = artifact_obj.get("worktree")
        if not wt_path:
            wt_path = os.path.expanduser(f"~/.fleet/{feature}")

        # Run hook with timeout (default 60s)
        timeout = manifest.get("artifact_hook_timeout", 60)
        result = subprocess.run(
            hook,
            shell=True,
            cwd=wt_path,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            print(f"Hook failed: {result.stderr}", file=sys.stderr)
            return 1

        # Update deps_sha snapshot
        deps_sha = {}
        for dep in deps:
            if dep in nodes:
                dep_tip = nodes[dep].get("range", {}).get("tip_sha", "unknown")
                deps_sha[dep] = dep_tip

        artifact_obj["deps_sha"] = deps_sha
        artifact_obj["stale"] = False
        artifact_obj["refresh_timestamp"] = datetime.utcnow().isoformat() + "Z"

        # Write manifest
        manifest_write_atomic(feature, manifest)

        print(f"✓ Refreshed artifact '{artifact}'")
        print(f"  Hook: {hook}")
        print(f"  Updated deps: {list(deps_sha.keys())}")

        if args.json:
            print(json.dumps({
                "status": "ok",
                "artifact": artifact,
                "hook": hook,
                "deps_updated": deps_sha,
                "stale": False
            }, indent=2))
        return 0
    except subprocess.TimeoutExpired:
        print(f"Hook timeout (>{timeout}s)", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Refresh failed: {e}", file=sys.stderr)
        return 1


def cmd_status(args) -> int:
    """Show feature status and DAG."""
    feature = args.feature
    as_json = args.json

    try:
        manifest = manifest_read(feature)
    except ManifestError as e:
        print(f"✗ Failed to read manifest: {e}", file=sys.stderr)
        return 1

    try:
        manifest, unobserved, divergences = reconcile(feature, manifest)
    except ReconcileError as e:
        print(f"⚠ Reconciliation warning: {e}", file=sys.stderr)

    if as_json:
        output = {
            "feature": feature,
            "nodes": {}
        }
        for node_name, node in manifest.get("nodes", {}).items():
            output["nodes"][node_name] = {
                "kind": node.get("kind"),
                "status": node.get("status"),
                "ghost": node.get("ghost"),
                "base": node.get("base"),
                "range": node.get("range", {})
            }
        print(json.dumps(output, indent=2))
    else:
        print(f"Feature: {feature}")
        print("Nodes:")
        for node_name, node in manifest.get("nodes", {}).items():
            if not isinstance(node, dict):
                continue
            status_str = f"[{node.get('status', 'draft')}]"
            kind_str = f"({node.get('kind', 'feature')})"
            print(f"  {node_name} {kind_str} {status_str}")
            if node.get("ghost"):
                print(f"    ghost: {node.get('ghost')}")
            if node.get("desc"):
                print(f"    desc: {node.get('desc')}")

    if unobserved:
        print(f"⚠ Unobserved writes: {', '.join(unobserved)}")
    if divergences:
        print(f"⚠ DAG divergences detected:")
        for d in divergences:
            print(f"  {d['node']}: {d['reason']}")

    return 0


def cmd_cascade(args) -> int:
    """Cascade rebase descendants."""
    feature = args.feature
    node = args.node
    dry_run = args.dry_run

    try:
        manifest = manifest_read(feature)
    except ManifestError as e:
        print(f"✗ Failed to read manifest: {e}", file=sys.stderr)
        return 1

    if dry_run:
        # Show dry-run summary
        summary = dryrun_plan(
            "CASCADE",
            {"nodes": [node]},
            manifest
        )
        print(summary)
        return 0

    # Confirm before executing
    descendants = topo_sort_descendants(manifest, node)
    if not descendants:
        print(f"No descendants to cascade for node {node}")
        return 0

    response = input(
        f"Cascade {node} and rebase {len(descendants)} descendants? (yes/no): "
    ).strip()
    if response != "yes":
        print("Cancelled.")
        return 0

    try:
        result = cascade(feature, node, dry_run=False)
        print(f"✓ Cascade complete")
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(f"✗ Cascade failed: {e}", file=sys.stderr)
        return 1


def cmd_merge(args) -> int:
    """Merge-back (squash cleanup)."""
    feature = args.feature
    node = args.node
    mode = args.mode
    dry_run = args.dry_run

    try:
        manifest = manifest_read(feature)
    except ManifestError as e:
        print(f"✗ Failed to read manifest: {e}", file=sys.stderr)
        return 1

    if dry_run:
        # Show dry-run summary
        summary = dryrun_plan(
            "MERGE",
            {"nodes": [node]},
            manifest
        )
        print(summary)
        return 0

    # Confirm before executing
    response = input(
        f"Merge-back {node} (mode={mode})? (yes/no): "
    ).strip()
    if response != "yes":
        print("Cancelled.")
        return 0

    try:
        result = merge_back(feature, node, merge_mode=mode)
        print(f"✓ Merge complete")
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(f"✗ Merge failed: {e}", file=sys.stderr)
        return 1


def cmd_publish(args) -> int:
    """Publish presentation branch (Unit 3.1: BUG-13 lease-fail wiring)."""
    feature = args.feature
    node = args.node
    dry_run = args.dry_run

    try:
        manifest = manifest_read(feature)
    except ManifestError as e:
        print(f"✗ Failed to read manifest: {e}", file=sys.stderr)
        return 1

    if dry_run:
        # Show dry-run summary
        summary = dryrun_plan(
            "PUBLISH",
            {"nodes": [node]},
            manifest
        )
        print(summary)
        return 0

    # Confirm before executing
    response = input(f"Publish {node}? (yes/no): ").strip()
    if response != "yes":
        print("Cancelled.")
        return 0

    # Unit 3.1: BUG-13 lease-fail recovery wiring
    try:
        result = present_gen(feature, node)
        print(f"✓ Publish complete")
        print(json.dumps(result, indent=2))
        return 0
    except (MergeLeaseFailError, PresentLeaseFailError) as e:
        # BUG-13 fix: dispatch lease-fail recovery (not generic exception)
        print(f"⚠ Force-push lease rejected (remote changed)")
        try:
            result = handle_lease_fail(feature, node)
            status = result.get("status")
            if status == "resolved":
                print(f"✓ {result.get('message')}")
                if args.json:
                    print(json.dumps(result, indent=2))
                return 0
            elif status == "manual":
                print(f"⚠ {result.get('message')}", file=sys.stderr)
                print(f"  User must manually resolve on GitHub", file=sys.stderr)
                return 1
            elif status == "cancelled":
                print(f"User cancelled lease recovery", file=sys.stderr)
                return 1
            else:
                print(f"✗ Lease recovery failed: unknown status {status}", file=sys.stderr)
                return 1
        except Exception as recovery_error:
            print(f"✗ Lease recovery failed: {recovery_error}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"✗ Publish failed: {e}", file=sys.stderr)
        return 1


def cmd_reconcile(args) -> int:
    """Reconcile manifest with git state."""
    feature = args.feature
    as_json = args.json

    try:
        manifest, unobserved, divergences = reconcile(feature)
    except ReconcileError as e:
        print(f"✗ Reconciliation failed: {e}", file=sys.stderr)
        return 1

    if as_json:
        output = {
            "feature": feature,
            "unobserved_writes": unobserved,
            "divergences": divergences
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"✓ Reconciliation complete for {feature}")
        if unobserved:
            print(f"  Unobserved writes: {', '.join(unobserved)}")
        if divergences:
            print(f"  Divergences: {len(divergences)}")
            for d in divergences:
                print(f"    - {d['node']}: {d['reason']}")

    return 0


def cmd_classify(args) -> int:
    """Classify natural language intent."""
    utterance = args.utterance
    feature = args.feature
    as_json = args.json

    try:
        result = nli_classify(utterance, feature)
    except NLIError as e:
        print(f"✗ Classification failed: {e}", file=sys.stderr)
        return 1

    if as_json:
        # Convert nodes list to node names for JSON output
        output = result.copy()
        output["nodes"] = result.get("nodes", [])
        print(json.dumps(output, indent=2))
    else:
        print(f"Intent: {result['intent']} (confidence: {result['confidence']:.1%})")
        print(f"Feature: {result['feature']}")
        if result.get("nodes"):
            print(f"Nodes: {', '.join(result['nodes'])}")
        if result.get("requires_confirmation"):
            print("⚠ Requires user confirmation (destructive operation)")

    return 0


def cmd_dryrun(args) -> int:
    """Preview operation impact."""
    feature = args.feature
    # dryrun_plan dispatches on canonical UPPERCASE intent names (CASCADE,
    # MERGE, PUBLISH, ...); normalize the CLI-provided intent so lowercase
    # input like "--intent cascade" produces a real preview instead of the
    # "(No preview available)" fallback.
    intent = args.intent.upper()
    nodes = args.nodes or []
    as_json = args.json

    try:
        manifest = manifest_read(feature)
    except ManifestError as e:
        print(f"✗ Failed to read manifest: {e}", file=sys.stderr)
        return 1

    try:
        summary = dryrun_plan(intent, {"nodes": nodes}, manifest)
    except DryrunError as e:
        print(f"✗ Dryrun failed: {e}", file=sys.stderr)
        return 1

    if as_json:
        output = {
            "intent": intent,
            "nodes": nodes,
            "summary": summary
        }
        print(json.dumps(output, indent=2))
    else:
        print(summary)

    return 0


# ============================================================================
# Helper Functions
# ============================================================================

def _parse_plan_spec(feature: str, nodes_or_spec_file) -> Dict:
    """
    Parse node specs into manifest structure.

    Args:
        feature: Feature name
        nodes_or_spec_file: List of "node:kind(parent)" specs or path to spec file

    Returns:
        Manifest dict with nodes, edges, feature metadata
    """
    manifest = {
        "feature": feature,
        "nodes": {},
        "edges": {}
    }

    if isinstance(nodes_or_spec_file, list):
        # Parse CLI specs: ["a1:feature", "a2:feature(a1)", "d1:decision(a1)"]
        for spec in nodes_or_spec_file:
            node_name, kind_and_parent = spec.split(":", 1)
            kind = kind_and_parent
            parent = None

            # Extract parent if present: "feature(a1)" → kind="feature", parent="a1"
            if "(" in kind_and_parent and ")" in kind_and_parent:
                kind = kind_and_parent.split("(")[0]
                parent = kind_and_parent.split("(")[1].rstrip(")")

            node_obj = {
                "kind": kind,
                "status": "draft"
            }
            # Decision nodes require an 'options' dict to pass schema validation.
            # Seed an empty dict here; options are populated later (via materialize
            # of option ghosts or manual manifest edit).
            if kind == "decision":
                node_obj["options"] = {}
            manifest["nodes"][node_name] = node_obj

            if parent:
                manifest["edges"][node_name] = [parent]
    else:
        # Load from spec file (would parse PLAN.md or YAML)
        # For now, minimal impl: assume file doesn't exist or is YAML
        raise ValueError(f"Spec file loading not yet implemented")

    return manifest


def _validate_dag_cycles(manifest: Dict) -> List[str]:
    """
    Detect cycles in DAG via edges.

    Returns:
        List of error messages (empty if no cycles)
    """
    errors = []
    edges = manifest.get("edges", {})
    visited = set()
    rec_stack = set()

    def has_cycle(node, visited, rec_stack):
        visited.add(node)
        rec_stack.add(node)

        for parent in edges.get(node, []):
            if parent not in visited:
                if has_cycle(parent, visited, rec_stack):
                    return True
            elif parent in rec_stack:
                return True

        rec_stack.remove(node)
        return False

    for node in manifest.get("nodes", {}):
        if node not in visited:
            if has_cycle(node, visited, rec_stack):
                errors.append(f"Cycle detected involving {node}")

    return errors


def _print_plan_summary(manifest: Dict) -> None:
    """Print dry-run summary of plan."""
    print("Plan summary:")
    print(f"  Feature: {manifest.get('feature')}")
    print(f"  Nodes: {list(manifest.get('nodes', {}).keys())}")
    edges = manifest.get("edges", {})
    if edges:
        print("  Edges (child → parents):")
        for child, parents in edges.items():
            print(f"    {child} → {parents}")


def _get_existing_ghosts(feature: str) -> set:
    """Get set of existing ghost branches for feature."""
    try:
        result = subprocess.run(
            ["git", "branch", "-a"],
            capture_output=True,
            text=True,
            timeout=5
        )
        branches = result.stdout.strip().split("\n")
        prefix = f"ghost/{feature}/"
        return {b.strip().lstrip("* ") for b in branches if prefix in b}
    except Exception:
        return set()


def _create_ghost_branch(feature: str, node_name: str, ghost_branch: str) -> None:
    """Create ghost branch from main without moving HEAD.

    Uses `git branch` (not `git checkout -b`) so the current HEAD/worktree
    stays on its branch. Leaving HEAD checked out on a ghost branch would make
    a later `git worktree add <path> <ghost>` fail with
    'already used by worktree'.
    """
    try:
        subprocess.run(
            ["git", "branch", ghost_branch, "main"],
            check=True,
            capture_output=True,
            timeout=10
        )
    except subprocess.CalledProcessError as e:
        raise FleetError(f"Failed to create ghost branch {ghost_branch}: {e}")


def _create_pool_dir(feature: str) -> None:
    """Create worktree pool directory."""
    feature_path = os.path.expanduser(f"~/.fleet/{feature}")
    pool_root = os.path.join(feature_path, "wt")
    os.makedirs(pool_root, exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
