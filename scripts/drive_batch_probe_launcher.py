#!/usr/bin/env python3
"""Opt-in launcher for a single, root-metered read-only Drive batch comparison.

Preview by default. No timers, no backup-service changes, no Apple requests
unless explicitly invoked with "run" after the static service is installed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import drive_metered as metered

UNIT = 'apple-sync-drive-batch-probe.service'


class ProbeLaunchError(Exception):
    """Privacy-safe local probe launch refusal."""


def ready() -> None:
    state = metered.service_state(UNIT)
    if state.get('LoadState') != 'loaded':
        raise ProbeLaunchError('PROBE_SERVICE_NOT_INSTALLED')
    if state.get('ActiveState') not in ('inactive', 'failed'):
        raise ProbeLaunchError('PROBE_SERVICE_ALREADY_RUNNING_OR_UNCERTAIN')
    for other in (metered.UNIT, metered.SINGLE_UNIT):
        current = metered.service_state(other)
        if current.get('ActiveState') not in ('inactive', 'failed'):
            raise ProbeLaunchError('ANOTHER_METERED_SERVICE_ACTIVE')
    budget = metered.public_budget()
    if not budget or budget['pending'] or budget['remaining_bytes'] <= 2_000_000:
        raise ProbeLaunchError('PROBE_BUDGET_NOT_READY')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('preview', 'status', 'run'),
                        default='preview')
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'preview':
            print('PROBE PREVIEW: at most two pending folders; one '
                  'read-only batch request plus independent listings; '
                  'shared cumulative traffic ledger; no checkpoint writes')
            print('Requires installed static probe unit and explicit run; '
                  'nothing started')
            return 0
        if args.action == 'status':
            state = metered.service_state(UNIT)
            print('PROBE SERVICE: ' + state.get('ActiveState', 'unknown') +
                  ' (loaded=' + state.get('LoadState', 'unknown') +
                  ', result=' + state.get('Result', 'unknown') + ')')
            metered.show_state()
            return 0
        ready()
        result = subprocess.run(
            ['systemctl', '--system', '--no-block', 'start', UNIT],
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise ProbeLaunchError('PROBE_START_NOT_AUTHORIZED_OR_REFUSED')
        print('PROBE REQUEST ACCEPTED: single read-only comparison; no timer enabled',
              flush=True)
        if not args.detach:
            metered.monitor(UNIT)
        return 0
    except (ProbeLaunchError, metered.LauncherError, OSError,
            subprocess.SubprocessError) as exc:
        print('PROBE LAUNCHER: ' +
              (str(exc) if isinstance(exc, (ProbeLaunchError,
                                            metered.LauncherError))
               else 'LOCAL_SERVICE_ERROR'), file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
