#!/usr/bin/env python3
"""Private, persistent accounting ledger for explicitly metered Drive scans.

This is a cumulative *measured* budget, not a kernel-enforced byte quota.
If the controlling process terminates while a run is pending, the ledger
deliberately remains blocked until an operator reconciles it. Never infer
that an interrupted run used zero bytes.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time

ROOT = Path('/var/lib/apple-sync-drive-metered')
STATE_NAME = 'budget-v1.json'
LOCK_NAME = 'budget.lock'
CAP_BYTES = 100_000_000  # 100 MB measured ingress + egress, from ledger creation
RESERVE_BYTES = 2_000_000  # sampled controller cutoff margin, not strict
VERSION = 1


class BudgetError(Exception):
    """Sanitized local-state or budget failure."""


def _secure_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
            info.st_mode & 0o077 or info.st_nlink != 1):
        raise BudgetError('UNSAFE_METER_BUDGET_FILE')


def _valid(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**64 - 1


def _initial() -> dict:
    return {'version': VERSION, 'cap_bytes': CAP_BYTES,
            'ingress_bytes': 0, 'egress_bytes': 0,
            'passes': 0, 'pending': None}


def _check(data: object) -> dict:
    if not isinstance(data, dict) or set(data) != set(_initial()):
        raise BudgetError('INVALID_METER_BUDGET')
    if data['version'] != VERSION or data['cap_bytes'] != CAP_BYTES:
        raise BudgetError('METER_BUDGET_VERSION_OR_CAP_MISMATCH')
    if not all(_valid(data[k]) for k in
               ('ingress_bytes', 'egress_bytes', 'passes')):
        raise BudgetError('INVALID_METER_BUDGET_TOTAL')
    pending = data['pending']
    if pending is not None and not (isinstance(pending, dict)
        and set(pending) == {'unit', 'started'}
        and isinstance(pending['unit'], str)
        and pending['unit'].startswith('apple-sync-drive-metered-')
        and pending['unit'].endswith('.service')
        and _valid(pending['started'])):
        raise BudgetError('INVALID_METER_BUDGET_PENDING')
    return data


class BudgetLedger:
    """Exclusive root-owned ledger lock remains held throughout all passes."""

    def __init__(self, root: Path = ROOT):
        self.root = root
        self.lock_fd: int | None = None
        self.data: dict | None = None

    def __enter__(self) -> 'BudgetLedger':
        if self.root.is_symlink():
            raise BudgetError('UNSAFE_METER_BUDGET_DIRECTORY')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o077):
            raise BudgetError('UNSAFE_METER_BUDGET_DIRECTORY')
        lock = self.root / LOCK_NAME
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock_fd = fd
        try:
            _secure_file(lock)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BudgetError('ANOTHER_METERED_INVENTORY_RUNNING') from exc
            state = self.root / STATE_NAME
            if state.exists() or state.is_symlink():
                _secure_file(state)
                try:
                    self.data = _check(json.loads(state.read_text('utf-8')))
                except (ValueError, UnicodeError, OSError) as exc:
                    raise BudgetError('UNREADABLE_METER_BUDGET') from exc
            else:
                self.data = _initial()
                self._write()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def _write(self) -> None:
        assert self.data is not None
        _check(self.data)
        encoded = (json.dumps(self.data, sort_keys=True) + '\n').encode()
        fd, temp = tempfile.mkstemp(prefix='.budget-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.root / STATE_NAME)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @property
    def used(self) -> int:
        assert self.data is not None
        return self.data['ingress_bytes'] + self.data['egress_bytes']

    @property
    def remaining(self) -> int:
        return max(0, CAP_BYTES - self.used)

    def check_ready(self) -> None:
        assert self.data is not None
        if self.data['pending'] is not None:
            raise BudgetError('PREVIOUS_RUN_UNRECONCILED_NO_NEW_SCANS')
        if self.remaining <= RESERVE_BYTES:
            raise BudgetError('CUMULATIVE_METER_BUDGET_EXHAUSTED')

    def begin(self, unit: str) -> None:
        self.check_ready()
        if not (unit.startswith('apple-sync-drive-metered-')
                and unit.endswith('.service')):
            raise BudgetError('UNSAFE_METERED_UNIT')
        assert self.data is not None
        self.data['pending'] = {'unit': unit, 'started': int(time.time())}
        self._write()  # durable *before* any Apple network requests

    def complete(self, ingress: int, egress: int) -> None:
        assert self.data is not None
        if self.data['pending'] is None:
            raise BudgetError('NO_METERED_RUN_TO_COMMIT')
        if not _valid(ingress) or not _valid(egress):
            raise BudgetError('INVALID_METERED_MEASUREMENT')
        self.data['ingress_bytes'] += ingress
        self.data['egress_bytes'] += egress
        self.data['passes'] += 1
        self.data['pending'] = None
        self._write()

    def summary(self) -> str:
        assert self.data is not None
        return (f'CUMULATIVE MEASURED: ingress={self.data["ingress_bytes"]} bytes, '
                f'egress={self.data["egress_bytes"]} bytes, '
                f'total={self.used} bytes, remaining={self.remaining} bytes, '
                f'passes={self.data["passes"]}, '
                f'pending={"yes" if self.data["pending"] is not None else "no"}')
