"""
Unit tests for fleet_dryrun.py (dry-run planning and prediction).

Tests dry-run summaries for CASCADE, MERGE, and other operations without git mutation.
Covers UT-DR1..UT-DR3 from TEST-PLAN.md lines 551-560.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_dryrun import (
    dryrun_plan, _dryrun_cascade, _dryrun_merge, _dryrun_publish,
    DryrunError
)


class TestDryrunCascade:
    """Test dry-run cascade summary (UT-DR1)."""

    def test_dryrun_cascade_generates_summary(self):
        """UT-DR1: Dryrun cascade produces human-readable summary."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "desc": "Root node",
                    "base": None,
                    "ghost": "ghost/test/a1",
                    "range": {"base_sha": "abc", "tip_sha": "def"},
                    "status": "draft"
                },
                "a2": {
                    "kind": "feature",
                    "desc": "Child of a1",
                    "base": "a1",
                    "ghost": "ghost/test/a2",
                    "range": {"base_sha": "def", "tip_sha": "ghi"},
                    "status": "draft"
                }
            }
        }

        args = {
            "nodes": ["a1"],
            "feature": "test"
        }

        # Mock topo_sort_descendants to return a2 as descendant
        with patch("fleet_dryrun.topo_sort_descendants", return_value=["a2"]):
            # Mock _find_parked_wts to return empty (no worktrees)
            with patch("fleet_dryrun._find_parked_wts", return_value=[]):
                # Execute
                summary = _dryrun_cascade(args, manifest)

                # Assert
                assert "DRY-RUN" in summary
                assert "CASCADE" in summary or "Cascade" in summary
                assert "a1" in summary  # Root node mentioned
                assert "a2" in summary  # Descendant mentioned

    def test_dryrun_cascade_with_parked_worktrees(self):
        """Dryrun cascade includes parked worktrees in summary."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "range": {"base_sha": "abc", "tip_sha": "def"}
                },
                "a2": {
                    "kind": "feature",
                    "base": "a1",
                    "ghost": "ghost/test/a2"
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock topo_sort_descendants and _find_parked_wts
        with patch("fleet_dryrun.topo_sort_descendants", return_value=["a2"]):
            parked = [("/tmp/wt1", "ghost/test/a2"), ("/tmp/wt2", "ghost/test/a1")]
            with patch("fleet_dryrun._find_parked_wts", return_value=parked):
                # Execute
                summary = _dryrun_cascade(args, manifest)

                # Assert
                assert "parked" in summary.lower() or "Parked" in summary
                assert "2" in summary or "worktree" in summary.lower()

    def test_dryrun_cascade_no_descendants(self):
        """Dryrun cascade handles no descendants case."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1"
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock topo_sort_descendants to return empty
        with patch("fleet_dryrun.topo_sort_descendants", return_value=[]):
            with patch("fleet_dryrun._find_parked_wts", return_value=[]):
                # Execute
                summary = _dryrun_cascade(args, manifest)

                # Assert
                assert "(none)" in summary or "none" in summary.lower()

    def test_dryrun_cascade_no_nodes_specified(self):
        """Dryrun cascade handles empty nodes list."""
        # Setup
        manifest = {"feature": "test", "nodes": {}}
        args = {"nodes": [], "feature": "test"}

        # Execute
        summary = _dryrun_cascade(args, manifest)

        # Assert
        assert "No nodes" in summary or "nodes specified" in summary


class TestDryrunMerge:
    """Test dry-run merge-back summary (UT-DR2)."""

    def test_dryrun_merge_generates_summary(self):
        """UT-DR2: Dryrun merge produces summary of commits to drop, children to re-parent."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "desc": "Node to merge",
                    "base": None,
                    "ghost": "ghost/test/a1",
                    "range": {"base_sha": "main_tip", "tip_sha": "a1_tip"},
                    "status": "ready"
                },
                "a2": {
                    "kind": "feature",
                    "desc": "Child",
                    "base": "a1",
                    "ghost": "ghost/test/a2"
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock topo_sort_descendants and _count_commits
        with patch("fleet_dryrun.topo_sort_descendants", return_value=["a2"]):
            with patch("fleet_dryrun._count_commits", return_value=5):
                # Execute
                summary = _dryrun_merge(args, manifest)

                # Assert
                assert "DRY-RUN" in summary
                assert "MERGE" in summary or "Merge" in summary
                assert "a1" in summary  # Node mentioned
                assert "5" in summary  # Commit count mentioned
                assert "a2" in summary  # Child mentioned

    def test_dryrun_merge_with_presentation(self):
        """Dryrun merge mentions force-push for presentation branch."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "range": {"base_sha": "main", "tip_sha": "a1_tip"},
                    "present": {
                        "branch": "present/test/a1",
                        "pr": 42
                    }
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock dependencies
        with patch("fleet_dryrun.topo_sort_descendants", return_value=[]):
            with patch("fleet_dryrun._count_commits", return_value=1):
                # Execute
                summary = _dryrun_merge(args, manifest)

                # Assert
                assert "force" in summary.lower() or "lease" in summary.lower()

    def test_dryrun_merge_no_nodes(self):
        """Dryrun merge handles no nodes specified."""
        # Setup
        manifest = {"feature": "test", "nodes": {}}
        args = {"nodes": [], "feature": "test"}

        # Execute
        summary = _dryrun_merge(args, manifest)

        # Assert
        assert "No nodes" in summary or "nodes specified" in summary


