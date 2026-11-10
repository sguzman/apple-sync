"""Mock-only QA for dormant guarded replay worker body: no Apple traffic."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_worker_entry as entry

RECEIPT = Path('/run/user/1000/.apple-sync-replay-aBcDE123')
CATEGORIES = sorted(entry.worker.REPLAY_OUTCOMES)


def observation(outcome='REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED'):
    return {
        'version': 1, 'outcome': outcome,
        'directory_children_checked': 2,
        'directory_children_stable': 1,
        'directory_children_batch_only_difference': 1,
        'directory_children_single_read_difference': 0,
        'replay_requests_completed': 3, 'replay_folders_selected': 2,
        'measured_ip_bytes': None, 'ip_accounting_verified': False,
        'checkpoint_writes': 0, 'checkpoints_advanced': 0,
        'production_commit_authorized': False,
    }


class GuardedEntryTests(unittest.TestCase):
    def execute(self, *, preflight_error=None, worker_error=None,
                publish_error=None, failure_error=None, result=None):
        if result is None:
            result = observation()
        def check():
            self.events.append('preflight')
            if preflight_error is not None:
                raise preflight_error
            return SimpleNamespace(receipt_dir=RECEIPT)

        def replay():
            self.events.append('replay')
            if worker_error is not None:
                raise worker_error
            return result

        def publish_success(data, *, directory):
            self.events.append('success_receipt')
            self.success_data = data
            self.success_directory = directory
            if publish_error is not None:
                raise publish_error

        def publish_failure(code, *, directory):
            self.events.append('failure_receipt')
            self.failure_code = code
            self.failure_directory = directory
            if failure_error is not None:
                raise failure_error

        self.events = []
        self.success_data = None
        self.success_directory = None
        self.failure_code = None
        self.failure_directory = None
        out, err = StringIO(), StringIO()
        with (patch.object(entry.preflight, 'validate_own_process',
                           side_effect=check),
              patch.object(entry.worker, 'replay_once', side_effect=replay),
              patch.object(entry.worker, 'publish_observation',
                           side_effect=publish_success),
              patch.object(entry.worker, 'publish_failure',
                           side_effect=publish_failure),
              redirect_stdout(out), redirect_stderr(err)):
            exit_code = entry.guarded_once()
        return exit_code, out.getvalue(), err.getvalue()

    def test_preflight_precedes_every_read_and_receipt(self):
        code, _out, _err = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(self.events,
                         ['preflight', 'replay', 'success_receipt'])

    def test_valid_observation_is_published_once(self):
        code, _out, _err = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(self.events.count('success_receipt'), 1)
        self.assertEqual(self.success_data, observation())
        self.assertEqual(self.success_directory, RECEIPT)

    def test_success_stdout_is_aggregate_only(self):
        code, out, err = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertIn('REPLAY_WORKER_OBSERVATION:', out)
        self.assertIn('checkpoint_writes=0', out)
        self.assertIn('root_accounting_pending_verification', out)
        self.assertNotIn(str(RECEIPT), out)
        self.assertNotIn('equivalent=yes', out)

    def test_all_fixed_outcome_categories_are_supported(self):
        for name in CATEGORIES:
            with self.subTest(outcome=name):
                code, out, _err = self.execute(result=observation(name))
                self.assertEqual(code, 0)
                self.assertIn(name, out)
                self.assertNotIn('failure_receipt', self.events)

    def test_preflight_exception_blocks_all_backend_operations(self):
        code, out, err = self.execute(
            preflight_error=RuntimeError('/private/account/secret'))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(self.events, ['preflight'])
        self.assertIn('PREFLIGHT_UNVERIFIED', err)
        self.assertNotIn('private', err)

    def test_preflight_failure_does_not_publish_failure_receipt(self):
        self.execute(preflight_error=ValueError('unknown channel'))
        self.assertNotIn('failure_receipt', self.events)
        self.assertNotIn('replay', self.events)

    def test_preflight_refuses_before_any_apple_transport_even_if_mock_would_succeed(self):
        code, _out, _err = self.execute(
            preflight_error=entry.preflight.ReplayWorkerPreflightError(
                'REPLAY_WORKER_CGROUP_UNTRUSTED'))
        self.assertEqual(code, 2)
        self.assertEqual(self.events, ['preflight'])

    def test_replay_known_failure_generates_fixed_category_receipt(self):
        error = entry.worker.ReplayWorkerError('REPLAY_BACKUP_INTERRUPTED')
        code, out, err = self.execute(worker_error=error)
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(self.failure_code, 'REPLAY_BACKUP_INTERRUPTED')
        self.assertEqual(self.events,
                         ['preflight', 'replay', 'failure_receipt'])
        self.assertEqual(self.failure_directory, RECEIPT)
        self.assertIn('REPLAY_BACKUP_INTERRUPTED', err)

    def test_all_worker_failure_categories_are_preserved(self):
        for reason in sorted(entry.worker.REPLAY_FAILURES):
            with self.subTest(reason=reason):
                code, _out, _err = self.execute(
                    worker_error=entry.worker.ReplayWorkerError(reason))
                self.assertEqual(code, 2)
                self.assertEqual(self.failure_code, reason)

    def test_unknown_worker_failure_is_reduced_to_local_category(self):
        code, out, err = self.execute(
            worker_error=entry.worker.ReplayWorkerError('/private/cloud/file'))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(self.failure_code, 'REPLAY_LOCAL_FAILURE')
        self.assertNotIn('private', err)

    def test_unexpected_transport_exception_is_sanitized(self):
        code, out, err = self.execute(
            worker_error=OSError('secret token=private'))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(self.failure_code, 'REPLAY_LOCAL_FAILURE')
        self.assertNotIn('secret', err)
        self.assertNotIn('private', err)

    def test_worker_observation_schema_rejection_fails_closed(self):
        code, _out, err = self.execute(
            publish_error=entry.worker.ReplayWorkerError(
                'REPLAY_RESULT_INVALID'))
        self.assertEqual(code, 2)
        self.assertEqual(self.failure_code, 'REPLAY_RESULT_INVALID')
        self.assertIn('REPLAY_RESULT_INVALID', err)

    def test_success_receipt_filesystem_error_generates_fixed_failure(self):
        code, _out, err = self.execute(
            publish_error=PermissionError('/private/denied'))
        self.assertEqual(code, 2)
        self.assertEqual(self.failure_code, 'REPLAY_LOCAL_FAILURE')
        self.assertNotIn('private', err)

    def test_failed_failure_receipt_still_returns_nonzero(self):
        code, out, err = self.execute(
            worker_error=RuntimeError('private'),
            failure_error=OSError('cannot write private file'))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('REPLAY_WORKER_FAILED: REPLAY_LOCAL_FAILURE', err)

    def test_unwritable_channel_never_produces_success(self):
        code, _out, _err = self.execute(
            publish_error=OSError('channel missing'),
            failure_error=OSError('channel missing'))
        self.assertEqual(code, 2)
        self.assertIn('failure_receipt', self.events)

    def test_failure_path_does_not_publish_success(self):
        code, _out, _err = self.execute(worker_error=RuntimeError('no Apple'))
        self.assertEqual(code, 2)
        self.assertNotIn('success_receipt', self.events)

    def test_success_path_does_not_publish_failure(self):
        code, _out, _err = self.execute()
        self.assertEqual(code, 0)
        self.assertNotIn('failure_receipt', self.events)

    def test_untrusted_outcome_does_not_exit_zero_even_if_publisher_is_mocked(self):
        bad = observation('/private/unrecognized_outcome')
        code, _out, err = self.execute(result=bad)
        self.assertEqual(code, 2)
        self.assertNotIn('private', err)
        self.assertIn('REPLAY_LOCAL_FAILURE', err)

    def test_none_result_does_not_exit_zero(self):
        # The real strict publisher would reject this earlier; the mocked
        # writer deliberately accepts it to verify the final fixed code.
        code, _out, err = self.execute(result=[])
        self.assertEqual(code, 2)
        self.assertIn('REPLAY_LOCAL_FAILURE', err)

    def test_worker_stays_dormant_and_entry_has_no_cli(self):
        body = Path(entry.__file__).read_text(encoding='utf-8')
        worker = Path(entry.worker.__file__).read_text(encoding='utf-8')
        self.assertNotIn("if __name__ == '__main__'", body)
        self.assertNotIn('subprocess.run(', body)
        self.assertNotIn('ledger.begin(', body)
        self.assertIn('REPLAY_WORKER_DORMANT', worker)


if __name__ == '__main__':
    unittest.main()
