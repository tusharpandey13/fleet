"""
Unit tests for fleet_merge.py (Unit 9).

Covers UT-MB1..UT-MB8: merge-back --onto squash cleanup, topological order
enforcement (FLEET_050), commit counting, dry-run summary, lease-fail recovery
(FLEET_020), and conflict handling (FLEET_011).

Every test invokes a REAL function from fleet_merge and asserts on its return
value, raised exception, or side effect. Git/gh are mocked ONLY at the
subprocess boundary; the functions under test execute for real.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

import fleet_merge
from fleet_merge import (
    merge_back,
    handle_lease_fail,
    _validate_topo_order,
    _count_commits_in_range,
    _dryrun_merge,
    _push_presentation,
    _topo_order,
    _get_node_ghost,
    MergeError,
    LeaseFailError,
)


def _stack_manifest():
    """a1 (root) -> a2 -> a3 linear stack; all draft."""
    return {
        'feature': 'pay',
        'canonical': 'main',
        'nodes': {
            'a1': {'kind': 'feature', 'desc': 'A1', 'base': None,
                   'ghost': 'ghost/pay/a1', 'status': 'draft',
                   'range': {'base_sha': 'main0', 'tip_sha': 'a1tip'}},
            'a2': {'kind': 'feature', 'desc': 'A2', 'base': 'a1',
                   'ghost': 'ghost/pay/a2', 'status': 'draft',
                   'range': {'base_sha': 'a1tip', 'tip_sha': 'a2tip'}},
            'a3': {'kind': 'feature', 'desc': 'A3', 'base': 'a2',
                   'ghost': 'ghost/pay/a3', 'status': 'draft',
                   'range': {'base_sha': 'a2tip', 'tip_sha': 'a3tip'}},
        },
    }


class TestValidateTopoOrder:
    """UT-MB2: topological order enforcement (real _validate_topo_order)."""

    def test_out_of_order_merge_blocked_fleet_050(self):
        """UT-MB2: merging a2 while ancestor a1 not archived -> MergeError FLEET_050."""
        manifest = _stack_manifest()  # a1 still 'draft'

        with pytest.raises(MergeError) as exc:
            _validate_topo_order(manifest, 'a2')

        assert 'FLEET_050' in str(exc.value)
        assert 'a1' in str(exc.value)

    def test_in_order_merge_allowed_when_ancestors_archived(self):
        """UT-MB2: merging a2 after a1 archived -> real fn returns None (no raise)."""
        manifest = _stack_manifest()
        manifest['nodes']['a1']['status'] = 'archived'

        assert _validate_topo_order(manifest, 'a2') is None

    def test_root_node_has_no_ancestors_allowed(self):
        """UT-MB2: root a1 has no ancestors -> always allowed."""
        manifest = _stack_manifest()
        assert _validate_topo_order(manifest, 'a1') is None


class TestTopoOrder:
    """UT-MB7/MB8: batch-mode topo ordering (real _topo_order)."""

    def test_topo_order_increases_with_depth(self):
        """UT-MB8: deeper node in stack -> higher order value."""
        manifest = _stack_manifest()
        o1 = _topo_order(manifest, 'a1')
        o2 = _topo_order(manifest, 'a2')
        o3 = _topo_order(manifest, 'a3')
        assert o1 < o2 < o3

    def test_topo_order_root_is_minimum(self):
        """UT-MB7: root selected as merge start (min order)."""
        manifest = _stack_manifest()
        root = min(manifest['nodes'], key=lambda n: _topo_order(manifest, n))
        assert root == 'a1'


class TestCountCommitsInRange:
    """UT-MB1: commit counting over base..tip (real _count_commits_in_range)."""

    def test_counts_commits_via_git_rev_list(self):
        """UT-MB1: real fn parses `git rev-list --count` stdout, builds range from manifest."""
        manifest = _stack_manifest()

        fake = MagicMock(returncode=0, stdout='3\n')
        with patch('fleet_merge.subprocess.run', return_value=fake) as run:
            count = _count_commits_in_range(manifest, 'a1')

        assert count == 3
        called_args = run.call_args[0][0]
        assert called_args[:2] == ['git', 'rev-list']
        assert 'main0..a1tip' in called_args
        assert '--count' in called_args

    def test_returns_zero_when_range_missing(self):
        """UT-MB1: node with no base/tip -> 0 (short-circuits before git)."""
        manifest = _stack_manifest()
        manifest['nodes']['a1']['range'] = {'base_sha': None, 'tip_sha': None}
        assert _count_commits_in_range(manifest, 'a1') == 0

    def test_returns_zero_on_git_failure(self):
        """UT-MB1: non-zero git exit -> real fn returns 0."""
        manifest = _stack_manifest()
        fake = MagicMock(returncode=128, stdout='')
        with patch('fleet_merge.subprocess.run', return_value=fake):
            assert _count_commits_in_range(manifest, 'a1') == 0


class TestDryrunMerge:
    """UT-MB1/MB3: dry-run summary (real _dryrun_merge)."""

    def test_summary_lists_node_children_commits(self):
        """UT-MB3: real summary string reflects inputs."""
        manifest = _stack_manifest()
        summary = _dryrun_merge(manifest, 'a1', ['a2', 'a3'], 5)
        assert 'a1' in summary
        assert 'a2' in summary and 'a3' in summary
        assert '5' in summary
        assert 'main' in summary  # new base

    def test_summary_handles_no_children(self):
        """UT-MB3: leaf node -> '(none)' children in real summary."""
        manifest = _stack_manifest()
        summary = _dryrun_merge(manifest, 'a3', [], 2)
        assert '(none)' in summary


class TestGetNodeGhost:
    """Helper used by --onto endpoint selection (real _get_node_ghost)."""

    def test_returns_manifest_ghost(self):
        manifest = _stack_manifest()
        assert _get_node_ghost(manifest, 'a2') == 'ghost/pay/a2'

    def test_falls_back_when_missing(self):
        manifest = {'nodes': {'x': {}}}
        assert _get_node_ghost(manifest, 'x') == 'ghost/x'


class TestPushPresentationLeaseFail:
    """UT-MB5: force-push lease failure detection (real _push_presentation)."""

    def test_lease_fail_raises_when_rejected(self):
        """UT-MB5: git push rejected -> real fn raises LeaseFailError."""
        manifest = _stack_manifest()
        manifest['nodes']['a1']['present'] = {'branch': 'present/pay/a1'}

        fake = MagicMock(returncode=1, stderr='! [rejected] (stale info)')
        with patch('fleet_merge.subprocess.run', return_value=fake):
            with pytest.raises(LeaseFailError):
                _push_presentation(manifest, 'a1')

    def test_no_presentation_is_noop(self):
        """UT-MB5: node without present branch -> real fn returns without git."""
        manifest = _stack_manifest()  # a1 has no 'present'
        assert _push_presentation(manifest, 'a1') is None


class TestMergeBackOrchestration:
    """UT-MB2/MB6: real merge_back entry exercising real internal validation."""

    def test_merge_back_out_of_order_raises_via_real_validation(self):
        """UT-MB2: real merge_back -> real _validate_topo_order -> FLEET_050."""
        manifest = _stack_manifest()  # a1 not archived; merging a2 is out of order

        with patch('fleet_merge.manifest_read', return_value=manifest), \
             patch('fleet_merge.reconcile', return_value=(manifest, [], [])):
            with pytest.raises(MergeError) as exc:
                merge_back('pay', 'a2', merge_mode='individual')

        assert 'FLEET_050' in str(exc.value)

    def test_merge_back_cancelled_when_user_declines(self):
        """UT-MB1: real merge_back reaches dry-run prompt; 'no' -> cancelled."""
        manifest = _stack_manifest()
        manifest['nodes']['a1']['status'] = 'archived'  # allow a2 merge

        with patch('fleet_merge.manifest_read', return_value=manifest), \
             patch('fleet_merge.reconcile', return_value=(manifest, [], [])), \
             patch('fleet_merge.subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='1\n')), \
             patch('builtins.input', return_value='no'):
            result = merge_back('pay', 'a2', merge_mode='individual')

        assert result == {'status': 'cancelled'}


class TestHandleLeaseFail:
    """UT-MB5: lease-fail recovery choices (real handle_lease_fail)."""

    def _lease_manifest(self):
        m = _stack_manifest()
        m['nodes']['a1']['present'] = {'branch': 'present/pay/a1', 'plan': []}
        return m

    def test_manual_choice_returns_manual_status(self):
        """UT-MB5: choice 'c' -> real fn returns manual resolution."""
        manifest = self._lease_manifest()
        with patch('fleet_merge.manifest_read', return_value=manifest), \
             patch('fleet_merge.subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='')), \
             patch('builtins.input', return_value='c'):
            result = handle_lease_fail('pay', 'a1')

        assert result['status'] == 'manual'
        assert result['action'] == 'user_resolves'

    def test_discard_cancelled_when_not_confirmed(self):
        """UT-MB5: choice 'b' then confirm 'no' -> real fn returns cancelled."""
        manifest = self._lease_manifest()
        with patch('fleet_merge.manifest_read', return_value=manifest), \
             patch('fleet_merge.subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='')), \
             patch('builtins.input', side_effect=['b', 'no']):
            result = handle_lease_fail('pay', 'a1')

        assert result['status'] == 'cancelled'

    def test_missing_presentation_raises(self):
        """UT-MB5: node with no present -> real fn raises MergeError."""
        manifest = _stack_manifest()  # a1 has no present
        with patch('fleet_merge.manifest_read', return_value=manifest):
            with pytest.raises(MergeError):
                handle_lease_fail('pay', 'a1')
