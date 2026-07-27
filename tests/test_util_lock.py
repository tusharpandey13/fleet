"""
Unit tests for fleet_lock.py.

Tests lock acquire/release, stale-lock detection, and topological DAG utilities
(git_depth_in_dag, find_dag_root, get_max_descendant) - UT-U1 through UT-U7 and
lock management cases.
"""

import os
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from subprocess import CompletedProcess

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_lock import (
    acquire_lock, release_lock, is_stale_lock,
    git_depth_in_dag, find_dag_root, get_max_descendant,
    LockError, LockTimeoutError
)


class TestLockAcquireRelease:
    """Test lock acquire and release operations."""

    def test_lock_acquire_no_contention(self, tmp_path):
        """UT-U5: Lock acquire — no contention instant acquire."""
        # Setup
        lock_path = str(tmp_path / "test.lock")

        # Execute
        start = time.time()
        lock_obj = acquire_lock(lock_path, timeout=30)
        elapsed = time.time() - start

        # Assert: instant acquire (< 100ms)
        assert elapsed < 0.1
        assert lock_obj is not None
        assert lock_obj.mode == 'a+'

        # Cleanup
        release_lock(lock_obj)

    def test_lock_acquire_writes_pid_timestamp(self, tmp_path):
        """Lock acquire writes PID and timestamp to lock file."""
        # Setup
        lock_path = str(tmp_path / "test.lock")

        # Execute
        lock_obj = acquire_lock(lock_path, timeout=30)
        current_pid = os.getpid()

        # Assert: lock file contains PID:timestamp
        with open(lock_path, 'r') as f:
            content = f.read().strip()

        assert str(current_pid) in content
        assert ':' in content
        parts = content.split(':')
        assert len(parts) == 2
        assert int(parts[0]) == current_pid

        # Cleanup
        release_lock(lock_obj)

    def test_lock_release_closes_file(self, tmp_path):
        """Lock release closes file object."""
        # Setup
        lock_path = str(tmp_path / "test.lock")
        lock_obj = acquire_lock(lock_path, timeout=30)

        # Execute
        release_lock(lock_obj)

        # Assert: file is closed
        assert lock_obj.closed

    def test_lock_creates_directory(self, tmp_path):
        """Lock acquire creates directory if not exist."""
        # Setup
        nested_lock_path = str(tmp_path / "a" / "b" / "c" / "test.lock")

        # Execute
        lock_obj = acquire_lock(nested_lock_path, timeout=30)

        # Assert: directory created
        assert os.path.exists(os.path.dirname(nested_lock_path))

        # Cleanup
        release_lock(lock_obj)


