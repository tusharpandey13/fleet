"""
Unit tests for fleet_present.py (Unit 10).

Covers UT-P1..UT-P6: presentation branch generation from pinned plan,
deterministic expected-tree computation (UT-P5), plan curation, lease-fail
(FLEET_020), PR creation, and staleness marking.

Every test invokes a REAL function from fleet_present (present_gen or
_compute_expected_tree) and asserts on its return value, raised exception, or
its side effect on the manifest dict. Git/gh, WAL, lock, and manifest I/O are
mocked ONLY at their boundaries; present_gen itself runs for real.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

import fleet_present
from fleet_present import (
    present_gen,
    _compute_expected_tree,
    PresentError,
    LeaseFailError,
)


def _manifest_with_plan(plan=None, pr=None):
    """Manifest whose node a1 has a pinned presentation plan."""
    present = {'branch': 'present/pay/a1'}
    if plan is not None:
        present['plan'] = plan
    if pr is not None:
        present['pr'] = pr
    return {
        'feature': 'pay',
        'canonical': 'main',
        'nodes': {
            'a1': {
                'kind': 'feature', 'desc': 'Feature A1', 'base': None,
                'ghost': 'ghost/pay/a1', 'status': 'draft',
                'range': {'base_sha': 'abc123', 'tip_sha': 'def456'},
                'present': dict(present),
            }
        },
    }


def _git_side_effect(cmd, **kwargs):
    """Canned subprocess.run for git/gh: rev-list yields commits, all succeed."""
    result = MagicMock()
    result.returncode = 0
    result.stderr = ''
    if cmd[:2] == ['git', 'rev-list']:
        result.stdout = 'c1\nc2\n'
    elif cmd[:1] == ['gh']:
        result.stdout = 'https://github.com/org/repo/pull/123\n'
    else:
        result.stdout = ''
    return result


class TestComputeExpectedTree:
    """UT-P5: deterministic tree computation (real _compute_expected_tree)."""

    def test_same_plan_yields_identical_tree(self):
        """UT-P5: identical plan+range -> byte-identical result on repeat."""
        plan = [{'name': 'docs', 'sha_range': 'a..b'},
                {'name': 'impl', 'sha_range': 'b..c'}]
        rng = {'base_sha': 'a', 'tip_sha': 'c'}

        t1 = _compute_expected_tree(plan, rng)
        t2 = _compute_expected_tree(plan, rng)

        assert t1 == t2
        assert isinstance(t1, str) and len(t1) == 40

    def test_different_plan_yields_different_tree(self):
        """UT-P5: changing the plan changes the computed tree (real fn is sensitive)."""
        rng = {'base_sha': 'a', 'tip_sha': 'c'}
        t1 = _compute_expected_tree(
            [{'name': 'docs', 'sha_range': 'a..b'}], rng)
        t2 = _compute_expected_tree(
            [{'name': 'impl', 'sha_range': 'a..b'}], rng)
        assert t1 != t2

    def test_plan_order_is_significant(self):
        """UT-P5: reordering plan items changes tree (order-sensitive determinism)."""
        rng = {'base_sha': 'a', 'tip_sha': 'c'}
        p1 = [{'name': 'x', 'sha_range': 'a..b'}, {'name': 'y', 'sha_range': 'b..c'}]
        p2 = [{'name': 'y', 'sha_range': 'b..c'}, {'name': 'x', 'sha_range': 'a..b'}]
        assert _compute_expected_tree(p1, rng) != _compute_expected_tree(p2, rng)


class TestPresentGenSuccess:
    """UT-P1/P2: real present_gen happy path with pinned plan."""

    def _run(self, manifest):
        writes = {}

        def _capture_write(feature, obj):
            writes['manifest'] = obj

        with patch('fleet_present.manifest_read', return_value=manifest), \
             patch('fleet_present.manifest_write_atomic', side_effect=_capture_write), \
             patch('fleet_present.wal_write'), \
             patch('fleet_present.subprocess.run', side_effect=_git_side_effect):
            result = present_gen('pay', 'a1', lock_obj=MagicMock())
        return result, writes.get('manifest', manifest)

    def test_returns_published_status_and_branch(self):
        """UT-P1: real present_gen publishes and returns branch name."""
        manifest = _manifest_with_plan(plan=[{'name': 'all', 'sha_range': 'abc123..def456'}])
        result, _ = self._run(manifest)
        assert result['status'] == 'published'
        assert result['branch'] == 'present/pay/a1'

    def test_marks_present_not_stale(self):
        """UT-P6: after real present_gen, node present.stale set False + source tip pinned."""
        manifest = _manifest_with_plan(plan=[{'name': 'all', 'sha_range': 'abc123..def456'}])
        _, written = self._run(manifest)
        node = written['nodes']['a1']
        assert node['present']['stale'] is False
        assert node['present']['source_tip_sha'] == 'def456'

    def test_creates_pr_on_first_publish(self):
        """UT-P1: no existing PR -> real present_gen parses gh output, records PR 123."""
        manifest = _manifest_with_plan(plan=[{'name': 'all', 'sha_range': 'abc123..def456'}])
        result, _ = self._run(manifest)
        assert result['pr'] == 123

    def test_does_not_recreate_existing_pr(self):
        """UT-P1: existing PR reused; real present_gen keeps prior number."""
        manifest = _manifest_with_plan(
            plan=[{'name': 'all', 'sha_range': 'abc123..def456'}], pr=555)
        result, _ = self._run(manifest)
        assert result['pr'] == 555


class TestPresentGenCuration:
    """UT-P1: plan curation prompt path (real present_gen reads input)."""

    def test_curation_cancelled_when_empty(self):
        """UT-P1: no plan pinned + empty curation input -> real fn returns cancel error."""
        manifest = _manifest_with_plan(plan=None)  # no plan -> curation prompt
        with patch('fleet_present.manifest_read', return_value=manifest), \
             patch('fleet_present.wal_write'), \
             patch('fleet_present.manifest_write_atomic'), \
             patch('fleet_present.subprocess.run', side_effect=_git_side_effect), \
             patch('builtins.input', return_value=''):  # empty -> finish with no plan
            result = present_gen('pay', 'a1', lock_obj=MagicMock())
        assert result == {'error': 'Plan curation cancelled'}

    def test_curated_plan_is_used(self):
        """UT-P1: user enters one plan line -> real fn proceeds to publish."""
        manifest = _manifest_with_plan(plan=None)
        with patch('fleet_present.manifest_read', return_value=manifest), \
             patch('fleet_present.wal_write'), \
             patch('fleet_present.manifest_write_atomic'), \
             patch('fleet_present.subprocess.run', side_effect=_git_side_effect), \
             patch('builtins.input', side_effect=['docs:abc123..def456', '']):
            result = present_gen('pay', 'a1', lock_obj=MagicMock())
        assert result['status'] == 'published'


class TestPresentGenErrors:
    """UT-P4: lease-fail + error paths (real present_gen)."""

    def test_lease_fail_on_force_push(self):
        """UT-P4: force-push rejected -> real present_gen raises LeaseFailError."""
        manifest = _manifest_with_plan(plan=[{'name': 'all', 'sha_range': 'abc123..def456'}])

        def _push_rejected(cmd, **kwargs):
            r = MagicMock(returncode=0, stderr='', stdout='')
            if cmd[:2] == ['git', 'rev-list']:
                r.stdout = 'c1\n'
            if cmd[:3] == ['git', 'push', '--force-with-lease']:
                r.returncode = 1
                r.stderr = '! [rejected] stale info; remote tip changed'
                # present_gen uses check=True on push, so raise CalledProcessError
                import subprocess as _sp
                raise _sp.CalledProcessError(1, cmd, stderr=r.stderr)
            return r

        with patch('fleet_present.manifest_read', return_value=manifest), \
             patch('fleet_present.wal_write'), \
             patch('fleet_present.manifest_write_atomic'), \
             patch('fleet_present.subprocess.run', side_effect=_push_rejected):
            with pytest.raises(LeaseFailError):
                present_gen('pay', 'a1', lock_obj=MagicMock())

    def test_unknown_node_raises(self):
        """UT-P1: node absent from manifest -> real present_gen raises PresentError."""
        manifest = _manifest_with_plan(plan=[{'name': 'all', 'sha_range': 'abc123..def456'}])
        with patch('fleet_present.manifest_read', return_value=manifest), \
             patch('fleet_present.wal_write'), \
             patch('fleet_present.manifest_write_atomic'), \
             patch('fleet_present.subprocess.run', side_effect=_git_side_effect):
            with pytest.raises(PresentError):
                present_gen('pay', 'zzz', lock_obj=MagicMock())
