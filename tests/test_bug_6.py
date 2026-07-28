"""
Unit tests for BUG-6: Decision resolution self-deadlocks when cascading.

BUG-6 (Part 1 - Lock deadlock): When resolving a decision with descendants
that need re-parenting, decide_resolve acquires the lock, then calls cascade,
which tries to acquire the same lock again. Because flock is per-open-file-
description, the second acquire blocks indefinitely.

Fix: Thread the held lock_obj through cascade -> reconcile so they use the
same file descriptor instead of re-acquiring.

BUG-6 (Part 2 - Re-parent fails): When cascading decision descendants that
have base=decision_node, they can't be re-parented because the decision node
has no ghost branch to use as old_base.

Fix: Pass rebase_base_sha override to cascade, and use promoted option's ghost
as both new_base and (computed via merge-base) as old_base.
"""

import pytest
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, call

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_decision import decide_resolve, DecisionError


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class TestBug6LockThreading:
    """BUG-6 (Part 1): Lock must be threaded through cascade to avoid deadlock."""

    def test_decide_resolve_threads_lock_through_cascade(self, tmp_path, monkeypatch):
        """
        decide_resolve must pass the held lock to cascade via lock_obj parameter.

        When decide_resolve acquires a lock and then calls cascade, it should
        pass the lock_obj so cascade doesn't try to re-acquire the same lock
        (which would deadlock).
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FLEET_EXPERIMENTAL", "1")

        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            "feature": "test",
            "experimental": True,
            "canonical": "main",
            "nodes": {
                "d1": {
                    "kind": "decision",
                    "desc": "Decision",
                    "base": None,
                    "promoted": None,
                    "status": "unresolved",
                    "options": {
                        "optA": {"ghost": "ghost/test/optA"},
                        "optB": {"ghost": "ghost/test/optB"}
                    }
                },
                "b1": {
                    "kind": "feature",
                    "desc": "Descendant",
                    "base": "d1",
                    "ghost": "ghost/test/b1",
                    "range": {"base_sha": None, "tip_sha": "b1_tip"},
                    "status": "draft",
                    "assumes": {}
                }
            }
        }

        manifest_path = feature_dir / "manifest.yaml"
        import yaml
        manifest_path.write_text(yaml.dump(manifest))

        lock_obj_mock = MagicMock()

        with patch("fleet_decision.acquire_lock", return_value=lock_obj_mock):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade") as mock_cascade:
                    mock_cascade.return_value = {"status": "success", "rebased": ["b1"]}

                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic"):
                            # Execute
                            result = decide_resolve("test", "d1", "optB")

                            # Assert: cascade was called with lock_obj parameter
                            assert mock_cascade.called, "cascade not called"
                            call_kwargs = mock_cascade.call_args[1]
                            assert "lock_obj" in call_kwargs, "lock_obj not passed to cascade"
                            assert call_kwargs["lock_obj"] is lock_obj_mock, \
                                "cascade received wrong lock_obj (not the same instance)"

                            # Result should succeed
                            assert result["status"] == "resolved"

    def test_decide_resolve_passes_rebase_base_sha_to_cascade(self, tmp_path, monkeypatch):
        """
        decide_resolve must pass rebase_base_sha (promoted option's ghost) to cascade.

        For decision re-parent, descendants need an explicit rebase base since
        the decision node has no git base_sha (no top-level ghost).
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FLEET_EXPERIMENTAL", "1")

        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            "feature": "test",
            "experimental": True,
            "canonical": "main",
            "nodes": {
                "d1": {
                    "kind": "decision",
                    "desc": "Decision",
                    "base": None,
                    "promoted": None,
                    "status": "unresolved",
                    "options": {
                        "optA": {"ghost": "ghost/test/optA"},
                        "optB": {"ghost": "ghost/test/optB"}
                    }
                },
                "b1": {
                    "kind": "feature",
                    "desc": "Descendant with base=decision",
                    "base": "d1",
                    "ghost": "ghost/test/b1",
                    "range": {"base_sha": None, "tip_sha": "b1_tip"},
                    "status": "draft",
                    "assumes": {}
                }
            }
        }

        manifest_path = feature_dir / "manifest.yaml"
        import yaml
        manifest_path.write_text(yaml.dump(manifest))

        lock_obj_mock = MagicMock()
        chosen_ghost = "ghost/test/optB"

        with patch("fleet_decision.acquire_lock", return_value=lock_obj_mock):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade") as mock_cascade:
                    mock_cascade.return_value = {"status": "success", "rebased": ["b1"]}

                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic"):
                            # Execute: resolve to optB
                            result = decide_resolve("test", "d1", "optB")

                            # Assert: cascade called with rebase_base_sha
                            assert mock_cascade.called
                            call_kwargs = mock_cascade.call_args[1]
                            assert "rebase_base_sha" in call_kwargs, \
                                "rebase_base_sha not passed to cascade"
                            assert call_kwargs["rebase_base_sha"] == chosen_ghost, \
                                f"Expected rebase_base_sha={chosen_ghost}, got {call_kwargs['rebase_base_sha']}"

                            # Result should succeed
                            assert result["status"] == "resolved"
                            assert result["promoted"] == "optB"
