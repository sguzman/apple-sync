#!/usr/bin/env python3
"""Use a separate, unprivileged Polkit agent for the one-time installer.

Polkit 127 can reject pkexec's *internal* terminal agent on systems using
the socket-activated helper, even with valid PAM credentials. Register an
independent agent for this exact local process first. Never alter Polkit
services or policies to work around an authentication failure.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


class AuthorizationError(Exception):
    """Sanitized local preflight failure (no password was requested)."""


def process_start_time(path: Path = Path('/proc/self/stat')) -> int:
    """Read field 22 of /proc/self/stat, without splitting the comm field."""
    raw = path.read_text('ascii')
    _, sep, tail = raw.rpartition(') ')
    if not sep:
        raise AuthorizationError('PROCESS_IDENTITY_UNAVAILABLE')
    words = tail.split()
    if len(words) <= 19 or not words[19].isdigit():
        raise AuthorizationError('PROCESS_IDENTITY_UNAVAILABLE')
    return int(words[19])


def authenticate_installer(installer: Path, *, seconds: float = 6) -> int:
    """Make exactly one authorized request, with no internal pkexec agent.

    pkttyagent is a *nonprivileged* subprocess and uses its own tty to
    authenticate. --fallback avoids replacing an existing desktop agent.
    If that agent is already registered, pkexec uses it instead.
    """
    if not os.isatty(0):
        raise AuthorizationError('INTERACTIVE_TERMINAL_REQUIRED')
    if not shutil.which('pkexec') or not shutil.which('pkttyagent'):
        raise AuthorizationError('POLKIT_TOOLS_UNAVAILABLE')
    if not installer.is_file():
        raise AuthorizationError('INSTALLER_SOURCE_MISSING')
    agent = None
    read_fd, write_fd = os.pipe()
    try:
        process_id = str(os.getpid()) + ',' + str(process_start_time())
        try:
            agent = subprocess.Popen(
                ['pkttyagent', '--process', process_id,
                 '--notify-fd', str(write_fd), '--fallback'],
                pass_fds=(write_fd,))
        finally:
            os.close(write_fd)
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            events = selector.select(seconds)
            ready = bool(events) and os.read(read_fd, 1) == b''
        if not ready:
            raise AuthorizationError('POLKIT_AGENT_REGISTRATION_TIMEOUT')
        if agent.poll() is None:
            print('POLKIT: separate unprivileged authentication agent ready',
                  flush=True)
        else:
            print('POLKIT: external terminal agent not registered; '
                  'existing desktop agent may handle the request', flush=True)
        # No internal terminal fallback, avoiding the Polkit 127 regression.
        # One authorization request only; never automatically try again.
        result = subprocess.run(
            ['pkexec', '--disable-internal-agent',
             '/usr/bin/python3', str(installer)],
            check=False)
        if result.returncode in (126, 127):
            print('POLKIT: authorization was not granted. '
                  'The installer did not run. '
                  'Check the authentication agent and system journal.',
                  flush=True)
        return result.returncode
    finally:
        os.close(read_fd)
        if agent is not None and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=3)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait(timeout=3)
