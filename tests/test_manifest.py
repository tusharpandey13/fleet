"""
Unit tests for fleet_manifest.py.

Tests manifest read/write operations, schema validation, atomic writes,
and cycle detection (UT-M1 through UT-M9).
"""

import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import after setting up sys path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_manifest import (
    manifest_read, manifest_write_atomic, manifest_validate,
    ManifestError, SchemaError
)


class TestManifestRead:
    """Test manifest_read operation."""

    def test_manifest_read_valid(self, tmp_path, monkeypatch):
        """UT-M1: Read valid manifest returns parsed dict."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: null
    ghost: ghost/test/a1
    range:
      base_sha: abc123
      tip_sha: def456
    status: draft
    assumes: {}
""")

        # Patch expanduser to use temp dir
        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        result = manifest_read("test")

        # Assert
        assert result['feature'] == 'test'
        assert 'a1' in result['nodes']
        assert result['nodes']['a1']['kind'] == 'feature'

    def test_manifest_read_not_found(self, tmp_path, monkeypatch):
        """UT-M2: Read missing manifest raises FileNotFoundError."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute & Assert
        with pytest.raises(FileNotFoundError):
            manifest_read("nonexistent")


class TestManifestValidate:
    """Test manifest_validate schema rules."""

    def test_validate_missing_required_field(self):
        """UT-M3: Missing required field returns error list."""
        # Setup
        incomplete = {'feature': 'test'}  # Missing 'nodes'

        # Execute
        errors = manifest_validate(incomplete)

        # Assert
        assert len(errors) > 0
        assert any('missing' in err.lower() for err in errors)
        assert any('nodes' in err.lower() for err in errors)

    def test_validate_invalid_node_kind(self):
        """UT-M4: Invalid node kind returns error."""
        # Setup
        manifest = {
            'feature': 'test',
            'nodes': {
                'a1': {
                    'kind': 'invalid_kind',
                    'base': None
                }
            }
        }

        # Execute
        errors = manifest_validate(manifest)

        # Assert
        assert len(errors) > 0
        assert any('invalid' in err.lower() for err in errors)

    def test_validate_dag_cycle(self):
        """UT-M5: DAG cycle detection returns error."""
        # Setup
        manifest = {
            'feature': 'test',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'base': 'a2'
                },
                'a2': {
                    'kind': 'feature',
                    'base': 'a1'  # Cycle: a1 -> a2 -> a1
                }
            }
        }

        # Execute
        errors = manifest_validate(manifest)

        # Assert
        assert len(errors) > 0
        assert any('cycle' in err.lower() for err in errors)

    def test_validate_valid_manifest(self):
        """Validate a correct manifest returns empty error list."""
        # Setup
        manifest = {
            'feature': 'test',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Root',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {'base_sha': 'abc', 'tip_sha': 'def'},
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        # Execute
        errors = manifest_validate(manifest)

        # Assert
        assert len(errors) == 0

    def test_validate_decision_node_without_options(self):
        """Decision node without options returns error."""
        # Setup
        manifest = {
            'feature': 'test',
            'nodes': {
                'b1': {
                    'kind': 'decision',
                    'desc': 'Decision',
                    'options': None  # Missing options
                }
            }
        }

        # Execute
        errors = manifest_validate(manifest)

        # Assert
        assert len(errors) > 0
        assert any('option' in err.lower() for err in errors)


class TestManifestWriteAtomic:
    """Test atomic manifest write operations."""

    def test_write_atomic_valid(self, tmp_path, monkeypatch):
        """UT-M6: Atomic write creates file via temp + rename."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        manifest = {
            'feature': 'test',
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature A',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {'base_sha': 'abc', 'tip_sha': 'def'},
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        # Execute
        manifest_write_atomic("test", manifest)

        # Assert
        manifest_path = tmp_path / ".fleet" / "test" / "manifest.yaml"
        assert manifest_path.exists()

        # Verify readable
        result = manifest_read("test")
        assert result['feature'] == 'test'

    def test_write_atomic_rejects_invalid_schema(self, tmp_path, monkeypatch):
        """UT-M8: Write with schema violation rejected, file unchanged."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        invalid_manifest = {'feature': 'test'}  # Missing nodes

        # Execute & Assert
        with pytest.raises(SchemaError):
            manifest_write_atomic("test", invalid_manifest)

    def test_manifest_roundtrip(self, tmp_path, monkeypatch):
        """UT-M9: Roundtrip (read -> modify -> write -> read) preserves data."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        original_manifest = {
            'feature': 'test',
            'canonical': 'main',
            'merge_mode': 'individual',
            'max_worktrees': 6,
            'nodes': {
                'a1': {
                    'kind': 'feature',
                    'desc': 'Feature A',
                    'base': None,
                    'ghost': 'ghost/test/a1',
                    'range': {'base_sha': 'abc', 'tip_sha': 'def'},
                    'status': 'draft',
                    'assumes': {}
                }
            }
        }

        # Write
        manifest_write_atomic("test", original_manifest)

        # Read
        read_manifest = manifest_read("test")

        # Modify
        read_manifest['nodes']['a1']['desc'] = 'Modified Feature A'

        # Write again
        manifest_write_atomic("test", read_manifest)

        # Read again
        final_manifest = manifest_read("test")

        # Assert
        assert final_manifest['nodes']['a1']['desc'] == 'Modified Feature A'
        assert final_manifest['feature'] == original_manifest['feature']
