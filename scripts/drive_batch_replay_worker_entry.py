#!/usr/bin/env python3
"""Guarded replay worker body, deliberately NOT connected to a CLI.

This is only the worker-side body for a future *separately authorized*
root-accounted transient system service. The existing worker command still
prints REPLAY_WORKER_DORMANT for every argument, including --execute.
No installed service, timer, launcher, or existing command points here.

Before ANY Apple transport, validate this process's own cgroup/UID and
private receipt channel. All emitted receipts are observational only; the
separate root controller must stop the cgroup and commit the cumulative
measured IP bytes BEFORE reading one. These checks are not a hard quota.
"""
from __future__ import annotations

import sys

import drive_batch_replay_worker as worker
import drive_batch_replay_worker_preflight as preflight

# A future executable entrypoint may call this, after a separate manual
# installation/security review. Never add a CLI here before that approval.


def guarded_once() -> int:
    """Validate identity; attempt exactly one read-only bracketed replay.

    Fixed stdout/stderr categories only: no cloud metadata, paths, IDs,
    timestamps, payloads or raw exceptions. On a transport or validation
    failure, publish only a fixed failure category; never fall back to a
    success receipt. Exit 0 means a receipt was *written*, NOT that the
    metadata is equivalent or that IP traffic is already reconciled.
    """
    try:
        identity = preflight.validate_own_process()
    except Exception:
        # A missing/invalid channel means we cannot safely publish a
        # failure receipt either. Root will observe nonzero exit and still
        # reconcile any actual traffic before accepting a result.
        print('REPLAY_WORKER_REFUSED: PREFLIGHT_UNVERIFIED',
              file=sys.stderr, flush=True)
        return 2

    try:
        result = worker.replay_once()
        worker.publish_observation(result, directory=identity.receipt_dir)
    except Exception as exc:
        category = (
            str(exc) if type(exc) is worker.ReplayWorkerError
            and str(exc) in worker.REPLAY_FAILURES
            else 'REPLAY_LOCAL_FAILURE'
        )
        try:
            worker.publish_failure(category, directory=identity.receipt_dir)
        except Exception:
            # A partial/untrusted channel must never silently pass as a
            # success or overwrite a previous receipt. The root-side
            # reader will fail closed if the failure receipt is absent.
            pass
        print('REPLAY_WORKER_FAILED: ' + category,
              file=sys.stderr, flush=True)
        return 2

    # Outcome text is drawn only from the fixed worker allowlist; avoid
    # rendering an arbitrary result dictionary to systemd's journal.
    outcome = result.get('outcome') if type(result) is dict else None
    if type(outcome) is not str or outcome not in worker.REPLAY_OUTCOMES:
        print('REPLAY_WORKER_FAILED: REPLAY_LOCAL_FAILURE',
              file=sys.stderr, flush=True)
        return 2
    print('REPLAY_WORKER_OBSERVATION: ' + outcome +
          '; checkpoint_writes=0; root_accounting_pending_verification',
          flush=True)
    return 0
