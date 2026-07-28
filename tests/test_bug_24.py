"""
Unit tests for BUG-24: Decision re-parent with descendant fails on base_sha null.

BUG-24: When resolving a decision with a descendant that has base=decision_node,
cascade fails with "Node X has no base_sha to rebase from". The descendant's
base_sha is null because the decision node has no top-level ghost.

Fix: Pass promoted_option_ghost as both new_base AND old_base override for
decision-child rebases.
"""

import pytest
import sys
import os
import json
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_cascade import cascade, CascadeError
from fleet_manifest import manifest_read, manifest_write_atomic


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class TestBug24DecisionReParentWithDescendant:
    """BUG-24: Decision re-parent with descendant having base=decision_node."""

    def test_cascade_decision_descendant_with_null_base_sha(self, tmp_path, monkeypatch):
        """
        Cascade on a decision node with descendants should not fail on null base_sha.

        Scenario: d1 (decision) with child b1 (base=d1). When cascading with
        rebase_base_sha override (promoted option ghost), b1 should be rebased
        even though b1.range.base_sha is None (parent has no top-level ghost).

        Expected: Cascade succeeds, b1 rebased onto promoted option.
        Previous behavior: CascadeError("Node b1 has no base_sha to rebase from")
        """
        # Setup: Create a mock git repo
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # Initialize repo and set up branches
        os.chdir(repo_dir)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], check=True, capture_output=True)

        # Create initial commit on master
        (repo_dir / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], check=True, capture_output=True)
        master_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        main_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

        # Create decision node options (optA, optB branches)
        subprocess.run(["git", "checkout", "-b", "ghost/test/optA"], check=True, capture_output=True)
        (repo_dir / "optA.txt").write_text("Option A\n")
        subprocess.run(["git", "add", "optA.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Option A"], check=True, capture_output=True)
        optA_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

        subprocess.run(["git", "checkout", master_branch], check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "ghost/test/optB"], check=True, capture_output=True)
        (repo_dir / "optB.txt").write_text("Option B\n")
        subprocess.run(["git", "add", "optB.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Option B"], check=True, capture_output=True)
        optB_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

        # Create descendant (b1) based on a decision node
        # b1's history: main <- optA <- b1_commit
        subprocess.run(["git", "checkout", "ghost/test/optA"], check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "ghost/test/b1"], check=True, capture_output=True)
        (repo_dir / "b1.txt").write_text("B1 content\n")
        subprocess.run(["git", "add", "b1.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "B1 commit"], check=True, capture_output=True)
        b1_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

        # Create manifest with decision d1 and descendant b1
        # d1 is a decision (no top-level ghost, has options)
        # b1's base is d1, but there's no ghost/test/d1 branch
        # (d1's ghosts are under options: ghost/test/optA, ghost/test/optB)
        manifest = {
            "feature": "test",
            "canonical": "main",
            "experimental": True,
            "nodes": {
                "d1": {
                    "kind": "decision",
                    "desc": "Decision node",
                    "base": None,
                    "promoted": None,
                    "status": "unresolved",
                    "options": {
                        "optA": {
                            "ghost": "ghost/test/optA"
                        },
                        "optB": {
                            "ghost": "ghost/test/optB"
                        }
                    }
                },
                "b1": {
                    "kind": "feature",
                    "desc": "Descendant of decision",
                    "base": "d1",
                    "ghost": "ghost/test/b1",
                    "range": {
                        "base_sha": None,  # BUG-24: decision node has no ghost, so base_sha is null
                        "tip_sha": b1_sha
                    },
                    "status": "draft",
                    "assumes": {}
                }
            }
        }

        manifest_path = feature_dir / "manifest.yaml"
        import yaml
        manifest_path.write_text(yaml.dump(manifest))

        # Mock lock
        lock_obj = MagicMock()

        # Execute: Cascade with rebase_base_sha override (promote to optB)
        # This should rebase b1 from optA onto optB
        try:
            result = cascade(
                "test",
                "d1",
                dry_run=False,
                lock_obj=lock_obj,
                rebase_base_sha=optB_sha  # Promote decision to optB
            )

            # Assert: Should succeed (or at least not fail on base_sha null)
            # Note: May fail on conflict if the test setup has real git conflicts,
            # but should NOT fail with "has no base_sha" error
            assert result.get("status") in ("success", "conflict"), \
                f"Expected success or conflict, got: {result}"
            assert "has no base_sha" not in str(result.get("error", "")), \
                f"Should not fail with base_sha error: {result}"

        except CascadeError as e:
            # Check that it's NOT the specific "has no base_sha" error
            assert "has no base_sha" not in str(e), \
                f"BUG-24: Cascade failed with base_sha null error (not fixed): {e}"
            # Other cascade errors are acceptable (e.g., rebase conflicts)
            raise
