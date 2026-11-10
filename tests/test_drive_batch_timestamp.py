"""Pure offline metadata diagnostics; no iCloud requests or state changes."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_timestamp as timestamp
import drive_inventory as inventory


class TimestampClassifierTests(unittest.TestCase):
    def test_exact_match(self):
        value = '2026-11-09T11:50:00.123456789Z'
        self.assertEqual(
            timestamp.classify_modtime(value, value, is_dir=False),
            timestamp.EXACT_MATCH)

    def test_equivalent_timezone_offsets_are_representation_mismatches(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00.123456789Z',
            '2026-11-09T05:50:00.123456789-06:00',
            is_dir=False), timestamp.REPRESENTATION_MISMATCH)

    def test_equivalent_fractional_zero_padding_is_representation_only(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00.1Z',
            '2026-11-09T11:50:00.100000000+00:00',
            is_dir=True), timestamp.REPRESENTATION_MISMATCH)

    def test_one_nanosecond_difference_is_not_rounded_away(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00.123456789Z',
            '2026-11-09T11:50:00.123456788Z',
            is_dir=False), timestamp.SUBSECOND_MISMATCH)

    def test_truncated_fraction_can_hide_a_real_difference(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00.123456789Z',
            '2026-11-09T11:50:00.123456Z',
            is_dir=True), timestamp.SUBSECOND_MISMATCH)

    def test_file_second_difference_has_specific_code(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00Z', '2026-11-09T11:50:01Z',
            is_dir=False), timestamp.FILE_VALUE_MISMATCH)

    def test_directory_second_difference_has_specific_code(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00Z', '2026-11-09T11:49:59Z',
            is_dir=True), timestamp.DIRECTORY_VALUE_MISMATCH)

    def test_equal_wall_time_different_zone_is_not_equal_instant(self):
        self.assertEqual(timestamp.classify_modtime(
            '2026-11-09T11:50:00Z', '2026-11-09T11:50:00-06:00',
            is_dir=False), timestamp.FILE_VALUE_MISMATCH)

    def test_timezone_normalization_across_utc_midnight(self):
        self.assertEqual(timestamp.classify_modtime(
            '1970-01-01T00:00:00.000000001Z',
            '1969-12-31T19:00:00.000000001-05:00',
            is_dir=False), timestamp.REPRESENTATION_MISMATCH)

    def test_epoch_negative_seconds_and_nanos_exact(self):
        self.assertEqual(timestamp.instant_parts(
            '1969-12-31T23:59:59.999999999Z'), (-1, 999999999))
        self.assertEqual(timestamp.instant_parts(
            '1970-01-01T00:00:00Z'), (0, 0))

    def test_nine_digit_fraction_keeps_all_digits(self):
        self.assertEqual(timestamp.instant_parts(
            '1970-01-01T00:00:00.123456789+00:00'),
            (0, 123456789))
        self.assertEqual(timestamp.instant_parts(
            '1970-01-01T00:00:00.000000001Z'),
            (0, 1))

    def test_invalid_types_and_timestamps_fail_closed(self):
        invalid = (None, 0, True, b'2026-11-09T11:50:00Z',
                   '2026-11-09T11:50:00', '2026-11-09T25:50:00Z',
                   '2026-11-09T11:50:00.1234567890Z',
                   'private cloud secret', '2026-11-09T11:50:00+24:00')
        for bad in invalid:
            with self.subTest(value=type(bad).__name__):
                self.assertIsNone(timestamp.instant_parts(bad))
                self.assertEqual(timestamp.classify_modtime(
                    bad, '2026-11-09T11:50:00Z', is_dir=False),
                    timestamp.UNVERIFIED)

    def test_invalid_directory_flag_fails_closed(self):
        for flag in (None, 0, 1, 'true'):
            with self.subTest(flag=flag):
                self.assertEqual(timestamp.classify_modtime(
                    '2026-11-09T11:50:00Z',
                    '2026-11-09T11:50:00Z', is_dir=flag),
                    timestamp.UNVERIFIED)

    def test_classifier_returns_only_fixed_codes_no_private_timestamps(self):
        left = '2026-11-09T11:50:00Z'
        right = '2026-11-09T11:50:01Z'
        code = timestamp.classify_modtime(left, right, is_dir=False)
        self.assertEqual(code, timestamp.FILE_VALUE_MISMATCH)
        self.assertNotIn(left, code)
        self.assertNotIn(right, code)
        self.assertNotIn('/', code)

    def test_diagnostic_mode_never_writes_checkpoint_or_runs_subprocess(self):
        with (patch.object(inventory, 'save_progress',
                           side_effect=AssertionError('checkpoint write')),
              patch.object(inventory.subprocess, 'run',
                           side_effect=AssertionError('network command'))):
            result = timestamp.classify_modtime(
                '2026-11-09T11:50:00.1Z',
                '2026-11-09T11:50:00.2Z', is_dir=True)
        self.assertEqual(result, timestamp.SUBSECOND_MISMATCH)


if __name__ == '__main__':
    unittest.main()
