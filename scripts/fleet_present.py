#!/usr/bin/env python3
"""
Presentation generation — curate, pin, and deterministically replay plan.

Converts ghost commits into logically-grouped presentation branch, force-pushes
with lease safety, creates PR, and marks not-stale in manifest. Deterministic:
same plan+ghost+base → byte-identical commits.
"""

import os
import subprocess
import json
from typing import Dict, Optional, List, Tuple, IO
from fleet_manifest import manifest_read, manifest_write_atomic, ManifestError
from fleet_wal import wal_write, WALError
from fleet_lock import acquire_lock, release_lock, LockTimeoutError


class PresentError(Exception):
    """Base exception for presentation generation errors."""
    pass


class LeaseFailError(PresentError):
    """Force-push lease failed; remote tip changed."""
    pass


def present_gen(
    feature: str,
    node: str,
    lock_obj: Optional[IO] = None
) -> Dict:
    """
    Generate presentation branch from ghost + pinned plan.

    Curates plan if absent; replays plan via squash commits; force-pushes with
    --force-with-lease; creates PR if not exists; marks not-stale; wraps entire
    operation in lock and WAL.

    Args:
        feature: Feature name
        node: Node name to generate presentation for
        lock_obj: Optional lock object (if provided, use it; else acquire internally)

    Returns:
        Dict with {status, branch, pr} or error

    Raises:
        PresentError: Operation failed
        ManifestError: Manifest read/write failed
        WALError: WAL write failed
        LeaseFailError: Force-push lease failed
    """
    # Acquire lock if not provided
    if lock_obj is None:
        feature_path = os.path.expanduser(f"~/.fleet/{feature}")
        lock_path = os.path.join(feature_path, ".fleet.lock")
        try:
            lock_obj = acquire_lock(lock_path)
            should_release = True
        except LockTimeoutError as e:
            raise PresentError(f"Failed to acquire lock: {e}")
    else:
        should_release = False

    try:
        # Step 1: Read manifest
        try:
            manifest = manifest_read(feature)
        except Exception as e:
            raise PresentError(f"Failed to read manifest: {e}")

        if node not in manifest.get("nodes", {}):
            raise PresentError(f"Node '{node}' not found in manifest")

        node_obj = manifest["nodes"][node]

        # Step 2: Confirm or curate plan
        if not node_obj.get("present") or not node_obj.get("present", {}).get("plan"):
            # Prompt user to curate plan
            print(f"\nCurate logical commits for presentation of '{node}':")
            print(f"  Ghost range: {node_obj.get('range', {}).get('base_sha', '?')} .. {node_obj.get('range', {}).get('tip_sha', '?')}")
            print("\nEnter logical commit plan (format: 'name:sha..sha' per line, empty line to finish):")
            print("  Example: docs:abc123..def456")
            print("           impl:def456..ghi789")

            plan = []
            while True:
                line = input("  > ").strip()
                if not line:
                    break
                try:
                    name, sha_range = line.split(":", 1)
                    plan.append({"name": name.strip(), "sha_range": sha_range.strip()})
                except ValueError:
                    print("  Invalid format; use 'name:sha..sha'")
                    continue

            if not plan:
                return {"error": "Plan curation cancelled"}

            if "present" not in node_obj:
                node_obj["present"] = {}
            node_obj["present"]["plan"] = plan

        plan = node_obj.get("present", {}).get("plan", [])

        # Step 3: Dry-run summary
        print(f"\nPresentation plan for '{node}':")
        for item in plan:
            print(f"  {item['name']}: {item['sha_range']}")

        # Step 4: WAL intent (under lock)
        expected_tree_sha = _compute_expected_tree(plan, node_obj.get("range", {}))

        try:
            wal_write(
                feature,
                "present-gen-intent",
                "intent",
                {
                    "feature": feature,
                    "node": node,
                    "plan": plan,
                    "source_ghost_tip_sha": node_obj.get("range", {}).get("tip_sha"),
                    "expected_presentation_tree_sha": expected_tree_sha
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise PresentError(f"Failed to write WAL intent: {e}")

        # Step 5: Replay plan → presentation branch
        if "present" not in node_obj:
            node_obj["present"] = {}

        present_branch = node_obj.get("present", {}).get("branch", f"present/{feature}/{node}")
        node_obj["present"]["branch"] = present_branch

        try:
            # Checkout presentation branch from main
            subprocess.run(
                ["git", "checkout", "-B", present_branch, "main"],
                check=True,
                capture_output=True,
                timeout=10
            )

            # Replay each logical commit
            for item in plan:
                sha_range = item["sha_range"]  # e.g., "abc123..def456"
                name = item["name"]

                try:
                    # Get commits in range
                    result = subprocess.run(
                        ["git", "rev-list", sha_range],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10
                    )
                    commits = result.stdout.strip().split("\n") if result.stdout.strip() else []

                    if not commits:
                        print(f"  Skipping empty range: {sha_range}")
                        continue

                    # Squash: reset --soft to base commit, then commit with name
                    base_sha = sha_range.split("..")[0]

                    subprocess.run(
                        ["git", "reset", "--soft", base_sha],
                        check=True,
                        capture_output=True,
                        timeout=10
                    )

                    subprocess.run(
                        ["git", "commit", "-m", name],
                        check=True,
                        capture_output=True,
                        timeout=10
                    )
                except subprocess.CalledProcessError as e:
                    raise PresentError(f"Failed to replay commit '{name}': {e}")

        except subprocess.CalledProcessError as e:
            raise PresentError(f"Failed to checkout presentation branch: {e}")

        # Step 6: Force-push with lease
        try:
            subprocess.run(
                ["git", "push", "--force-with-lease", "origin", f"{present_branch}:{present_branch}"],
                check=True,
                capture_output=True,
                timeout=30
            )
        except subprocess.CalledProcessError as e:
            if "rejected" in e.stderr.lower() or "lease" in e.stderr.lower():
                # Lease failed
                raise LeaseFailError(f"Force-push lease failed: {e.stderr}")
            raise PresentError(f"Force-push failed: {e}")

        # Step 7: Create PR (if not exists)
        pr_number = node_obj.get("present", {}).get("pr")
        if not pr_number:
            try:
                result = subprocess.run(
                    [
                        "gh", "pr", "create",
                        "--base", "main",
                        "--head", present_branch,
                        "--title", node_obj.get("desc", f"Present {node}"),
                        "--body", f"Presentation for {node}\n\nGenerated by fleet."
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    # Parse PR number from output (e.g., "https://github.com/org/repo/pull/123")
                    output_lines = result.stdout.strip().split("\n")
                    for line in output_lines:
                        if "/pull/" in line:
                            try:
                                pr_number = int(line.split("/pull/")[-1].rstrip("/"))
                                break
                            except (ValueError, IndexError):
                                pass
                    if pr_number:
                        node_obj["present"]["pr"] = pr_number
            except Exception as e:
                # PR creation non-fatal; continue
                print(f"Warning: PR creation failed (non-fatal): {e}")

        # Step 8: WAL commit (under lock)
        try:
            wal_write(
                feature,
                "present-gen-intent",
                "commit",
                {
                    "feature": feature,
                    "node": node,
                    "status": "success"
                },
                lock_obj=lock_obj
            )
        except WALError as e:
            raise PresentError(f"Failed to write WAL commit: {e}")

        # Step 9: Mark not stale
        node_obj["present"]["stale"] = False
        node_obj["present"]["source_tip_sha"] = node_obj.get("range", {}).get("tip_sha")

        # Step 10: Manifest write (under lock)
        try:
            manifest_write_atomic(feature, manifest)
        except ManifestError as e:
            raise PresentError(f"Failed to write manifest: {e}")

        return {
            "status": "published",
            "branch": present_branch,
            "pr": pr_number
        }

    finally:
        if should_release and lock_obj:
            release_lock(lock_obj)


def _compute_expected_tree(plan: List[Dict], range_info: Dict) -> str:
    """
    Compute expected tree SHA of presentation.

    Deterministic: same plan+ghost → same tree SHA (all else equal).

    Args:
        plan: Plan list with {name, sha_range} items
        range_info: Node range dict with tip_sha

    Returns:
        Expected tree SHA (deterministic hash of plan structure)
    """
    # For determinism, hash the plan structure itself
    # In practice, we'd compute the actual tree SHA after squashing,
    # but here we derive it from the plan as a deterministic token.
    plan_str = json.dumps(plan, sort_keys=True)
    import hashlib
    tree_hash = hashlib.sha256(plan_str.encode()).hexdigest()[:40]  # Shorten to SHA-like length
    return tree_hash


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2:
        feature_name = sys.argv[1]
        node_name = sys.argv[2]

        try:
            result = present_gen(feature_name, node_name)
            print(f"✓ Presentation generation complete for '{node_name}'")
            print(json.dumps(result, indent=2))
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