class TestLockStalenessDetection:
    """Test stale-lock detection and reclamation."""

    def test_lock_stale_check_dead_pid(self, tmp_path):
        """UT-U6: Lock stale-check — PID dead lock reclaimed."""
        # Setup
        lock_path = str(tmp_path / "test.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)

        # Write lock file with dead PID
        dead_pid = 99999  # Unlikely to exist
        with open(lock_path, 'w') as f:
            f.write(f"{dead_pid}:{int(time.time())}\n")

        # Execute
        is_stale = is_stale_lock(lock_path, timeout=1800)

        # Assert: detected as stale
        assert is_stale is True

    def test_lock_stale_check_old_timestamp(self, tmp_path):
        """UT-U7: Lock stale-check — old timestamp lock reclaimed."""
        # Setup
        lock_path = str(tmp_path / "test.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)

        # Write lock file with timestamp beyond timeout
        current_pid = os.getpid()
        old_time = int(time.time() - 2000)  # 2000 seconds ago
        with open(lock_path, 'w') as f:
            f.write(f"{current_pid}:{old_time}\n")

        # Execute
        is_stale = is_stale_lock(lock_path, timeout=1800)

        # Assert: detected as stale (old age)
        assert is_stale is True

    def test_lock_stale_check_alive_recent(self, tmp_path):
        """Lock stale-check (alive process, recent) NOT stale."""
        # Setup
        lock_path = str(tmp_path / "test.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)

        # Write lock file with current PID and recent timestamp
        current_pid = os.getpid()
        recent_time = int(time.time() - 10)  # 10 seconds ago
        with open(lock_path, 'w') as f:
            f.write(f"{current_pid}:{recent_time}\n")

        # Execute
        is_stale = is_stale_lock(lock_path, timeout=1800)

        # Assert: NOT stale
        assert is_stale is False

    def test_lock_stale_check_nonexistent(self, tmp_path):
        """Lock stale-check (nonexistent file) returns True."""
        # Setup
        lock_path = str(tmp_path / "nonexistent.lock")

        # Execute
        is_stale = is_stale_lock(lock_path, timeout=1800)

        # Assert
        assert is_stale is True

    def test_lock_stale_check_empty_file(self, tmp_path):
        """Lock stale-check (empty file) returns True."""
        # Setup
        lock_path = str(tmp_path / "empty.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            f.write("")

        # Execute
        is_stale = is_stale_lock(lock_path, timeout=1800)

        # Assert
        assert is_stale is True

    def test_lock_stale_check_malformed(self, tmp_path):
        """Lock stale-check (malformed) returns True."""
        # Setup
        lock_path = str(tmp_path / "malformed.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            f.write("invalid_format\n")

        # Execute
        is_stale = is_stale_lock(lock_path, timeout=1800)

        # Assert
        assert is_stale is True


class TestGitDepthInDAG:
    """Test topological depth computation in DAG."""

    def test_git_depth_in_dag_single_chain(self):
        """UT-U1: git_depth_in_dag single-chain depth = chain length."""
        # Setup: a1 -> a2 -> a3 (single chain)
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'desc': 'Root'},
                'a2': {'kind': 'feature', 'base': 'a1', 'desc': 'A2'},
                'a3': {'kind': 'feature', 'base': 'a2', 'desc': 'A3'}
            }
        }

        # Execute
        depth_a1 = git_depth_in_dag('a1', manifest)
        depth_a2 = git_depth_in_dag('a2', manifest)
        depth_a3 = git_depth_in_dag('a3', manifest)

        # Assert
        assert depth_a1 == 0  # Root
        assert depth_a2 == 1  # One edge from a1
        assert depth_a3 == 2  # Two edges from a1

    def test_git_depth_in_dag_multi_level(self):
        """UT-U2: git_depth_in_dag multi-level depth = longest path."""
        # Setup: a1 root; a2a and a2b parallel; a3 on a2a
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'desc': 'Root'},
                'a2a': {'kind': 'feature', 'base': 'a1', 'desc': 'A2a'},
                'a2b': {'kind': 'feature', 'base': 'a1', 'desc': 'A2b'},
                'a3': {'kind': 'feature', 'base': 'a2a', 'desc': 'A3'}
            }
        }

        # Execute
        depth_a1 = git_depth_in_dag('a1', manifest)
        depth_a2a = git_depth_in_dag('a2a', manifest)
        depth_a2b = git_depth_in_dag('a2b', manifest)
        depth_a3 = git_depth_in_dag('a3', manifest)

        # Assert
        assert depth_a1 == 0
        assert depth_a2a == 1
        assert depth_a2b == 1  # Same depth as a2a (parallel)
        assert depth_a3 == 2   # Deepest: a1 -> a2a -> a3

    def test_git_depth_in_dag_parallel_branches_same_depth(self):
        """UT-U3: git_depth_in_dag parallel branches at same depth."""
        # Setup: a1 root; a2a, a2b parallel at depth 1
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'desc': 'Root'},
                'a2a': {'kind': 'feature', 'base': 'a1', 'desc': 'A2a'},
                'a2b': {'kind': 'feature', 'base': 'a1', 'desc': 'A2b'}
            }
        }

        # Execute
        depth_a2a = git_depth_in_dag('a2a', manifest)
        depth_a2b = git_depth_in_dag('a2b', manifest)

        # Assert: both at same depth
        assert depth_a2a == depth_a2b == 1

    def test_git_depth_in_dag_unreachable_node(self):
        """UT-U4: git_depth_in_dag unreachable from main DAG has depth from its own root."""
        # Setup: orphan node based on nonexistent node (not in main DAG with a1)
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'desc': 'Root'},
                'orphan': {'kind': 'feature', 'base': 'nonexistent', 'desc': 'Orphan'}
            }
        }

        # Execute
        depth = git_depth_in_dag('orphan', manifest)

        # Assert: orphan has depth 1 relative to its root 'nonexistent'
        # find_dag_root('orphan') returns 'nonexistent'
        # DFS from 'nonexistent' at depth 0 finds orphan as descendant (base=='nonexistent')
        # so orphan gets depth 1
        assert depth == 1

    def test_git_depth_in_dag_with_artifact_deps(self):
        """Depth computation for artifact nodes uses artifact as root."""
        # Setup: a1 -> a2 (feature), c1 artifact depending on a2
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'desc': 'Root'},
                'a2': {'kind': 'feature', 'base': 'a1', 'desc': 'A2'},
                'c1': {
                    'kind': 'artifact',
                    'deps': ['a2'],
                    'desc': 'Artifact'
                }
            }
        }

        # Execute
        depth_a1 = git_depth_in_dag('a1', manifest)
        depth_a2 = git_depth_in_dag('a2', manifest)
        depth_c1 = git_depth_in_dag('c1', manifest)

        # Assert
        assert depth_a1 == 0  # a1 is root
        assert depth_a2 == 1  # a2 based on a1
        # c1 is an artifact with no base; find_dag_root('c1') returns 'c1'
        # so c1 is its own root with depth 0
        assert depth_c1 == 0


