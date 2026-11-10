#!/usr/bin/env python3
"""Pure launch-identity gate for an UNINSTALLED, dormant replay worker.

This gate does not run or authorize Apple access. In particular, it
CANNOT attest systemd IPAccounting or the cumulative ledger; ONLY the
separate trusted root controller can verify those facts. Do not call
replay_once merely because this pure input check succeeds.

No CLI, subprocess, network, file IO, systemd calls or secrets.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

import drive_batch_replay_systemd_plan as plan

MAX_CGROUP_BYTES = 4096
INVOCATION = re.compile(r'[0-9a-fA-F]{32}\Z')
CGROUP = re.compile(
    r'0::/system\.slice/(apple-sync-drive-metered-replay-[1-9][0-9]*\.service)\Z'
)


class ReplayWorkerGateError(Exception):
    """Constant refusal category only; never echo provided environment."""


@dataclass(frozen=True)
class ReplayLaunchIdentity:
    unit: str
    receipt_dir: Path
    uid: int


def check_launch(*, uid: object, euid: object, environ: Mapping[str, str],
                 cgroup: str) -> ReplayLaunchIdentity:
    """Check that a worker is in the expected exact systemd cgroup.

    This is a necessary (not sufficient) condition: unprivileged environment
    variables are forgeable. The actual execution path must also be gated
    by an independently reviewed root controller, active IPAccounting,
    durable budget reservation, private receipt channel and archive lock.
    """
    if (type(uid) is not int or not 1 <= uid < 2**31
            or type(euid) is not int or euid != uid):
        raise ReplayWorkerGateError('REPLAY_WORKER_USER_UNTRUSTED')
    if not isinstance(environ, Mapping):
        raise ReplayWorkerGateError('REPLAY_WORKER_ENV_UNTRUSTED')
    metered = environ.get('APPLE_SYNC_REPLAY_METERED')
    invocation = environ.get('INVOCATION_ID')
    unit = environ.get('APPLE_SYNC_REPLAY_UNIT')
    receipt = environ.get('APPLE_SYNC_REPLAY_RECEIPT_DIR')
    if (metered != '1' or type(invocation) is not str
            or not INVOCATION.fullmatch(invocation)
            or type(unit) is not str
            or not plan.accounting.UNIT_PATTERN.fullmatch(unit)):
        raise ReplayWorkerGateError('REPLAY_WORKER_ENV_UNTRUSTED')
    if (type(cgroup) is not str
            or not 0 < len(cgroup.encode('utf-8', 'ignore')) <= MAX_CGROUP_BYTES):
        raise ReplayWorkerGateError('REPLAY_WORKER_CGROUP_UNTRUSTED')
    # One unified-cgroup-v2 membership only. Refuse user.slice, scopes,
    # trailing nested services, ambiguous multi-line or legacy mounts.
    match = CGROUP.fullmatch(cgroup.removesuffix('\n'))
    if match is None or match.group(1) != unit:
        raise ReplayWorkerGateError('REPLAY_WORKER_CGROUP_UNTRUSTED')
    try:
        if type(receipt) is not str:
            raise ValueError('missing')
        directory = Path(receipt)
        plan._channel(directory, uid)
    except (ValueError, TypeError, plan.ReplaySystemdPlanError):
        raise ReplayWorkerGateError('REPLAY_WORKER_CHANNEL_UNTRUSTED') from None
    return ReplayLaunchIdentity(unit=unit, receipt_dir=directory, uid=uid)
