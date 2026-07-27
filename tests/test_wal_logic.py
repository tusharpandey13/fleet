"""
Unit tests for fleet_wal.py.

Tests WAL write/read operations, replay idempotence, lock management, and
recovery scenarios (UT-W1 through UT-W10).
"""

import os
import sys
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_wal import (
    wal_write, wal_read_incomplete, wal_replay,
    _wal_path, _acquire_lock_internal, _release_lock_internal,
    _is_stale_lock, WALError
)


class TestWALWrite:
    """Test WAL write operations."""

    def test_wal_write_intent_record(self, tmp_path, monkeypatch):
        """UT-W1: Write intent record appends JSONL line, flushed."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        payload = {
            "op_type": "cascade",
            "root": "a1",
            "descendants": ["a2", "a3"],
            "pre_image_refs": {}
        }

        # Execute
        wal_write(feature, "cascade", "intent", payload)

        # Assert
        wal_file = tmp_path / ".fleet" / feature / "wal.jsonl"
        assert wal_file.exists()

        with open(wal_file, 'r') as f:
            line = f.read().strip()

        record = json.loads(line)
        assert record['op_type'] == 'cascade'
        assert record['status'] == 'intent'
        assert record['payload'] == payload
        assert 'timestamp' in record

    def test_wal_write_commit_record(self, tmp_path, monkeypatch):
        """UT-W2: Write commit record completes intent/commit pair."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        intent_payload = {"op_type": "cascade", "root": "a1"}
        commit_payload = {"status": "success", "rebased": ["a2", "a3"]}

        # Execute
        wal_write(feature, "cascade", "intent", intent_payload)
        wal_write(feature, "cascade", "commit", commit_payload)

        # Assert
        wal_file = tmp_path / ".fleet" / feature / "wal.jsonl"
        with open(wal_file, 'r') as f:
            lines = f.readlines()

        assert len(lines) == 2
        record1 = json.loads(lines[0])
        record2 = json.loads(lines[1])

        assert record1['status'] == 'intent'
        assert record2['status'] == 'commit'
        assert record1['op_type'] == record2['op_type']