class TestFindDagRoot:
    """Test topological root finding."""

    def test_find_dag_root_single_chain(self):
        """Find DAG root in single chain returns root."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None},
                'a2': {'kind': 'feature', 'base': 'a1'},
                'a3': {'kind': 'feature', 'base': 'a2'}
            }
        }

        # Execute
        root_from_a1 = find_dag_root('a1', manifest)
        root_from_a2 = find_dag_root('a2', manifest)
        root_from_a3 = find_dag_root('a3', manifest)

        # Assert: all find a1 as root
        assert root_from_a1 == 'a1'
        assert root_from_a2 == 'a1'
        assert root_from_a3 == 'a1'

    def test_find_dag_root_parallel_branches(self):
        """Find DAG root with parallel branches."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None},
                'a2a': {'kind': 'feature', 'base': 'a1'},
                'a2b': {'kind': 'feature', 'base': 'a1'}
            }
        }

        # Execute
        root_a2a = find_dag_root('a2a', manifest)
        root_a2b = find_dag_root('a2b', manifest)

        # Assert
        assert root_a2a == 'a1'
        assert root_a2b == 'a1'

    def test_find_dag_root_already_root(self):
        """Find DAG root when node is already root."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None}
            }
        }

        # Execute
        root = find_dag_root('a1', manifest)

        # Assert
        assert root == 'a1'

    def test_find_dag_root_nonexistent_node(self):
        """Find DAG root for nonexistent node returns node itself."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None}
            }
        }

        # Execute
        root = find_dag_root('nonexistent', manifest)

        # Assert
        assert root == 'nonexistent'


