import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from sys import path as sys_path
sys_path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import drive_meter_budget as budget


class MeterBudgetTests(unittest.TestCase):
    def test_initialization_is_private_and_empty(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root) as ledger:
                self.assertEqual(ledger.used, 0)
                self.assertEqual(ledger.remaining, budget.CAP_BYTES)
                self.assertEqual(ledger.data['passes'], 0)
                ledger.check_ready()
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                state = root / budget.STATE_NAME
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(
                    (root / budget.LOCK_NAME).stat().st_mode), 0o600)

    def test_multiple_passes_persist_across_process_boundaries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root) as ledger:
                ledger.begin('apple-sync-drive-metered-1-0.service')
                ledger.complete(469066, 368241)
            with budget.BudgetLedger(root) as ledger:
                self.assertEqual(ledger.used, 837307)
                ledger.begin('apple-sync-drive-metered-2-0.service')
                ledger.complete(1000, 2000)
            with budget.BudgetLedger(root) as ledger:
                self.assertEqual(ledger.data['passes'], 2)
                self.assertEqual(ledger.data['ingress_bytes'], 470066)
                self.assertEqual(ledger.data['egress_bytes'], 370241)
                self.assertEqual(ledger.used, 840307)
                self.assertIn('remaining=99159693 bytes', ledger.summary())

    def test_interrupted_pending_pass_blocks_all_future_runs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root) as ledger:
                ledger.begin('apple-sync-drive-metered-3-0.service')
                self.assertIsNotNone(ledger.data['pending'])
            with budget.BudgetLedger(root) as ledger:
                with self.assertRaisesRegex(
                        budget.BudgetError, 'PREVIOUS_RUN_UNRECONCILED'):
                    ledger.check_ready()
                with self.assertRaisesRegex(
                        budget.BudgetError, 'PREVIOUS_RUN_UNRECONCILED'):
                    ledger.begin('apple-sync-drive-metered-4-0.service')
                self.assertEqual(ledger.data['passes'], 0)

    def test_exact_remaining_margin_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root) as ledger:
                ledger.begin('apple-sync-drive-metered-1-0.service')
                ledger.complete(budget.CAP_BYTES - budget.RESERVE_BYTES, 0)
                self.assertEqual(ledger.remaining, budget.RESERVE_BYTES)
                with self.assertRaisesRegex(
                        budget.BudgetError, 'CUMULATIVE_METER_BUDGET_EXHAUSTED'):
                    ledger.check_ready()

    def test_overshoot_is_recorded_and_future_scans_blocked(self):
        with TemporaryDirectory() as directory:
            with budget.BudgetLedger(Path(directory) / 'meter') as ledger:
                ledger.begin('apple-sync-drive-metered-1-0.service')
                ledger.complete(budget.CAP_BYTES + 1234, 100)
                self.assertGreater(ledger.used, budget.CAP_BYTES)
                self.assertEqual(ledger.remaining, 0)
                with self.assertRaisesRegex(
                        budget.BudgetError, 'CUMULATIVE_METER_BUDGET_EXHAUSTED'):
                    ledger.check_ready()

    def test_second_controller_cannot_bypass_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root):
                with self.assertRaisesRegex(
                        budget.BudgetError, 'ANOTHER_METERED_INVENTORY_RUNNING'):
                    with budget.BudgetLedger(root):
                        pass

    def test_symlinked_directory_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / 'real'
            real.mkdir(mode=0o700)
            (root / 'link').symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(
                    budget.BudgetError, 'UNSAFE_METER_BUDGET_DIRECTORY'):
                with budget.BudgetLedger(root / 'link'):
                    pass

    def test_symlinked_state_file_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            root.mkdir(mode=0o700)
            target = Path(directory) / 'outside.json'
            target.write_text(json.dumps(budget._initial()))
            (root / budget.STATE_NAME).symlink_to(target)
            with self.assertRaisesRegex(
                    budget.BudgetError, 'UNSAFE_METER_BUDGET_FILE'):
                with budget.BudgetLedger(root):
                    pass
            self.assertEqual(json.loads(target.read_text()), budget._initial())

    def test_group_readable_state_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root):
                pass
            state = root / budget.STATE_NAME
            state.chmod(0o640)
            with self.assertRaisesRegex(
                    budget.BudgetError, 'UNSAFE_METER_BUDGET_FILE'):
                with budget.BudgetLedger(root):
                    pass

    def test_invalid_state_is_not_reset(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root):
                pass
            state = root / budget.STATE_NAME
            state.write_text('not JSON')
            with self.assertRaisesRegex(
                    budget.BudgetError, 'UNREADABLE_METER_BUDGET'):
                with budget.BudgetLedger(root):
                    pass
            self.assertEqual(state.read_text(), 'not JSON')

    def test_state_cap_mismatch_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root):
                pass
            state = root / budget.STATE_NAME
            data = json.loads(state.read_text())
            data['cap_bytes'] *= 10
            state.write_text(json.dumps(data))
            with self.assertRaisesRegex(
                    budget.BudgetError, 'METER_BUDGET_VERSION_OR_CAP_MISMATCH'):
                with budget.BudgetLedger(root):
                    pass

    def test_invalid_measurement_does_not_clear_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'meter'
            with budget.BudgetLedger(root) as ledger:
                ledger.begin('apple-sync-drive-metered-1-0.service')
                with self.assertRaisesRegex(
                        budget.BudgetError, 'INVALID_METERED_MEASUREMENT'):
                    ledger.complete(True, 1)
            with budget.BudgetLedger(root) as ledger:
                self.assertIsNotNone(ledger.data['pending'])

    def test_commit_without_pending_fails(self):
        with TemporaryDirectory() as directory:
            with budget.BudgetLedger(Path(directory) / 'meter') as ledger:
                with self.assertRaisesRegex(
                        budget.BudgetError, 'NO_METERED_RUN_TO_COMMIT'):
                    ledger.complete(1, 2)


if __name__ == '__main__':
    unittest.main()