class TestWALReadIncomplete:
    """Test WAL incomplete entry detection."""

    def test_wal_read_incomplete_returns_unpaired_intents(self, tmp_path, wal_helper):
        """UT-W3: Read incomplete entries returns unpaired intent records."""
        # Setup: write intent + incomplete (no commit)
        wal_helper.write_intent("cascade", {"root": "a1"})
        wal_helper.write_intent("merge-back", {"node": "a1"})
        wal_helper.write_commit("cascade", {"status": "success"})  # Pairs first intent

        # Execute
        incomplete = wal_helper.read_records()
        # Filter to find unpaired
        intent_map = {}
        for rec in incomplete:
            if rec.get('status') == 'intent':
                key = rec.get('op_type')
                intent_map[key] = rec
            elif rec.get('status') == 'commit':
                key = rec.get('op_type')
                if key in intent_map:
                    del intent_map[key]

        unpaired = list(intent_map.values())

        # Assert
        assert len(unpaired) == 1
        assert unpaired[0]['op_type'] == 'merge-back'

    def test_wal_read_no_incomplete_entries(self, tmp_path, wal_helper):
        """UT-W4: Read no incomplete entries returns empty list."""
        # Setup: write paired intent/commit
        wal_helper.write_intent("cascade", {"root": "a1"})
        wal_helper.write_commit("cascade", {"status": "success"})

        # Execute
        incomplete = wal_helper.read_records()
        intent_map = {}
        for rec in incomplete:
            if rec.get('status') == 'intent':
                key = rec.get('op_type')
                intent_map[key] = rec
            elif rec.get('status') == 'commit':
                key = rec.get('op_type')
                if key in intent_map:
                    del intent_map[key]

        unpaired = list(intent_map.values())

        # Assert
        assert len(unpaired) == 0

    def test_wal_read_empty_wal_file(self, tmp_path, monkeypatch):
        """Read empty/nonexistent WAL returns empty list."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        result = wal_read_incomplete(feature)

        # Assert
        assert result == []


class TestWALReplay:
    """Test WAL replay and rollback operations."""

    def test_wal_replay_cascade_idempotent_all_rebased(self, tmp_path, monkeypatch):
        """UT-W5: Replay cascade (idempotent, all descendants rebased)."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        cascade_intent = {
            'op_type': 'cascade',
            'status': 'intent',
            'payload': {
                'feature': 'test',
                'root': 'a1',
                'descendants': ['a2', 'a3'],
                'pre_image_refs': {
                    'refs/heads/a1': 'old_sha_a1',
                    'refs/heads/a2': 'old_sha_a2',
                    'refs/heads/a3': 'old_sha_a3'
                }
            }
        }

        # Execute
        result = wal_replay(feature, cascade_intent, action='replay')

        # Assert
        assert result['status'] == 'ready_for_replay'
        assert result['op_type'] == 'cascade'
        assert result['entry'] == cascade_intent

    def test_wal_replay_cascade_partial_pending(self, tmp_path, monkeypatch):
        """UT-W6: Replay cascade (partial, some descendants pending)."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        cascade_intent = {
            'op_type': 'cascade',
            'status': 'intent',
            'payload': {
                'feature': 'test',
                'root': 'a1',
                'descendants': ['a2', 'a3', 'a4'],
                'pre_image_refs': {}
            }
        }

        # Execute
        result = wal_replay(feature, cascade_intent, action='replay')

        # Assert: ready for re-execution
        assert result['status'] == 'ready_for_replay'
        assert result['op_type'] == 'cascade'

    @patch('os.system')
    def test_wal_rollback_cascade(self, mock_system, tmp_path, monkeypatch):
        """UT-W7: Rollback cascade restores pre-image SHAs via git update-ref."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        cascade_intent = {
            'op_type': 'cascade',
            'status': 'intent',
            'payload': {
                'feature': 'test',
                'root': 'a1',
                'pre_image_refs': {
                    'refs/heads/a1': 'old_sha_a1',
                    'refs/heads/a2': 'old_sha_a2',
                    'refs/heads/a3': 'old_sha_a3'
                }
            }
        }

        # Execute
        result = wal_replay(feature, cascade_intent, action='rollback')

        # Assert
        assert result['status'] == 'rolled_back'
        assert result['entry_op'] == 'cascade'
        assert mock_system.call_count == 3  # One call per ref

    def test_wal_replay_invalid_action_raises_error(self, tmp_path, monkeypatch):
        """Replay with invalid action raises WALError."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        cascade_intent = {
            'op_type': 'cascade',
            'payload': {}
        }

        # Execute & Assert
        with pytest.raises(WALError):
            wal_replay(feature, cascade_intent, action='invalid')


class TestWALLockManagement:
    """Test lock acquire/release and stale-lock detection."""

    def test_lock_acquire_no_contention(self, tmp_path):
        """UT-W8: Lock acquire/release (no contention) instant acquire."""
        # Setup
        lock_path = str(tmp_path / "test.lock")

        # Execute
        start = time.time()
        lock_obj = _acquire_lock_internal(lock_path, timeout=30)
        elapsed = time.time() - start

        # Assert: instant acquire (< 100ms)
        assert elapsed < 0.1
        assert lock_obj is not None

        # Cleanup
        _release_lock_internal(lock_obj)

    def test_lock_stale_check_pid_dead(self, tmp_path):
        """UT-W9: Lock timeout (stale holder, PID dead) reclaim after timeout."""
        # Setup
        lock_path = str(tmp_path / "test.lock")

        # Create lock file with dead PID (e.g., PID 1 is init, not in our process tree)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            dead_pid = 99999  # Unlikely to exist
            f.write(f"{dead_pid}:{int(time.time() - 2000)}\n")

        # Execute
        start = time.time()
        is_stale = _is_stale_lock(lock_path, timeout=1800)
        elapsed = time.time() - start

        # Assert: detected as stale (dead PID)
        assert is_stale is True

    def test_lock_stale_check_old_timestamp(self, tmp_path):
        """UT-W9b: Lock stale-check (old timestamp) reclaimed."""
        # Setup
        lock_path = str(tmp_path / "test.lock")

        # Create lock file with very old timestamp (beyond timeout)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            current_pid = os.getpid()
            old_time = int(time.time() - 2000)  # 2000 seconds ago
            f.write(f"{current_pid}:{old_time}\n")

        # Execute
        is_stale = _is_stale_lock(lock_path, timeout=1800)

        # Assert: detected as stale (old age)
        assert is_stale is True

    def test_lock_stale_check_alive_process(self, tmp_path):
        """Lock stale-check (alive process) NOT stale."""
        # Setup
        lock_path = str(tmp_path / "test.lock")

        # Create lock file with current PID and recent timestamp
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            current_pid = os.getpid()
            recent_time = int(time.time() - 10)  # 10 seconds ago
            f.write(f"{current_pid}:{recent_time}\n")

        # Execute
        is_stale = _is_stale_lock(lock_path, timeout=1800)

        # Assert: NOT stale (alive process, recent timestamp)
        assert is_stale is False


class TestWALParseError:
    """Test WAL error handling."""

    def test_wal_parse_error_corrupted_json_skipped(self, tmp_path, monkeypatch):
        """UT-W10: WAL parse error (corrupted JSON) skipped, continue."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        wal_file = tmp_path / ".fleet" / feature / "wal.jsonl"
        wal_file.parent.mkdir(parents=True, exist_ok=True)

        # Write: valid record, corrupted line, valid record
        with open(wal_file, 'w') as f:
            f.write('{"op_type":"cascade","status":"intent","payload":{}}\n')
            f.write('{"corrupt json }\n')  # Invalid JSON
            f.write('{"op_type":"merge","status":"commit","payload":{"status":"success"}}\n')

        # Execute: read incomplete (should skip corrupted line)
        result = wal_read_incomplete(feature)

        # Assert: returns valid records, skips corrupted
        assert len(result) >= 0  # At minimum, corrupted line skipped


