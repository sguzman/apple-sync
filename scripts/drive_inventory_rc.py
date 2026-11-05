#!/usr/bin/env python3
"""Optional persistent rclone RC folder reader for Drive metadata inventory.

Keeps one rclone backend session alive for an entire bounded inventory pass,
rather than creating a fresh backend with every individual lsjson command.
All IPC is through a Unix socket inside a private directory. This module
never downloads file bodies, writes to Apple, or changes the archive.

The existing lsjson per-folder reader remains the default until a measured
comparison confirms that this mode helps on the user's rclone installation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time

import diagnose_drive_downloads as diagnosis

SOCKET_NAME = 'rclone.sock'
STARTUP_SECONDS = 12
MAX_FOLDER_OUTPUT_BYTES = 8 * 1024 * 1024


class RcloneRCError(Exception):
    """Sanitized RPC error; never reveal private folder names or tokens."""


class RcloneRCDeferred(Exception):
    """Another backup became active while a listing was in progress."""


def normalize_folder_listing(folder: str, response: object) -> list[dict]:
    """RC uses paths from the fs root; lsjson uses paths relative to folder.

    Require immediate children and the exact requested folder prefix.
    Reject anything outside that folder instead of silently reparenting it.
    """
    if not isinstance(response, dict) or not isinstance(response.get('list'), list):
        raise RcloneRCError('INVALID_RC_FOLDER_RESPONSE')
    output = []
    prefix = folder + '/' if folder else ''
    for item in response['list']:
        if not isinstance(item, dict):
            raise RcloneRCError('INVALID_RC_FOLDER_ENTRY')
        path = item.get('Path')
        if not isinstance(path, str) or not path.startswith(prefix):
            raise RcloneRCError('RC_PATH_OUTSIDE_REQUESTED_FOLDER')
        relpath = path[len(prefix):]
        if not diagnosis.valid_path(relpath) or '/' in relpath:
            raise RcloneRCError('RC_NONIMMEDIATE_OR_UNSAFE_PATH')
        output.append({**item, 'Path': relpath})
    return output


class RcloneRCReader:
    def __init__(self, state_root: Path, *, max_output: int = MAX_FOLDER_OUTPUT_BYTES):
        self.state_root = state_root
        self.max_output = max_output
        self.private_dir: tempfile.TemporaryDirectory | None = None
        self.socket: Path | None = None
        self.daemon: subprocess.Popen | None = None

    def __enter__(self) -> 'RcloneRCReader':
        if self.state_root.is_symlink() or not self.state_root.is_dir():
            raise RcloneRCError('UNSAFE_RC_STATE_DIRECTORY')
        self.private_dir = tempfile.TemporaryDirectory(
            prefix='.apple-drive-rc-', dir=self.state_root)
        try:
            private = Path(self.private_dir.name)
            if stat.S_IMODE(private.stat().st_mode) != 0o700:
                raise RcloneRCError('UNSAFE_RC_SOCKET_DIRECTORY')
            self.socket = private / SOCKET_NAME
            address = 'unix://' + str(self.socket)
            self.daemon = subprocess.Popen(
                ['rclone', 'rcd', '--rc-addr', address,
                 '--log-level', 'ERROR', '--checkers', '1',
                 '--tpslimit', '2'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + STARTUP_SECONDS
            while time.monotonic() < deadline:
                if self.daemon.poll() is not None:
                    raise RcloneRCError('RC_DAEMON_EXITED_BEFORE_READY')
                if self.socket.is_socket():
                    return self
                time.sleep(0.1)
            raise RcloneRCError('RC_SOCKET_STARTUP_TIMEOUT')
        except (OSError, subprocess.SubprocessError):
            self.__exit__(None, None, None)
            raise RcloneRCError('RC_DAEMON_STARTUP_FAILED') from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args) -> None:
        if self.daemon is not None:
            try:
                if self.daemon.poll() is None:
                    self.daemon.terminate()
                    try:
                        self.daemon.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.daemon.kill()
                        self.daemon.wait(timeout=3)
            finally:
                self.daemon = None
        if self.private_dir is not None:
            self.private_dir.cleanup()
            self.private_dir = None
            self.socket = None

    def __call__(self, folder: str, *, timeout: float) -> list[dict]:
        if not (folder == '' or diagnosis.valid_path(folder)):
            raise RcloneRCError('UNSAFE_RC_FOLDER')
        if self.daemon is None or self.daemon.poll() is not None or self.socket is None:
            raise RcloneRCError('RC_DAEMON_NOT_RUNNING')
        if diagnosis.backup_busy():
            raise RcloneRCDeferred()

        payload = json.dumps(
            {'fs': 'icloud:', 'remote': folder, 'opt': {'recurse': False}},
            ensure_ascii=False)
        cmd = ['rclone', 'rc', '--unix-socket', str(self.socket),
               'operations/list', '--json', payload,
               '--log-level', 'ERROR']
        process = None
        received = bytearray()
        deadline = time.monotonic() + timeout
        try:
            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise RcloneRCError('RC_CLIENT_START_FAILED') from exc
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RcloneRCError('RC_FOLDER_TIMEOUT')
                    if self.daemon.poll() is not None:
                        raise RcloneRCError('RC_DAEMON_EXITED')
                    if diagnosis.backup_busy():
                        raise RcloneRCDeferred()
                    if not selector.select(min(5, remaining)):
                        continue
                    block = os.read(process.stdout.fileno(), 65536)
                    if not block:
                        break
                    received.extend(block)
                    if len(received) > self.max_output:
                        raise RcloneRCError('RC_RESPONSE_TOO_LARGE')
            try:
                exit_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise RcloneRCError('RC_FOLDER_TIMEOUT') from exc
            if exit_code:
                raise RcloneRCError('RC_LISTING_FAILED')
            try:
                parsed = json.loads(received)
            except (ValueError, UnicodeError) as exc:
                raise RcloneRCError('INVALID_RC_JSON') from exc
            return normalize_folder_listing(folder, parsed)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
