#!/usr/bin/env python3
"""Pure allowlist for a FUTURE, root-owned, manual replay command executor.

DORMANT: no subprocess, no CLI, no systemd calls, no ledger or Apple access.
This policy is a necessary input validator, NOT permission to execute.
Only the separately authorized root controller may eventually own an
executor, after its code and worker trees have been verified as root-owned.

Exact argv AND exact timeout must equal one of the existing six pure
systemd planner operations for a single pinned uid/pid/receipt channel.
There is no "shell", "enable", "daemon-reload", custom executable, arbitrary
unit, second worker, command mutation or blanket systemctl capability.
"""
from __future__ import annotations

from pathlib import Path

import drive_batch_replay_systemd_plan as plan


class ReplayCommandPolicyError(Exception):
    """Constant refusal only; never echo command arguments or private data."""


class ReplayCommandPolicy:
    """No-side-effect command allowlist for one proposed metered replay run."""

    __slots__ = ('uid', 'pid', 'receipt_dir', '_allowed')

    def __init__(self, *, uid: int, pid: int, receipt_dir: Path):
        try:
            unit = plan.unit_name(pid)
            plan._channel(receipt_dir, uid)
            approved = (
                (plan.preflight_start_argv(pid), 15),
                (plan.preflight_properties_argv(pid), 6),
                (plan.preflight_stop_argv(pid), 15),
                (plan.worker_start_argv(unit, uid=uid,
                                        receipt_dir=receipt_dir), 15),
                (plan.properties_argv(unit), 6),
                (plan.stop_argv(unit), 15),
            )
        except (plan.ReplaySystemdPlanError, ValueError, TypeError):
            raise ReplayCommandPolicyError(
                'REPLAY_COMMAND_CONTEXT_UNTRUSTED') from None
        if len(set(approved)) != 6:
            raise ReplayCommandPolicyError(
                'REPLAY_COMMAND_PLAN_UNTRUSTED')
        self.uid = uid
        self.pid = pid
        self.receipt_dir = receipt_dir
        self._allowed = frozenset(approved)

    def validate(self, argv: object, timeout: object) -> None:
        """Accept only exactly planned tuple and timeout, without running it.

        Do not turn the result into a privilege token. A real root executor
        must also verify live prerequisites and use a fixed environment,
        shell=False, explicit binary paths and bounded output.
        """
        if (type(argv) is not tuple
                or not all(type(part) is str for part in argv)
                or type(timeout) is not int
                or (argv, timeout) not in self._allowed):
            raise ReplayCommandPolicyError(
                'REPLAY_COMMAND_NOT_ALLOWED')

    def planned_commands(self) -> tuple[tuple[tuple[str, ...], int], ...]:
        """Deterministic read-only review snapshot; never executes commands."""
        unit = plan.unit_name(self.pid)
        return (
            (plan.preflight_start_argv(self.pid), 15),
            (plan.preflight_properties_argv(self.pid), 6),
            (plan.preflight_stop_argv(self.pid), 15),
            (plan.worker_start_argv(unit, uid=self.uid,
                                    receipt_dir=self.receipt_dir), 15),
            (plan.properties_argv(unit), 6),
            (plan.stop_argv(unit), 15),
        )
