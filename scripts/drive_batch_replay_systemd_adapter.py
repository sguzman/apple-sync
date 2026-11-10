#!/usr/bin/env python3
"""Uninstalled systemd IO adapter for the manual iCloud replay experiment.

THIS IS NOT A LIVE CONTROLLER. The only executor is an injected callable;
there is no subprocess import, CLI, service installation or direct runner.
The future root-owned wrapper must separately provide the real executor,
the existing exclusive cumulative BudgetLedger, the trusted backup/timer
guards, and the receipt reader. The worker itself remains dormant.

No network, root commands, or archive writes occur by importing this module.
The soft IPAccounting threshold is advisory, not a kernel-enforced quota.
"""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import stat

import drive_batch_replay_systemd_plan as plan
import drive_batch_replay_accounting as accounting


class ReplayAdapterError(Exception):
    """Fixed error code only, never any stdout, stderr or private paths."""


def _is_trusted_directory(path: Path, owner: int, *, private: bool) -> bool:
    try:
        info = os.lstat(path)  # lstat refuses to conceal symlink ancestors
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
        return False
    mode = stat.S_IMODE(info.st_mode)
    return mode == 0o700 if private else not (mode & 0o022)


def require_private_channel(*, uid: int, directory: Path) -> None:
    """Validate fixed /run/user/<uid> channel before any worker start.

    No lstat result is cached across worker start. This does not create a
    directory or grant permissions; a future root wrapper must prepare
    it exclusively with secure ownership and mode first.
    """
    try:
        plan._channel(directory, uid)
    except plan.ReplaySystemdPlanError:
        raise ReplayAdapterError('REPLAY_CHANNEL_UNTRUSTED') from None
    ancestors = (Path('/run'), Path('/run/user'))
    if any(not _is_trusted_directory(p, 0, private=False)
           for p in ancestors):
        raise ReplayAdapterError('REPLAY_RUNTIME_UNTRUSTED')
    runtime = Path('/run/user') / str(uid)
    if (not _is_trusted_directory(runtime, uid, private=True)
            or not _is_trusted_directory(directory, uid, private=True)):
        raise ReplayAdapterError('REPLAY_CHANNEL_UNTRUSTED')


def parse_systemd_properties(source: object) -> dict[str, str]:
    """Reject missing/duplicate/unexpected fields; never print values."""
    if type(source) is not str or len(source) > 4096:
        raise ReplayAdapterError('REPLAY_PROPERTIES_UNVERIFIED')
    result: dict[str, str] = {}
    allowed = set(plan.PROPERTY_FIELDS)
    for line in source.splitlines():
        if not line or '=' not in line:
            raise ReplayAdapterError('REPLAY_PROPERTIES_UNVERIFIED')
        name, value = line.split('=', 1)
        if (name not in allowed or name in result
                or len(value) > 256 or '\x00' in value):
            raise ReplayAdapterError('REPLAY_PROPERTIES_UNVERIFIED')
        result[name] = value
    if set(result) != allowed:
        raise ReplayAdapterError('REPLAY_PROPERTIES_UNVERIFIED')
    return result


class ReplaySystemdAdapter:
    """Bindings for run_accounted, with NO default command executor.

    Caller-supplied executor MUST run argv with shell=False and without
    user-controlled environment or inherited credentials. The adapter
    handles one preselected process ID and receipt directory only.
    """

    def __init__(self, *, uid: int, pid: int, receipt_dir: Path,
                 execute_argv: Callable[[tuple[str, ...], int], object]):
        if not callable(execute_argv):
            raise ReplayAdapterError('REPLAY_EXECUTOR_REQUIRED')
        try:
            self.unit = plan.unit_name(pid)
            plan._channel(receipt_dir, uid)
        except plan.ReplaySystemdPlanError:
            raise ReplayAdapterError('REPLAY_ADAPTER_PARAMETERS_UNTRUSTED') from None
        self.uid = uid
        self.pid = pid
        self.receipt_dir = receipt_dir
        self._execute_argv = execute_argv

    def _run(self, args: tuple[str, ...], *, timeout: int) -> str:
        """Treat all executor exceptions, stdout and stderr as untrusted."""
        try:
            result = self._execute_argv(args, timeout)
            if (type(result.returncode) is not int or result.returncode != 0
                    or type(result.stdout) is not str
                    or len(result.stdout) > 4096):
                raise ReplayAdapterError('REPLAY_SYSTEMD_COMMAND_UNVERIFIED')
            return result.stdout
        except ReplayAdapterError:
            raise
        except Exception:
            raise ReplayAdapterError('REPLAY_SYSTEMD_COMMAND_UNVERIFIED') from None

    def start_worker(self) -> None:
        require_private_channel(uid=self.uid, directory=self.receipt_dir)
        args = plan.worker_start_argv(self.unit, uid=self.uid,
                                      receipt_dir=self.receipt_dir)
        self._run(args, timeout=15)

    def properties(self) -> dict[str, str]:
        data = self._run(plan.properties_argv(self.unit), timeout=6)
        return parse_systemd_properties(data)

    def stop_worker(self) -> None:
        # Do not reset-failed here: final counters must remain readable
        # until the separate accounting controller reconciles the ledger.
        self._run(plan.stop_argv(self.unit), timeout=15)

    def preflight(self) -> None:
        """No-Apple sleep preflight; always stop and verify final counters."""
        start_attempted = False
        sampled_ok = False
        try:
            start_attempted = True
            self._run(plan.preflight_start_argv(self.pid), timeout=15)
            sample = parse_systemd_properties(self._run(
                plan.preflight_properties_argv(self.pid), timeout=6))
            ingress, egress, active, substate, _ = accounting._snapshot(sample)
            if ingress != 0 or egress != 0 or active != 'active':
                raise ReplayAdapterError('REPLAY_PREFLIGHT_UNVERIFIED')
            if substate != 'running':
                raise ReplayAdapterError('REPLAY_PREFLIGHT_UNVERIFIED')
            sampled_ok = True
        finally:
            # An ambiguous launch may still have created a systemd unit.
            # Even if stop fails, never continue into a budget reservation.
            if start_attempted:
                try:
                    self._run(plan.preflight_stop_argv(self.pid), timeout=15)
                    final = parse_systemd_properties(self._run(
                        plan.preflight_properties_argv(self.pid), timeout=6))
                    ingress, egress, active, substate, _ = accounting._snapshot(final)
                    if (ingress != 0 or egress != 0
                            or active != 'inactive' or substate != 'dead'):
                        raise ReplayAdapterError('REPLAY_PREFLIGHT_UNVERIFIED')
                except Exception:
                    raise ReplayAdapterError('REPLAY_PREFLIGHT_STOP_UNCONFIRMED') from None
        if not sampled_ok:
            raise ReplayAdapterError('REPLAY_PREFLIGHT_UNVERIFIED')
