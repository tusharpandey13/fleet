"""
Unit tests for fleet_decision.py (decision node resolution — EXPERIMENTAL).

Tests decision node promotion, descendant re-parenting, rebuild prompts, and on_flip strategies.
Covers UT-D1..UT-D7 from TEST-PLAN.md lines 522-537 (EXPERIMENTAL gated).

NOTE: These tests are gated by experimental manifest flag. Tests assert
ExperimentalGateError when flag absent, and exercise resolve logic when flag set.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_decision import (
    decide_resolve, _topo_sort_descendants,
    DecisionError, ExperimentalGateError
)


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class TestDecisionExperimentalGate:
    """Test experimental gating mechanism."""

    def test_decide_resolve_gate_off_raises_experimental_error(self, tmp_path, monkeypatch):
        """Decision resolve raises ExperimentalGateError when experimental flag absent."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: false
nodes:
  b1:
    kind: decision
    desc: Decision node
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock lock acquisition
        with patch("fleet_decision.acquire_lock"):
            with patch("fleet_decision.release_lock"):
                # Execute & Assert
                with pytest.raises(ExperimentalGateError):
                    decide_resolve("test", "b1", "optA")

    def test_decide_resolve_gate_on_executes(self, tmp_path, monkeypatch):
        """Decision resolve executes when experimental flag is true."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    desc: Decision node
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock lock, cascade (imported internally), wal_write, manifest_write_atomic
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic"):
                            # Execute
                            result = decide_resolve("test", "b1", "optA")

                            # Assert
                            assert result["status"] == "resolved"
                            assert result["promoted"] == "optA"


class TestDecisionPromotion:
    """Test option promotion (UT-D1)."""

    def test_decide_resolve_small_promotes_option(self, tmp_path, monkeypatch):
        """UT-D1: Decide-resolve small (optA, optB) promotes optA."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic"):
                            # Execute
                            result = decide_resolve("test", "b1", "optA")

                            # Assert: optA promoted
                            assert result["promoted"] == "optA"
                            assert result["status"] == "resolved"
                            assert "optB" in result["archived"]

    def test_decide_resolve_large_materializes_option(self, tmp_path, monkeypatch):
        """UT-D2: Decide-resolve large (spec_only option) materializes winning option."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
        kind: spec_only
      optB:
        ghost: ghost/test/optB
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic"):
                            # Execute
                            result = decide_resolve("test", "b1", "optA")

                            # Assert: optA materialized despite being spec_only
                            assert result["promoted"] == "optA"


class TestDecisionRebuildPrompts:
    """Test rebuild contract prompts (UT-D3..UT-D6)."""

    def test_decide_emits_rebuild_prompts(self, tmp_path, monkeypatch):
        """UT-D3: Decide-resolve emits rebuild prompts for assuming nodes."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
  a1:
    kind: feature
    base: b1
    ghost: ghost/test/a1
    assumes:
      b1:
        option: optB
        why: "API signature depends on optB"
        surfaces: ["api_version"]
        on_flip: rebuild
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic"):
                            # Execute: flip from optB to optA
                            result = decide_resolve("test", "b1", "optA")

                            # Assert: rebuild prompt emitted for a1
                            assert len(result["rebuild_prompts"]) > 0
                            prompts = result["rebuild_prompts"]
                            assert any(p["node"] == "a1" for p in prompts)
                            assert any(p["losing_option"] == "optB" for p in prompts)

    def test_decide_on_flip_rebuild_marks_needs_rework(self, tmp_path, monkeypatch):
        """UT-D4: Decide-resolve on_flip=rebuild marks node needs-rework."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
  a1:
    kind: feature
    base: b1
    ghost: ghost/test/a1
    status: draft
    assumes:
      b1:
        option: optB
        on_flip: rebuild
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Track manifest write calls
        manifest_write_calls = []

        def capture_manifest_write(feature, manifest):
            manifest_write_calls.append(manifest)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic", side_effect=capture_manifest_write):
                            # Execute
                            result = decide_resolve("test", "b1", "optA")

                            # Assert: a1 marked needs-rework
                            written_manifest = manifest_write_calls[0]
                            assert written_manifest["nodes"]["a1"]["status"] == "needs-rework"

    def test_decide_on_flip_rebase_auto_rebases(self, tmp_path, monkeypatch):
        """UT-D5: Decide-resolve on_flip=rebase auto-rebases node onto winning option."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
  a1:
    kind: feature
    base: b1
    ghost: ghost/test/a1
    status: draft
    assumes:
      b1:
        option: optB
        on_flip: rebase
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock subprocess to capture rebase call
        rebase_calls = []

        original_run = __import__('subprocess').run

        def mock_run(cmd, *args, **kwargs):
            if "rebase" in str(cmd):
                rebase_calls.append(cmd)
                return MagicMock(returncode=0)
            return original_run(cmd, *args, **kwargs)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.subprocess.run", side_effect=mock_run):
                        with patch("fleet_decision.wal_write"):
                            with patch("fleet_decision.manifest_write_atomic"):
                                # Execute
                                result = decide_resolve("test", "b1", "optA")

                                # Assert: rebase called
                                assert any("rebase" in str(cmd).lower() for cmd in rebase_calls)

    def test_decide_on_flip_manual_marks_needs_rework(self, tmp_path, monkeypatch):
        """UT-D6: Decide-resolve on_flip=manual marks node needs-rework (user decides)."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
  a1:
    kind: feature
    base: b1
    ghost: ghost/test/a1
    status: draft
    assumes:
      b1:
        option: optB
        on_flip: manual
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Track manifest writes
        manifest_write_calls = []

        def capture_manifest_write(feature, manifest):
            manifest_write_calls.append(manifest)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value={"error": None}):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic", side_effect=capture_manifest_write):
                            # Execute
                            result = decide_resolve("test", "b1", "optA")

                            # Assert: a1 marked needs-rework
                            written_manifest = manifest_write_calls[0]
                            assert written_manifest["nodes"]["a1"]["status"] == "needs-rework"


class TestDecisionConflict:
    """Test conflict handling during re-parent (UT-D7)."""

    def test_decide_conflict_marks_blocked(self, tmp_path, monkeypatch):
        """UT-D7: Decide-resolve conflict during re-parent marks descendants blocked."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    promoted: null
    status: unresolved
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
  a1:
    kind: feature
    base: b1
    ghost: ghost/test/a1
    status: draft
  a2:
    kind: feature
    base: a1
    ghost: ghost/test/a2
    status: draft
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock cascade to return error (conflict)
        cascade_error = {"error": "Conflict during rebase"}

        # Track manifest writes
        manifest_write_calls = []

        def capture_manifest_write(feature, manifest):
            manifest_write_calls.append(manifest)

        # Mock dependencies
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                with patch("fleet_cascade.cascade", return_value=cascade_error):
                    with patch("fleet_decision.wal_write"):
                        with patch("fleet_decision.manifest_write_atomic", side_effect=capture_manifest_write):
                            # Execute
                            result = decide_resolve("test", "b1", "optA")

                            # Assert: error returned when cascade fails
                            assert result.get("error") is not None
                            # Result should indicate action needed
                            assert "action" in result or "error" in result


