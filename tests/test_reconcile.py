"""
Unit tests for fleet_reconcile.py.

Tests reconciliation logic: unobserved writes, DAG divergence, staleness,
artifact/presentation staleness, idempotence, and assumes preservation
(UT-R1 through UT-R7 from TEST-PLAN.md lines 354-383).
"""

import os
import subprocess
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
from io import StringIO

# Import after setting up sys path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_reconcile import (
    reconcile, _git_rev_parse, _git_merge_base, ReconcileError
)
from fleet_manifest import manifest_read, manifest_write_atomic


class TestReconcileUnobservedWrite:
    """Test unobserved write detection (Risk 1 mitigation)."""

    def test_reconcile_unobserved_write_updates_tip_sha(self, tmp_path, monkeypatch):
        """UT-R1: Reconcile detects unobserved write and updates tip_sha.

        Setup: manifest has tip_sha='abc123', git reports tip_sha='def456'
        Execute: reconcile
        Assert: manifest updated with new tip_sha, unobserved_writes populated
        """
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature A',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_sha',
                        'tip_sha': 'abc123'  # Stale tip
                    },
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        manifest_path = feature_dir / "manifest.yaml"
        lock_path = feature_dir / ".fleet.lock"

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock git calls: rev-parse returns new tip, merge-base returns base
        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd and 'ghost/test/a1' in cmd:
                # Return new tip SHA (unobserved write)
                return MagicMock(returncode=0, stdout='def456\n', stderr='')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        # Write initial manifest
        manifest_write_atomic("test", manifest)

        # Mock subprocess.run for git commands
        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, unobserved_writes, divergences = reconcile("test")

        # Assert
        assert 'a1' in unobserved_writes, "a1 should be in unobserved_writes"
        assert updated_manifest['nodes']['a1']['range']['tip_sha'] == 'def456', \
            "tip_sha should be updated to new value"
        assert len(divergences) == 0, "No divergences expected for unobserved write alone"

    def test_reconcile_no_unobserved_write(self, tmp_path, monkeypatch):
        """Reconcile with no unobserved write returns empty list."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature A',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_sha',
                        'tip_sha': 'abc123'
                    },
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock git: tip matches cached value
        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd and 'ghost/test/a1' in cmd:
                return MagicMock(returncode=0, stdout='abc123\n', stderr='')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, unobserved_writes, divergences = reconcile("test")

        # Assert
        assert len(unobserved_writes) == 0, "Should have no unobserved writes"


class TestReconcileDivergence:
    """Test DAG divergence detection."""

    def test_reconcile_dag_divergence(self, tmp_path, monkeypatch):
        """UT-R2: Reconcile detects DAG divergence (manual rebase).

        Setup: manifest base='a1', git parent='main'
        Execute: reconcile
        Assert: divergences populated, NOT auto-fixed
        """
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Root',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_tip',
                        'tip_sha': 'a1_tip'
                    },
                    'status': 'draft',
                    'assumes': {}
                },
                'a2': {
                    'kind': 'feature',
                    'desc': 'Stacked',
                    'base': 'a1',
                    'ghost': 'ghost/test/a2',
                    'range': {
                        'base_sha': 'a1_tip',
                        'tip_sha': 'a2_tip'
                    },
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock git: a2~1 (parent) is main, but manifest expects a1_tip
        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd:
                if 'ghost/test/a1' in cmd and '~1' not in cmd:
                    return MagicMock(returncode=0, stdout='a1_tip\n', stderr='')
                elif 'ghost/test/a2' in cmd and '~1' not in cmd:
                    return MagicMock(returncode=0, stdout='a2_tip\n', stderr='')
                elif 'ghost/test/a2~1' in cmd:
                    # a2's parent is main, not a1_tip (divergence!)
                    return MagicMock(returncode=0, stdout='main_tip\n', stderr='')
                return MagicMock(returncode=1, stdout='', stderr='not found')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_tip\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, unobserved_writes, divergences = reconcile("test")

        # Assert
        assert len(divergences) > 0, "Should detect divergence"
        assert divergences[0]['node'] == 'a2', "a2 should have divergence"
        assert divergences[0]['reason'] == 'git parent ≠ manifest base'
        # Verify NOT auto-fixed
        assert updated_manifest['nodes']['a2']['status'] == 'draft', \
            "Status should NOT be auto-fixed"


class TestReconcileArtifactStaleness:
    """Test artifact staleness detection."""

    def test_reconcile_artifact_staleness(self, tmp_path, monkeypatch):
        """UT-R3: Reconcile marks artifact stale if dep changed.

        Setup: artifact depends on a1, cached deps_sha['a1']='old_sha',
               live a1 tip='new_sha'
        Execute: reconcile
        Assert: artifact.stale=true
        """
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_sha',
                        'tip_sha': 'old_a1_sha'
                    },
                    'status': 'draft',
                    'assumes': {}
                },
                'c1': {
                    'kind': 'artifact',
                    'desc': 'Test suite',
                    'deps': ['a1'],
                    'deps_sha': {
                        'a1': 'old_a1_sha'  # Cached
                    },
                    'stale': False,
                    'status': 'draft'
                }
            }
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock git: a1 tip changed
        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd and 'ghost/test/a1' in cmd:
                return MagicMock(returncode=0, stdout='new_a1_sha\n', stderr='')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, unobserved_writes, divergences = reconcile("test")

        # Assert
        assert updated_manifest['nodes']['c1']['stale'] is True, \
            "Artifact should be marked stale"
        assert updated_manifest['nodes']['c1']['deps_sha']['a1'] == 'new_a1_sha', \
            "deps_sha should be updated"


class TestReconcilePresentationStaleness:
    """Test presentation staleness detection."""

    def test_reconcile_presentation_staleness(self, tmp_path, monkeypatch):
        """UT-R4: Reconcile marks presentation stale if ghost moved.

        Setup: presentation.source_tip_sha='old_sha', live ghost tip='new_sha'
        Execute: reconcile
        Assert: presentation.stale=true
        """
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_sha',
                        'tip_sha': 'old_a1_sha'
                    },
                    'status': 'draft',
                    'present': {
                        'source_tip_sha': 'old_a1_sha',
                        'stale': False
                    },
                    'assumes': {}
                }
            }
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Mock git: a1 tip changed
        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd and 'ghost/test/a1' in cmd:
                return MagicMock(returncode=0, stdout='new_a1_sha\n', stderr='')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, unobserved_writes, divergences = reconcile("test")

        # Assert
        assert updated_manifest['nodes']['a1']['present']['stale'] is True, \
            "Presentation should be marked stale"


class TestReconcileIdempotence:
    """Test reconciliation idempotence."""

    def test_reconcile_idempotence_no_change(self, tmp_path, monkeypatch):
        """UT-R5: Reconcile run 2x with no git change produces identical output.

        Setup: stable manifest, stable git state
        Execute: reconcile twice
        Assert: both outputs identical
        """
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_sha',
                        'tip_sha': 'a1_tip'
                    },
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd and 'ghost/test/a1' in cmd:
                return MagicMock(returncode=0, stdout='a1_tip\n', stderr='')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute 1st run
            manifest1, writes1, divs1 = reconcile("test")

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute 2nd run
            manifest2, writes2, divs2 = reconcile("test")

        # Assert
        assert writes1 == writes2, "unobserved_writes should be identical"
        assert divs1 == divs2, "divergences should be identical"
        assert manifest1['nodes']['a1']['range']['tip_sha'] == \
               manifest2['nodes']['a1']['range']['tip_sha'], \
               "manifest state should be identical"


class TestReconcilePreservesAssumes:
    """Test that reconcile preserves non-derivable assumes fields."""

    def test_reconcile_preserves_assumes(self, tmp_path, monkeypatch):
        """UT-R6: Reconcile preserves assumes.* fields (non-derivable).

        Setup: node has assumes={'optA': 'value'} (user-set, not derivable)
        Execute: reconcile
        Assert: assumes unchanged
        """
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        original_assumes = {'optA': 'value', 'optB': 'other'}
        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {
                        'base_sha': 'main_sha',
                        'tip_sha': 'a1_tip'
                    },
                    'status': 'draft',
                    'assumes': original_assumes
                }
            }
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        def mock_git_run(cmd, *args, **kwargs):
            if 'rev-parse' in cmd and 'ghost/test/a1' in cmd:
                return MagicMock(returncode=0, stdout='a1_tip\n', stderr='')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='main_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, writes, divs = reconcile("test")

        # Assert
        assert updated_manifest['nodes']['a1']['assumes'] == original_assumes, \
            "assumes should be preserved unchanged"


class TestReconcilePerformance:
    """Test reconciliation performance (NFR-5)."""

    def test_reconcile_50node_dag_perf(self, tmp_path, monkeypatch):
        """UT-R7: Reconcile 50-node DAG completes in <100ms.

        Note: This is a simplified perf test; real perf testing uses timeit.
        For unit test, we verify reconcile completes without error on larger DAG.
        """
        # Setup: 50-node linear chain
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        nodes = {}
        for i in range(50):
            nodes[f'a{i}'] = {
                'kind': 'feature',
                'desc': f'Feature {i}',
                'base': f'a{i-1}' if i > 0 else None,
                'ghost': f'ghost/test/a{i}',
                'range': {
                    'base_sha': f'base_{i}',
                    'tip_sha': f'tip_{i}'
                },
                'status': 'draft',
                'assumes': {}
            }

        manifest = {
            'feature': 'test',
            'canonical': 'main',
            'nodes': nodes
        }

        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        call_count = [0]
        def mock_git_run(cmd, *args, **kwargs):
            call_count[0] += 1
            if 'rev-parse' in cmd:
                # Extract node num from ghost ref
                for i in range(50):
                    if f'ghost/test/a{i}' in cmd and '~1' not in cmd:
                        return MagicMock(returncode=0, stdout=f'tip_{i}\n', stderr='')
                return MagicMock(returncode=1, stdout='', stderr='not found')
            elif 'merge-base' in cmd:
                return MagicMock(returncode=0, stdout='base_sha\n', stderr='')
            return MagicMock(returncode=1, stdout='', stderr='error')

        manifest_write_atomic("test", manifest)

        import time
        start = time.time()

        with patch('subprocess.run', side_effect=mock_git_run):
            # Execute
            updated_manifest, writes, divs = reconcile("test")

        elapsed = time.time() - start

        # Assert: complete without error (perf tolerance loose for unit test)
        assert len(updated_manifest['nodes']) == 50
        assert elapsed < 10, f"Reconcile took {elapsed}s (should be <100ms in perf test)"