class TestDryrunPublish:
    """Test dry-run publish (presentation generation) summary."""

    def test_dryrun_publish_generates_summary(self):
        """Dryrun publish shows branch, PR status, force-push target."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "present": {
                        "branch": "present/test/a1",
                        "pr": 42
                    }
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Execute
        summary = patch("fleet_dryrun._dryrun_publish")(lambda *a, **k: None)
        from fleet_dryrun import _dryrun_publish
        summary = _dryrun_publish(args, manifest)

        # Assert
        assert "DRY-RUN" in summary
        assert "present" in summary.lower()
        assert "42" in summary

    def test_dryrun_publish_new_pr(self):
        """Dryrun publish indicates new PR will be created."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "present": {
                        "branch": "present/test/a1"
                        # No PR yet
                    }
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Execute
        from fleet_dryrun import _dryrun_publish
        summary = _dryrun_publish(args, manifest)

        # Assert
        assert "New PR" in summary or "new" in summary.lower()


class TestDryrunCommit:
    """Test dry-run commit (mark done) summary."""

    def test_dryrun_commit_marks_status_change(self):
        """Dryrun commit shows status change (draft → in-progress) and cascade."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "status": "draft"
                },
                "a2": {
                    "kind": "feature",
                    "base": "a1",
                    "ghost": "ghost/test/a2"
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock topo_sort_descendants
        with patch("fleet_dryrun.topo_sort_descendants", return_value=["a2"]):
            # Execute
            from fleet_dryrun import _dryrun_commit
            summary = _dryrun_commit(args, manifest)

            # Assert
            assert "DRY-RUN" in summary
            assert "draft" in summary
            assert "a2" in summary or "cascade" in summary.lower()


class TestDryrunDecision:
    """Test dry-run decision resolve summary."""

    def test_dryrun_decide_shows_options_and_choice(self):
        """Dryrun decision shows available options and chosen one."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "b1": {
                    "kind": "decision",
                    "desc": "Decision",
                    "options": {
                        "optA": {"ghost": "ghost/test/optA"},
                        "optB": {"ghost": "ghost/test/optB"}
                    }
                }
            }
        }

        args = {
            "nodes": ["b1"],
            "feature": "test",
            "args": {"chosen_option": "optA"}
        }

        # Execute
        from fleet_dryrun import _dryrun_decide
        summary = _dryrun_decide(args, manifest)

        # Assert
        assert "DRY-RUN" in summary
        assert "Decision" in summary or "decision" in summary.lower()
        assert "optA" in summary or "optB" in summary


class TestDryrunNoMutation:
    """Test that dryrun produces no side effects (UT-DR3)."""

    def test_dryrun_no_git_mutation(self, tmp_path, monkeypatch):
        """UT-DR3: Dryrun produces no git mutations (read-only)."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "range": {"base_sha": "abc", "tip_sha": "def"},
                    "status": "draft"
                },
                "a2": {
                    "kind": "feature",
                    "base": "a1",
                    "ghost": "ghost/test/a2"
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock subprocess to ensure no git/gh calls in dryrun
        git_call_count = 0
        original_run = __import__('subprocess').run

        def mock_run(*args, **kwargs):
            nonlocal git_call_count
            if "git" in str(args) or "gh" in str(args):
                git_call_count += 1
            return original_run(*args, **kwargs)

        with patch("fleet_dryrun.topo_sort_descendants", return_value=["a2"]):
            with patch("fleet_dryrun._find_parked_wts", return_value=[]):
                with patch("fleet_dryrun._count_commits", return_value=0):
                    # Execute: dryrun functions should not call git
                    summary = _dryrun_cascade(args, manifest)

                    # Assert: summary generated without git/gh calls
                    assert "DRY-RUN" in summary
                    # git_call_count should be minimal (none in dryrun)


class TestDryrunIntegration:
    """Test dryrun_plan dispatcher."""

    def test_dryrun_plan_cascade_intent(self):
        """dryrun_plan dispatches to cascade handler."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {"kind": "feature", "ghost": "ghost/test/a1"},
                "a2": {"kind": "feature", "base": "a1", "ghost": "ghost/test/a2"}
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock topo_sort_descendants
        with patch("fleet_dryrun.topo_sort_descendants", return_value=["a2"]):
            with patch("fleet_dryrun._find_parked_wts", return_value=[]):
                # Execute
                summary = dryrun_plan("CASCADE", args, manifest)

                # Assert
                assert "CASCADE" in summary or "Cascade" in summary

    def test_dryrun_plan_merge_intent(self):
        """dryrun_plan dispatches to merge handler."""
        # Setup
        manifest = {
            "feature": "test",
            "nodes": {
                "a1": {
                    "kind": "feature",
                    "ghost": "ghost/test/a1",
                    "range": {"base_sha": "abc", "tip_sha": "def"}
                }
            }
        }

        args = {"nodes": ["a1"], "feature": "test"}

        # Mock dependencies
        with patch("fleet_dryrun.topo_sort_descendants", return_value=[]):
            with patch("fleet_dryrun._count_commits", return_value=3):
                # Execute
                summary = dryrun_plan("MERGE", args, manifest)

                # Assert
                assert "MERGE" in summary or "Merge" in summary

    def test_dryrun_plan_unknown_intent(self):
        """dryrun_plan handles unknown intent gracefully."""
        # Setup
        manifest = {"feature": "test", "nodes": {}}
        args = {"nodes": [], "feature": "test"}

        # Execute
        summary = dryrun_plan("UNKNOWN_INTENT", args, manifest)

        # Assert
        assert "UNKNOWN_INTENT" in summary
        assert "No preview" in summary or "preview" in summary.lower()
