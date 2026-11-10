#!/usr/bin/env python3
"""Dormant root replay lifecycle: channel lease + held-ledger integration.

No CLI, subprocess, service, Apple request or default executor is provided.
The *future* root-only manual controller must provide reviewed dependencies.
This wrapper owns a private receipt lease across the accounting run and
deletes it ONLY for a verifiably settled or never-launched outcome.

Crucial: on ANY raised exception, unexpected status, failed reconciliation
or uncertain stop, retain the channel and release only its descriptors.
The shared cumulative ledger's pending state is owned separately by the
accounting controller and must never be reset by this lifecycle.
"""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path

import drive_batch_replay_channel as channels
import drive_batch_replay_root_wiring as wiring
import metered_drive_batch_replay as receipts

# Only these states are returned after a fully stopped, IP-accounted worker
# and durable budget settlement by the tested run_accounted state machine.
SETTLED_STATES = frozenset((
    'REPLAY_OBSERVATION',
    'REPLAY_OBSERVATION_UNVERIFIED',
    'REPLAY_WORKER_FAILED',
    'REPLAY_WORKER_FAILED_UNVERIFIED',
    'REPLAY_WORKER_INTERRUPTED',
))
DEFERRED_STATE = 'REPLAY_DEFERRED_NO_TRAFFIC'


class ReplayLifecycleError(Exception):
    """Fixed, privacy-safe lifecycle status with no user/cloud data."""


def _cleanup_eligible(result: object) -> bool:
    """Require a complete, explicitly nonauthorizing settled result."""
    if type(result) is not dict:
        return False
    if (result.get('production_commit_authorized') is not False
            or type(result.get('checkpoint_writes')) is not int
            or result['checkpoint_writes'] != 0):
        return False
    status = result.get('status')
    if type(status) is not str:
        return False
    if status == DEFERRED_STATE:
        # The state machine returns this *before* ledger reservation and
        # any worker-launch callback. There is nothing to stop.
        return set(result) == {
            'status', 'production_commit_authorized', 'checkpoint_writes'}
    if status not in SETTLED_STATES:
        return False
    if (type(result.get('measured_ip_bytes')) is not int
            or not 0 <= result['measured_ip_bytes'] <= 2**64 - 1):
        return False
    fields = {'status', 'production_commit_authorized',
              'checkpoint_writes', 'measured_ip_bytes'}
    if status == 'REPLAY_OBSERVATION':
        fields |= {'outcome'}
        if (type(result.get('outcome')) is not str or
                result['outcome'] not in
                receipts.OBSERVATION_OUTCOMES):
            return False
    if status == 'REPLAY_WORKER_FAILED':
        fields |= {'code'}
        if (type(result.get('code')) is not str
                or result['code'] not in receipts.FAILURE_CODES):
            return False
    return set(result) == fields


def run_lifecycle(*, pid: int,
                  allocate_channel: Callable[[], channels.ReplayChannelLease],
                  ledger_factory: Callable[[], object],
                  execute_argv: Callable[[tuple[str, ...], int], object],
                  guard_setup: Callable[[], None],
                  idle_window: Callable[[], bool],
                  monotonic: Callable[[], float],
                  sleep: Callable[[float], None]) -> dict:
    """Compose the existing root-only channel lease and metered root wiring.

    This is injectable for *offline tests only*. In a future installed
    controller, supply exactly allocate_root_channel and reviewed immutable
    system bindings; never accept injected functions from unprivileged users.
    """
    if os.geteuid() != 0:
        raise ReplayLifecycleError('REPLAY_ROOT_SERVICE_REQUIRED')
    dependencies = (
        allocate_channel, ledger_factory, execute_argv,
        guard_setup, idle_window, monotonic, sleep)
    if any(not callable(callback) for callback in dependencies):
        raise ReplayLifecycleError('REPLAY_TRUSTED_DEPENDENCY_MISSING')

    lease = None
    try:
        lease = allocate_channel()
        if (not isinstance(lease, channels.ReplayChannelLease)
                or lease.closed
                or type(lease.uid) is not int
                or lease.uid <= 0
                or not isinstance(lease.path, Path)
                or not lease._same_entry()):
            raise ReplayLifecycleError('REPLAY_CHANNEL_LEASE_UNTRUSTED')
        result = wiring.run_wired(
            uid=lease.uid, pid=pid, receipt_dir=lease.path,
            ledger_factory=ledger_factory,
            execute_argv=execute_argv,
            guard_setup=guard_setup,
            idle_window=idle_window,
            monotonic=monotonic,
            sleep=sleep)
        if not _cleanup_eligible(result):
            raise ReplayLifecycleError('REPLAY_SETTLEMENT_UNVERIFIED')
        try:
            # A settled status implies run_accounted already confirmed stop,
            # counters and ledger commit. A deferred status never launched.
            lease.cleanup_after_stop(stopped_verified=True)
        except channels.ReplayChannelError:
            raise ReplayLifecycleError('REPLAY_CHANNEL_CLEANUP_UNVERIFIED') from None
        return result
    except ReplayLifecycleError:
        raise
    except Exception:
        raise ReplayLifecycleError('REPLAY_LIFECYCLE_UNVERIFIED') from None
    finally:
        # Descriptor close does NOT delete the channel on uncertain outcome.
        # Never clear a pending metering ledger or assume worker stopped here.
        if isinstance(lease, channels.ReplayChannelLease):
            try:
                lease.close()
            except OSError:
                # Preserve the original failure without risking unsafe
                # deletion. The root service's process exit closes fds.
                pass
