#!/usr/bin/env python3
"""Dormant orchestration core for a future root-metered iCloud replay.

All operations are injected. This module does not launch systemd services,
open Apple connections, install privileges or provide a live CLI. It is NOT
connected to an installed service. Only a separately approved root-owned
launcher may supply actual operations after its own security review.

The shared ledger must already be held under its exclusive lock. A durable
reservation precedes any injected worker launch. Every launch attempt is
followed by unconditional cgroup stop; no receipt is read until the stopped
unit has final verified IP counters and the ledger has committed them.
"""
from __future__ import annotations

from collections.abc import Callable
import re

from drive_meter_budget import RESERVE_BYTES
from metered_drive_inventory_trial import accounted_bytes

MAX_SECONDS = 85
SOFT_STOP_BYTES = 1_500_000
POLL_SECONDS = 0.25
UNIT_PATTERN = re.compile(r'apple-sync-drive-metered-replay-[1-9][0-9]*\.service\Z')


class ReplayAccountingError(Exception):
    """Fixed privacy-safe controller failure; never include callback data."""


def _snapshot(raw: object) -> tuple[int, int, str, str, str]:
    if not isinstance(raw, dict):
        raise ReplayAccountingError('REPLAY_IP_ACCOUNTING_UNVERIFIED')
    try:
        ingress, egress = accounted_bytes(raw)
        active, substate, status = (raw.get('ActiveState'),
                                    raw.get('SubState'),
                                    raw.get('ExecMainStatus'))
        if active not in ('active', 'inactive', 'failed') or substate not in (
                'running', 'exited', 'dead', 'failed'):
            raise ReplayAccountingError('REPLAY_SERVICE_STATE_UNVERIFIED')
        if not isinstance(status, str):
            raise ReplayAccountingError('REPLAY_SERVICE_STATE_UNVERIFIED')
        return ingress, egress, active, substate, status
    except ReplayAccountingError:
        raise
    except Exception:
        raise ReplayAccountingError('REPLAY_IP_ACCOUNTING_UNVERIFIED') from None


def run_accounted(*, ledger: object, unit: str,
                  guard_setup: Callable[[], None],
                  idle_window: Callable[[], bool],
                  preflight: Callable[[], None],
                  start_worker: Callable[[], None],
                  properties: Callable[[], dict],
                  stop_worker: Callable[[], None],
                  monotonic: Callable[[], float],
                  sleep: Callable[[float], None],
                  read_observation: Callable[[], dict],
                  read_failure: Callable[[], str]) -> dict:
    """Record one cgroup's traffic before accepting a replay receipt.

    This is an injected state machine, NOT a safe live entrypoint in itself.
    Callbacks must be supplied by a future separately tested root controller.
    Its output never constitutes metadata equivalence or inventory approval.
    A stop/measurement/ledger failure leaves the durable reservation pending
    rather than assuming that traffic cost zero bytes.
    """
    if not isinstance(unit, str) or not UNIT_PATTERN.fullmatch(unit):
        raise ReplayAccountingError('REPLAY_UNIT_UNTRUSTED')
    # No unit may start before setup, backup idleness and zero-traffic
    # IPAccounting preflight have succeeded. Never swallow their exceptions
    # and continue: they are required, not advisory.
    try:
        ledger.check_ready()
        guard_setup()
        idle = idle_window()
        if type(idle) is not bool:
            raise ReplayAccountingError('REPLAY_IDLE_STATUS_UNKNOWN')
        if not idle:
            return {
                'status': 'REPLAY_DEFERRED_NO_TRAFFIC',
                'production_commit_authorized': False,
                'checkpoint_writes': 0,
            }
        preflight()
        cutoff = min(SOFT_STOP_BYTES, ledger.remaining - RESERVE_BYTES)
    except ReplayAccountingError:
        raise
    except Exception:
        raise ReplayAccountingError('REPLAY_PREFLIGHT_UNVERIFIED') from None
    if type(cutoff) is not int or cutoff < 1:
        raise ReplayAccountingError('REPLAY_BUDGET_EXHAUSTED')

    try:
        ledger.begin(unit)  # persist reservation before any worker launch
    except Exception:
        raise ReplayAccountingError('REPLAY_LEDGER_RESERVATION_FAILED') from None

    highest_ingress = highest_egress = 0
    exit_status = None
    was_interrupted = True
    began = None
    try:
        try:
            # A failed/partially accepted start still requires a stop.
            start_worker()
            began = monotonic()
            while True:
                ingress, egress, active, substate, status = _snapshot(properties())
                highest_ingress = max(highest_ingress, ingress)
                highest_egress = max(highest_egress, egress)
                if ingress + egress >= cutoff:
                    break
                if substate in ('exited', 'dead', 'failed'):
                    # A terminal worker status is meaningful only if it was
                    # actually observed before the controller stopped it.
                    if re.fullmatch(r'[0-9]{1,4}', status):
                        exit_status = int(status)
                        was_interrupted = False
                    break
                if active != 'active':
                    break
                if monotonic() - began >= MAX_SECONDS:
                    break
                sleep(POLL_SECONDS)
        except Exception:
            # No arbitrary callback error is ever exposed. Even on an
            # interrupted sample we can reconcile measured bytes if and
            # only if stop+final counters are independently verified.
            pass
        finally:
            # Stop is mandatory even if the transient service launcher
            # partially accepted a unit before returning an error.
            try:
                stop_worker()
                ingress, egress, active, substate, _status = _snapshot(
                    properties())
                if active != 'inactive' or substate != 'dead':
                    raise ReplayAccountingError('REPLAY_STOP_UNCONFIRMED')
                highest_ingress = max(highest_ingress, ingress)
                highest_egress = max(highest_egress, egress)
            except Exception:
                raise ReplayAccountingError(
                    'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED') from None
        # From this point the cgroup must be inactive/dead. Commit counters
        # while still holding the exclusive shared ledger lock. Never
        # validate a receipt first.
        try:
            ledger.complete(highest_ingress, highest_egress)
        except Exception:
            raise ReplayAccountingError('REPLAY_LEDGER_RECONCILIATION_FAILED') from None
    except ReplayAccountingError:
        # The ledger's pending reservation intentionally remains blocked
        # when stop, final counters or reconciliation cannot be proven.
        raise

    total = highest_ingress + highest_egress
    base = {
        'measured_ip_bytes': total,
        'production_commit_authorized': False,
        'checkpoint_writes': 0,
    }
    if was_interrupted or exit_status is None:
        return {**base, 'status': 'REPLAY_WORKER_INTERRUPTED'}
    if exit_status != 0:
        try:
            code = read_failure()
        except Exception:
            return {**base, 'status': 'REPLAY_WORKER_FAILED_UNVERIFIED'}
        # Only a separately validated fixed code may be reported here.
        from metered_drive_batch_replay import FAILURE_CODES
        if type(code) is not str or code not in FAILURE_CODES:
            return {**base, 'status': 'REPLAY_WORKER_FAILED_UNVERIFIED'}
        return {**base, 'status': 'REPLAY_WORKER_FAILED', 'code': code}
    try:
        # Apply exactly the same full schema the root-side file reader uses
        # even if a future wrapper accidentally wires a weaker callback.
        from metered_drive_batch_replay import validate_observation_data
        receipt = validate_observation_data(read_observation())
    except Exception:
        return {**base, 'status': 'REPLAY_OBSERVATION_UNVERIFIED'}
    return {**base, 'status': 'REPLAY_OBSERVATION',
            'outcome': receipt['outcome']}
