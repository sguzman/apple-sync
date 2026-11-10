#!/usr/bin/env python3
"""Actual-process preflight for a future manually authorized replay worker.

DORMANT: no CLI and no call to replay_once, rclone, systemd or the ledger.
Checks /proc/self/cgroup (not a caller-supplied cgroup string) and the
real/effective user before trusting the exact replay-unit identity.
Validates the volatile receipt-channel mode/owners before any future read.

Passing this preflight DOES NOT prove IPAccounting, durable 100 MB ledger
reservation, or permission to make Apple requests. The trusted root-only
controller must still establish those facts independently.
"""
from __future__ import annotations

import os

import drive_batch_replay_systemd_adapter as systemd
import drive_batch_replay_worker_gate as gate

CGROUP_SOURCE = '/proc/self/cgroup'
CGROUP_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


class ReplayWorkerPreflightError(Exception):
    """Only fixed refusal codes; no sensitive cgroup or path in exceptions."""


def _own_cgroup() -> str:
    """Read bounded actual process membership, never environment-supplied."""
    try:
        fd = os.open(CGROUP_SOURCE, CGROUP_OPEN_FLAGS)
        try:
            raw = os.read(fd, gate.MAX_CGROUP_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        raise ReplayWorkerPreflightError(
            'REPLAY_WORKER_CGROUP_UNAVAILABLE') from None
    if not 0 < len(raw) <= gate.MAX_CGROUP_BYTES:
        raise ReplayWorkerPreflightError(
            'REPLAY_WORKER_CGROUP_UNTRUSTED')
    try:
        return raw.decode('utf-8', errors='strict')
    except UnicodeError:
        raise ReplayWorkerPreflightError(
            'REPLAY_WORKER_CGROUP_UNTRUSTED') from None


def validate_own_process() -> gate.ReplayLaunchIdentity:
    """Necessary preflight only; no live worker or network call follows."""
    uid = os.getuid()
    euid = os.geteuid()
    if (type(uid) is not int or not 1 <= uid < 2**31
            or type(euid) is not int or uid != euid):
        raise ReplayWorkerPreflightError(
            'REPLAY_WORKER_USER_UNTRUSTED')
    actual_cgroup = _own_cgroup()
    try:
        identity = gate.check_launch(
            uid=uid, euid=euid, environ=os.environ, cgroup=actual_cgroup)
    except gate.ReplayWorkerGateError as exc:
        # The gate emits only fixed, privacy-safe refusal categories.
        raise ReplayWorkerPreflightError(str(exc)) from None
    try:
        systemd.require_private_channel(
            uid=identity.uid, directory=identity.receipt_dir)
    except (systemd.ReplayAdapterError, OSError):
        raise ReplayWorkerPreflightError(
            'REPLAY_WORKER_CHANNEL_UNTRUSTED') from None
    return identity