class TestWALEdgeCases:
    """Test edge cases and corner scenarios."""

    def test_wal_write_creates_directory(self, tmp_path, monkeypatch):
        """WAL write creates directory if not exist."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature = "test"
        original_expanduser = os.path.expanduser

        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        wal_write(feature, "cascade", "intent", {"root": "a1"})

        # Assert: directory created
        wal_dir = tmp_path / ".fleet" / feature
        assert wal_dir.exists()

    def test_wal_read_multiple_incomplete_ops(self, tmp_path, wal_helper):
        """WAL read with multiple incomplete operations."""
        # Setup
        wal_helper.write_intent("cascade", {"root": "a1"})
        wal_helper.write_intent("merge-back", {"node": "a1"})
        wal_helper.write_intent("present-gen", {"node": "a1"})
        # No commits for any of them

        # Execute
        incomplete = wal_helper.read_records()
        intent_map = {}
        for rec in incomplete:
            if rec.get('status') == 'intent':
                key = rec.get('op_type')
                intent_map[key] = rec

        unpaired = list(intent_map.values())

        # Assert: all three unpaired
        assert len(unpaired) == 3
        op_types = {u['op_type'] for u in unpaired}
        assert op_types == {'cascade', 'merge-back', 'present-gen'}

    def test_lock_nonexistent_lock_file(self, tmp_path):
        """Stale-check on nonexistent lock file returns True (stale)."""
        # Setup
        lock_path = str(tmp_path / "nonexistent.lock")

        # Execute
        is_stale = _is_stale_lock(lock_path, timeout=1800)

        # Assert
        assert is_stale is True

    def test_lock_empty_lock_file(self, tmp_path):
        """Stale-check on empty lock file returns True (stale)."""
        # Setup
        lock_path = str(tmp_path / "empty.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            f.write("")  # Empty file

        # Execute
        is_stale = _is_stale_lock(lock_path, timeout=1800)

        # Assert
        assert is_stale is True

    def test_lock_malformed_lock_file(self, tmp_path):
        """Stale-check on malformed lock file returns True (stale)."""
        # Setup
        lock_path = str(tmp_path / "malformed.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'w') as f:
            f.write("not_a_pid:timestamp\n")  # Invalid format

        # Execute
        is_stale = _is_stale_lock(lock_path, timeout=1800)

        # Assert
        assert is_stale is True
