#!/usr/bin/env python3
"""Offline-only classification of iCloud Drive modification-time mismatches.

This module performs no network, subprocess, filesystem, or checkpoint IO.
Its output contains only fixed diagnostic codes. Classification does NOT
prove that batch metadata can replace the existing rclone record.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from drive_batch_checkpoint import valid_timestamp

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FRACTION = re.compile(r'\.(\d{1,9})(?:Z|[+-]\d{2}:\d{2})$')

EXACT_MATCH = 'BATCH_TIMESTAMP_EXACT_MATCH'
UNVERIFIED = 'BATCH_TIMESTAMP_UNVERIFIED'
REPRESENTATION_MISMATCH = 'BATCH_TIMESTAMP_REPRESENTATION_MISMATCH'
SUBSECOND_MISMATCH = 'BATCH_TIMESTAMP_SUBSECOND_MISMATCH'
FILE_VALUE_MISMATCH = 'BATCH_TIMESTAMP_FILE_VALUE_MISMATCH'
DIRECTORY_VALUE_MISMATCH = 'BATCH_TIMESTAMP_DIRECTORY_VALUE_MISMATCH'


def instant_parts(value: object) -> tuple[int, int] | None:
    """Return integral UTC seconds + nanoseconds, without float truncation.

    The valid_timestamp preflight enforces bounded RFC3339 fractional
    precision. Python datetime keeps only six decimal places, so extract
    all nine fractional digits separately instead of relying on microseconds.
    """
    if not valid_timestamp(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        whole_second = parsed.replace(microsecond=0).astimezone(timezone.utc)
        delta = whole_second - _EPOCH
        match = _FRACTION.search(value)
        nanoseconds = int(match.group(1).ljust(9, '0')) if match else 0
        return delta.days * 86400 + delta.seconds, nanoseconds
    except (AttributeError, ValueError, OverflowError, TypeError):
        return None


def classify_modtime(native: object, legacy: object, *, is_dir: bool) -> str:
    """Return a private-data-free status; never normalize records or approve.

    When seconds and nanoseconds match but source text differs, timestamps
    denote the same UTC instant. The *strict* migration gate must still
    reject it until the representation difference is independently explained.
    """
    if type(is_dir) is not bool:
        return UNVERIFIED
    native_parts = instant_parts(native)
    legacy_parts = instant_parts(legacy)
    if native_parts is None or legacy_parts is None:
        return UNVERIFIED
    if native == legacy:
        return EXACT_MATCH
    if native_parts == legacy_parts:
        return REPRESENTATION_MISMATCH
    if native_parts[0] == legacy_parts[0]:
        return SUBSECOND_MISMATCH
    return DIRECTORY_VALUE_MISMATCH if is_dir else FILE_VALUE_MISMATCH
