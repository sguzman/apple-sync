#!/usr/bin/env python3
"""Dormant transport boundary for a FUTURE manually authorized root replay.

No subprocess runner, CLI, service installer, Apple access or ledger hook.
This file MUST NOT be called with a live callable until separate review
and explicit authorization. It is testable via an injected fake only.

The fixed command policy is checked first; executable paths and the
environment are pinned before the injected callback. Returned command
status and output are validated and sanitized; raw stderr is NEVER
forwarded to the accounting adapter or logs.

IMPORTANT: this is NOT a live controller. The eventual runner must
enforce shell=False, close_fds=True, env=the supplied fixed mapping,
text=True, independent bounded capture, and the exact timeout. This
module cannot attest such behavior from an arbitrary injected callback.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable, Mapping

import drive_batch_replay_command_policy as policy
import drive_batch_replay_systemd_adapter as adapter

MAX_OUTPUT_CHARS = 4096
COMMAND_BINARIES = {
    'systemd-run': '/usr/bin/systemd-run',
    'systemctl': '/usr/bin/systemctl',
}
# Created afresh for each call. No HOME, PATH, DBUS, Python or cloud tokens
# from the ambient process environment are inherited.
FIXED_ENV = {
    'PATH': '/usr/bin',
    'LANG': 'C',
    'LC_ALL': 'C',
    'HOME': '/root',
    'PYTHONNOUSERSITE': '1',
}


class ReplayExecutorError(Exception):
    """Safe refusal category; no command details, credentials or stdout."""


@dataclass(frozen=True)
class ReplayCommandResult:
    """Minimal adapter-compatible return object, never raw stderr."""

    returncode: int
    stdout: str


class ReplayExecutorBoundary:
    """Root-only transport facade with no built-in command runner."""

    __slots__ = ('_policy', '_invoke')

    def __init__(
        self, *, uid: int, pid: int, receipt_dir: Path,
        invoke: Callable[[tuple[str, ...], int, Mapping[str, str]], object],
    ) -> None:
        if not callable(invoke):
            raise ReplayExecutorError('REPLAY_EXECUTOR_INVOKER_REQUIRED')
        try:
            self._policy = policy.ReplayCommandPolicy(
                uid=uid, pid=pid, receipt_dir=receipt_dir)
        except policy.ReplayCommandPolicyError:
            raise ReplayExecutorError(
                'REPLAY_EXECUTOR_CONTEXT_UNTRUSTED') from None
        self._invoke = invoke

    def __call__(self, argv: tuple[str, ...], timeout: int
                 ) -> ReplayCommandResult:
        if os.geteuid() != 0:
            raise ReplayExecutorError('REPLAY_EXECUTOR_ROOT_REQUIRED')
        try:
            self._policy.validate(argv, timeout)
        except policy.ReplayCommandPolicyError:
            raise ReplayExecutorError(
                'REPLAY_EXECUTOR_COMMAND_NOT_ALLOWED') from None

        command = (COMMAND_BINARIES[argv[0]], *argv[1:])
        env = dict(FIXED_ENV)
        try:
            response = self._invoke(command, timeout, env)
        except Exception:
            raise ReplayExecutorError(
                'REPLAY_EXECUTOR_COMMAND_UNVERIFIED') from None

        try:
            code, stdout, stderr = (
                response.returncode, response.stdout, response.stderr)
            if (type(code) is not int or code != 0
                    or type(stdout) is not str
                    or type(stderr) is not str
                    or len(stdout) > MAX_OUTPUT_CHARS
                    or len(stderr) > MAX_OUTPUT_CHARS
                    or '\x00' in stdout or '\x00' in stderr):
                raise ValueError('unverified result')
            if argv[:3] == ('systemctl', '--system', 'show'):
                # Reuse the strict exact-seven-field parser. The adapter
                # checks these fields again and verifies stop/accounting.
                adapter.parse_systemd_properties(stdout)
                sanitized = stdout
            else:
                # systemd-run/stop output is never relevant to the
                # accounting state machine and must not be propagated.
                sanitized = ''
        except Exception:
            raise ReplayExecutorError(
                'REPLAY_EXECUTOR_COMMAND_UNVERIFIED') from None

        return ReplayCommandResult(returncode=0, stdout=sanitized)
