#!/usr/bin/env python3
"""Uninstalled wiring of the replay metering ledger, systemd adapter and receipts.

THIS MODULE HAS NO EXECUTABLE ENTRYPOINT AND NO DEFAULT COMMAND EXECUTOR.
It cannot start systemd/Apple from the CLI. Its only entry function requires
injected dependencies: a trusted ledger factory, a reviewed shell-free command
executor, the existing root/timer and backup-idle guards, and a clock.
Every executor call is checked against the six pinned systemd argv/timeout
pairs for this uid/pid/receipt directory before reaching injected code.

In production, no user-controlled callback may be accepted. A separate,
root-owned, manual-only installation and another offline review are needed
before a real executor/worker could be wired. The replay worker is still
dormant. An observational result never authorizes checkpoint writes.
"""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path

import drive_batch_replay_accounting as accounting
import drive_batch_replay_command_policy as commands
import drive_batch_replay_root_executor as executor
import drive_batch_replay_systemd_adapter as systemd
import metered_drive_batch_replay as receipts


class ReplayWiringError(Exception):
    """Fixed-safe failure code, without executor or cloud details."""


def run_wired(*, uid: int, pid: int, receipt_dir: Path,
              ledger_factory: Callable[[], object],
              execute_argv: Callable[[tuple[str, ...], int], object],
              guard_setup: Callable[[], None],
              idle_window: Callable[[], bool],
              monotonic: Callable[[], float],
              sleep: Callable[[float], None]) -> dict:
    """One mocked/reviewed replay run, holding the SAME ledger lock throughout.

    The root wrapper must supply trusted, non-user-controlled callbacks.
    Receipt readers are NOT injected: always bind the strict version-1
    root observation and failure validators on this exact private channel.
    Failures before reservation are sanitized here. The accounting core
    leaves the ledger pending on stop/measurement/commit uncertainty.
    """
    for component in (ledger_factory, execute_argv, guard_setup, idle_window,
                      monotonic, sleep):
        if not callable(component):
            raise ReplayWiringError('REPLAY_TRUSTED_DEPENDENCY_MISSING')
    try:
        # Fix the exact six systemd calls and their timeout budgets before
        # entering the shared ledger lock or attempting any worker launch.
        # This adds a second enforcement layer *inside* the root wiring:
        # future callers cannot accidentally bypass the pure command policy
        # by supplying a permissive executor. The executor remains injected;
        # there is still NO live subprocess or default execution path.
        policy = commands.ReplayCommandPolicy(
            uid=uid, pid=pid, receipt_dir=receipt_dir)

        def policy_checked_executor(argv: tuple[str, ...],
                                    timeout: int) -> object:
            policy.validate(argv, timeout)
            return execute_argv(argv, timeout)

        io = systemd.ReplaySystemdAdapter(
            uid=uid, pid=pid, receipt_dir=receipt_dir,
            execute_argv=policy_checked_executor)
    except (systemd.ReplayAdapterError, commands.ReplayCommandPolicyError):
        raise ReplayWiringError('REPLAY_ADAPTER_UNTRUSTED') from None

    try:
        # Unlike an injected unheld ledger object, the context manager
        # holds the shared root budget flock until all receipts are read.
        with ledger_factory() as ledger:
            result = accounting.run_accounted(
                ledger=ledger,
                unit=io.unit,
                guard_setup=guard_setup,
                idle_window=idle_window,
                preflight=io.preflight,
                start_worker=io.start_worker,
                properties=io.properties,
                stop_worker=io.stop_worker,
                monotonic=monotonic,
                sleep=sleep,
                read_observation=lambda: receipts.read_observation(
                    io.receipt_dir),
                read_failure=lambda: receipts.read_failure(io.receipt_dir),
            )
    except accounting.ReplayAccountingError as exc:
        # These are already fixed category strings from the state machine.
        # Preserve its shutdown/ledger failure reason without a traceback.
        raise ReplayWiringError(str(exc)) from None
    except Exception:
        raise ReplayWiringError('REPLAY_WIRING_UNVERIFIED') from None

    if (type(result) is not dict
            or result.get('production_commit_authorized') is not False
            or result.get('checkpoint_writes') != 0):
        raise ReplayWiringError('REPLAY_RESULT_NOT_READ_ONLY')
    return result

def run_wired_with_transport(*, uid: int, pid: int,
                             receipt_dir: Path,
                             ledger_factory: Callable[[], object],
                             invoke: Callable[[tuple[str, ...], int, dict[str, str]], object],
                             guard_setup: Callable[[], None],
                             idle_window: Callable[[], bool],
                             monotonic: Callable[[], float],
                             sleep: Callable[[float], None]) -> dict:
    """OFFLINE-INJECTED facade + root accounting integration.

    No default runner: the caller must explicitly supply `invoke`. This
    helper must NOT be exposed from a root CLI, service or public API until a
    separately reviewed bounded subprocess implementation and installation
    are approved. Testing uses a fake invocation callback only.

    Verify real EUID before allocating or opening the ledger. Exact argv,
    timeouts, fixed env and absolute binary paths are enforced by the
    transport facade; root wiring independently checks the six-command
    allowlist a second time.
    """
    if os.geteuid() != 0:
        raise ReplayWiringError('REPLAY_ROOT_SERVICE_REQUIRED')
    try:
        boundary = executor.ReplayExecutorBoundary(
            uid=uid, pid=pid, receipt_dir=receipt_dir, invoke=invoke)
    except executor.ReplayExecutorError:
        raise ReplayWiringError('REPLAY_EXECUTOR_BOUNDARY_UNTRUSTED') from None
    return run_wired(
        uid=uid, pid=pid, receipt_dir=receipt_dir,
        ledger_factory=ledger_factory, execute_argv=boundary,
        guard_setup=guard_setup, idle_window=idle_window,
        monotonic=monotonic, sleep=sleep)
