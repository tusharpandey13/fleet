"""
Pytest fixtures for fleet testing.

Provides git setup, temp directories, manifest factories, and WAL utilities.
"""

import os
import json
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple

import pytest


@pytest.fixture
def tmp_fleet_dir(tmp_path):
    """
    Create a temporary fleet home directory (~/.fleet mock).

    Yields:
        Path to temp fleet directory
    """
    fleet_dir = tmp_path / ".fleet"
    fleet_dir.mkdir()
    yield fleet_dir


@pytest.fixture
def tmp_repo(tmp_path):
    """
    Create a temporary git repository.

    Yields:
        Tuple of (repo_path, original_cwd)
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    original_cwd = os.getcwd()
    os.chdir(str(repo_path))

    # Initialize git repo
    subprocess.run(['git', 'init'], check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test User'], check=True, capture_output=True)

    # Create initial commit on main
    (repo_path / 'README.md').write_text('# Test Repo\n')
    subprocess.run(['git', 'add', 'README.md'], check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'Initial commit'], check=True, capture_output=True)

    yield repo_path, original_cwd

    # Cleanup: restore original cwd
    os.chdir(original_cwd)


@pytest.fixture
def feature_name():
    """Return a test feature name."""
    return "payments"


@pytest.fixture
def minimal_manifest() -> Dict[str, Any]:
    """
    Create a minimal valid manifest dict.

    Returns:
        Manifest dict with required fields
    """
    return {
        'feature': 'payments',
        'canonical': 'main',
        'merge_mode': 'individual',
        'max_worktrees': 6,
        'nodes': {
            'a1': {
                'kind': 'feature',
                'desc': 'Root feature',
                'base': None,
                'ghost': 'ghost/pay/a1',
                'range': {
                    'base_sha': 'abc123',
                    'tip_sha': 'def456'
                },
                'status': 'draft',
                'assume': {}
            }
        }
    }


@pytest.fixture
def manifest_with_stack() -> Dict[str, Any]:
    """
    Create a manifest with stacked features.

    Returns:
        Manifest dict with a1 -> a2 stack
    """
    return {
        'feature': 'payments',
        'canonical': 'main',
        'nodes': {
            'a1': {
                'kind': 'feature',
                'desc': 'Root feature',
                'base': None,
                'ghost': 'ghost/pay/a1',
                'range': {
                    'base_sha': 'main_tip',
                    'tip_sha': 'a1_tip'
                },
                'status': 'draft',
                'assumes': {}
            },
            'a2': {
                'kind': 'feature',
                'desc': 'Stacked on a1',
                'base': 'a1',
                'ghost': 'ghost/pay/a2',
                'range': {
                    'base_sha': 'a1_tip',
                    'tip_sha': 'a2_tip'
                },
                'status': 'draft',
                'assumes': {}
            }
        }
    }


@pytest.fixture
def manifest_with_artifact() -> Dict[str, Any]:
    """
    Create a manifest with artifact dependency.

    Returns:
        Manifest dict with feature and artifact
    """
    return {
        'feature': 'payments',
        'canonical': 'main',
        'nodes': {
            'a1': {
                'kind': 'feature',
                'desc': 'Root feature',
                'base': None,
                'ghost': 'ghost/pay/a1',
                'range': {
                    'base_sha': 'main_tip',
                    'tip_sha': 'a1_tip'
                },
                'status': 'draft',
                'assumes': {}
            },
            'c1': {
                'kind': 'artifact',
                'desc': 'Test suite',
                'deps': ['a1'],
                'deps_sha': {
                    'a1': 'a1_tip'
                },
                'stale': False,
                'status': 'draft'
            }
        }
    }


@pytest.fixture
def mock_fleet_home(tmp_path, monkeypatch):
    """
    Mock ~/.fleet to use temp directory.

    Yields:
        Mocked fleet home path
    """
    fleet_dir = tmp_path / ".fleet"
    fleet_dir.mkdir()

    # Monkeypatch os.path.expanduser to return our temp path
    original_expanduser = os.path.expanduser

    def mock_expanduser(path: str) -> str:
        if path.startswith("~/.fleet"):
            rel_path = path[len("~/"):]
            return str(fleet_dir / rel_path)
        return original_expanduser(path)

    monkeypatch.setattr("os.path.expanduser", mock_expanduser)

    yield fleet_dir


class WALHelper:
    """Helper for WAL file operations in tests."""

    def __init__(self, wal_path: Path):
        self.wal_path = wal_path
        self.wal_path.parent.mkdir(parents=True, exist_ok=True)

    def write_record(self, op_type: str, status: str, payload: Dict[str, Any]) -> None:
        """Write a WAL record."""
        record = {
            'timestamp': '2026-07-27T14:32:15Z',
            'op_type': op_type,
            'status': status,
            'payload': payload
        }
        with open(self.wal_path, 'a') as f:
            f.write(json.dumps(record) + '\n')

    def write_intent(self, op_type: str, payload: Dict[str, Any]) -> None:
        """Write an intent record."""
        self.write_record(op_type, 'intent', payload)

    def write_commit(self, op_type: str, payload: Dict[str, Any] = None) -> None:
        """Write a commit record."""
        if payload is None:
            payload = {'status': 'success'}
        self.write_record(op_type, 'commit', payload)

    def read_records(self) -> list:
        """Read all records from WAL."""
        if not self.wal_path.exists():
            return []

        records = []
        with open(self.wal_path, 'r') as f:
            for line in f:
                try:
                    records.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    pass

        return records


@pytest.fixture
def wal_helper(tmp_path):
    """
    Provide a WAL helper for tests.

    Yields:
        WALHelper instance
    """
    wal_path = tmp_path / "test.wal.jsonl"
    yield WALHelper(wal_path)
