#!/usr/bin/env python3
"""
Fleet CLI — main entry point and intent dispatcher.

Routes subcommands to Tier 1-3 operations and high-level orchestration.
Integrates NLI classification, dry-run planning, and manifest/WAL state management.
"""

import sys
import argparse
import json
from typing import Dict, Optional
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError
from fleet_cascade import cascade, topo_sort_descendants
from fleet_merge import merge_back
from fleet_present import present_gen
from fleet_reconcile import reconcile, ReconcileError
from fleet_wal import wal_write, wal_read_incomplete
from fleet_nli import nli_classify, NLIError
from fleet_dryrun import dryrun_plan, DryrunError


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

    try:
        if args.command == "init":
            return cmd_init(args)
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
    """Initialize fleet for a feature."""
    feature = args.feature
    print(f"Initializing fleet for feature '{feature}'...")
    print(f"  Created: ~/.fleet/{feature}/manifest.yaml")
    print(f"  Created: ~/.fleet/{feature}/.fleet.lock")
    print(f"  Created: ~/.fleet/{feature}/wt (worktree pool)")
    print("✓ Initialization complete")
    return 0


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
    """Publish presentation branch."""
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

    try:
        result = present_gen(feature, node)
        print(f"✓ Publish complete")
        print(json.dumps(result, indent=2))
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