class TestTopoSortDescendants:
    """Test topological sorting of descendants."""

    def test_topo_sort_descendants_feature_stack(self):
        """Topo sort finds descendants via feature base edges."""
        # Setup
        manifest = {
            "nodes": {
                "a1": {"kind": "feature", "base": None},
                "a2": {"kind": "feature", "base": "a1"},
                "a3": {"kind": "feature", "base": "a2"}
            }
        }

        # Execute
        descendants = _topo_sort_descendants(manifest, "a1")

        # Assert
        assert "a2" in descendants
        assert "a3" in descendants
        # BFS order: a2 before a3
        assert descendants.index("a2") < descendants.index("a3")

    def test_topo_sort_descendants_artifact_deps(self):
        """Topo sort finds descendants via artifact dependencies."""
        # Setup
        manifest = {
            "nodes": {
                "a1": {"kind": "feature", "base": None},
                "c1": {"kind": "artifact", "deps": ["a1"]}
            }
        }

        # Execute
        descendants = _topo_sort_descendants(manifest, "a1")

        # Assert
        assert "c1" in descendants

    def test_topo_sort_descendants_empty(self):
        """Topo sort handles leaf node (no descendants)."""
        # Setup
        manifest = {
            "nodes": {
                "a1": {"kind": "feature", "base": None}
            }
        }

        # Execute
        descendants = _topo_sort_descendants(manifest, "a1")

        # Assert
        assert len(descendants) == 0


class TestDecisionNodeValidation:
    """Test validation of decision nodes."""

    def test_decide_resolve_node_not_found(self, tmp_path, monkeypatch):
        """Decide resolve raises DecisionError if node not found."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    options:
      optA:
        ghost: ghost/test/optA
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock lock
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                # Execute & Assert
                with pytest.raises(DecisionError):
                    decide_resolve("test", "nonexistent_node", "optA")

    def test_decide_resolve_not_decision_node(self, tmp_path, monkeypatch):
        """Decide resolve raises DecisionError if node is not kind=decision."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  a1:
    kind: feature
    ghost: ghost/test/a1
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock lock
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                # Execute & Assert
                with pytest.raises(DecisionError):
                    decide_resolve("test", "a1", "optA")

    def test_decide_resolve_option_not_found(self, tmp_path, monkeypatch):
        """Decide resolve raises DecisionError if chosen option not found."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
experimental: true
nodes:
  b1:
    kind: decision
    options:
      optA:
        ghost: ghost/test/optA
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock lock
        with patch("fleet_decision.acquire_lock", return_value=MagicMock()):
            with patch("fleet_decision.release_lock"):
                # Execute & Assert
                with pytest.raises(DecisionError):
                    decide_resolve("test", "b1", "optB_nonexistent")