class TestGetMaxDescendant:
    """Test max descendant selection with tie-breaking."""

    @patch('subprocess.check_output')
    def test_get_max_descendant_single_deepest(self, mock_check_output):
        """Get max descendant selects deepest node."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'ghost': 'ghost/a1'},
                'a2': {'kind': 'feature', 'base': 'a1', 'ghost': 'ghost/a2'},
                'a3': {'kind': 'feature', 'base': 'a2', 'ghost': 'ghost/a3'}
            }
        }

        def mock_git_rev_parse(cmd, *args, **kwargs):
            if 'a2' in str(cmd):
                return b'sha_a2\n'
            elif 'a3' in str(cmd):
                return b'sha_a3\n'
            return b'sha_default\n'

        mock_check_output.side_effect = mock_git_rev_parse

        # Execute
        max_desc = get_max_descendant(['a2', 'a3'], manifest, 'a1')

        # Assert: a3 is deepest
        assert max_desc == 'a3'

    @patch('subprocess.check_output')
    def test_get_max_descendant_parallel_tiebreak_sha(self, mock_check_output):
        """Get max descendant parallel branches tie-break by SHA."""
        # Setup: a1 root; a2a, a2b parallel at same depth
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'ghost': 'ghost/a1'},
                'a2a': {'kind': 'feature', 'base': 'a1', 'ghost': 'ghost/a2a'},
                'a2b': {'kind': 'feature', 'base': 'a1', 'ghost': 'ghost/a2b'}
            }
        }

        def mock_git_rev_parse(cmd, *args, **kwargs):
            if 'a2a' in str(cmd):
                return b'aaaa\n'  # Smaller SHA
            elif 'a2b' in str(cmd):
                return b'bbbb\n'  # Larger SHA
            return b'0000\n'

        mock_check_output.side_effect = mock_git_rev_parse

        # Execute
        max_desc = get_max_descendant(['a2a', 'a2b'], manifest, 'a1')

        # Assert: ties on depth; lexicographic SHA selects a2b (bbbb > aaaa)
        assert max_desc == 'a2b'

    @patch('subprocess.check_output')
    def test_get_max_descendant_empty_list(self, mock_check_output):
        """Get max descendant empty list returns root."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'ghost': 'ghost/a1'}
            }
        }

        # Execute
        max_desc = get_max_descendant([], manifest, 'a1')

        # Assert
        assert max_desc == 'a1'

    @patch('subprocess.check_output')
    def test_get_max_descendant_git_error_fallback(self, mock_check_output):
        """Get max descendant handles git error gracefully."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None, 'ghost': 'ghost/a1'},
                'a2': {'kind': 'feature', 'base': 'a1', 'ghost': 'ghost/a2'},
                'a3': {'kind': 'feature', 'base': 'a2', 'ghost': 'ghost/a3'}
            }
        }

        # Raise exception on git call
        mock_check_output.side_effect = Exception("git error")

        # Execute
        max_desc = get_max_descendant(['a2', 'a3'], manifest, 'a1')

        # Assert: returns one of the options (depth-based fallback)
        assert max_desc in ['a2', 'a3']


class TestLockEdgeCases:
    """Test edge cases for lock operations."""

    def test_lock_acquire_malformed_existing_lock(self, tmp_path):
        """Lock acquire with malformed existing lock reclaims."""
        # Setup
        lock_path = str(tmp_path / "malformed.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)

        # Write malformed lock file
        with open(lock_path, 'w') as f:
            f.write("not:a:valid:format\n")

        # Execute: should reclaim
        lock_obj = acquire_lock(lock_path, timeout=30)

        # Assert: successfully acquired
        assert lock_obj is not None
        assert lock_obj.mode == 'a+'

        # Cleanup
        release_lock(lock_obj)

    def test_lock_release_handles_errors_gracefully(self, tmp_path):
        """Lock release handles errors gracefully."""
        # Setup
        lock_path = str(tmp_path / "test.lock")
        lock_obj = acquire_lock(lock_path, timeout=30)

        # Close the lock object early to simulate error
        lock_obj.close()

        # Execute: should not raise
        release_lock(lock_obj)

        # Assert: no exception


class TestDAGComplexTopologies:
    """Test DAG utilities on complex topologies."""

    def test_depth_complex_dag_diamond(self):
        """Depth computation on diamond topology."""
        # Setup: a1 -> a2a, a2b -> a3 (diamond)
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None},
                'a2a': {'kind': 'feature', 'base': 'a1'},
                'a2b': {'kind': 'feature', 'base': 'a1'},
                'a3': {'kind': 'feature', 'base': 'a2a'}  # Only a3 depends on a2a
            }
        }

        # Execute
        depth_a1 = git_depth_in_dag('a1', manifest)
        depth_a2a = git_depth_in_dag('a2a', manifest)
        depth_a2b = git_depth_in_dag('a2b', manifest)
        depth_a3 = git_depth_in_dag('a3', manifest)

        # Assert
        assert depth_a1 == 0
        assert depth_a2a == 1
        assert depth_a2b == 1
        assert depth_a3 == 2  # Deepest

    def test_root_in_diamond_from_any_node(self):
        """Find root from any node in diamond DAG."""
        # Setup
        manifest = {
            'nodes': {
                'a1': {'kind': 'feature', 'base': None},
                'a2a': {'kind': 'feature', 'base': 'a1'},
                'a2b': {'kind': 'feature', 'base': 'a1'},
                'a3': {'kind': 'feature', 'base': 'a2a'}
            }
        }

        # Execute
        for node in ['a1', 'a2a', 'a2b', 'a3']:
            root = find_dag_root(node, manifest)
            assert root == 'a1'
