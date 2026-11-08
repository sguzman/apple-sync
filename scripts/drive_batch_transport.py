#!/usr/bin/env python3
"""Bounded Unix-socket transport for an EXPERIMENTAL rclone batch command.

No entry point, no rclone daemon launch, no Apple credentials and no checkpoint
writes. Must be invoked only by a future metered and archive-locked worker.
The caller owns a private socket from a deliberately patched rclone binary.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import stat

from drive_batch_contract import BatchContractError, validate_response

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TIMEOUT = 30


class BatchTransportError(Exception):
    """Fixed, privacy-safe error code; never return raw Apple/RC errors."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float):
        super().__init__('localhost', timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(str(self._path))
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


def _trusted_socket(path: Path) -> bool:
    try:
        parent = path.parent.lstat()
        child = path.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
            and stat.S_IMODE(parent.st_mode) == 0o700
            and stat.S_ISSOCK(child.st_mode) and child.st_uid == os.getuid())


def fetch_batch(socket_path: Path, batch: list[tuple[str, str]], *,
                timeout: float = 15) -> list[tuple[str, list[dict]]]:
    """Fetch one batch from an already-running private rclone RC socket.

    Request IDs stay in the HTTP POST body, never argv, logs or URLs.
    This layer validates the response but cannot establish completeness.
    """
    if (type(timeout) not in (int, float) or timeout <= 0 or
            timeout > MAX_TIMEOUT):
        raise BatchTransportError('INVALID_BATCH_TIMEOUT')
    try:
        from drive_batch_contract import DRIVE_ID, MAX_BATCH_FOLDERS
        if (not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH_FOLDERS
                or len({x[0] for x in batch}) != len(batch)
                or len({x[1] for x in batch}) != len(batch)
                or not all(isinstance(x, tuple) and len(x) == 2
                           and isinstance(x[0], str) and x[0]
                           and isinstance(x[1], str) and DRIVE_ID.fullmatch(x[1])
                           and '#' not in x[1] for x in batch)):
            raise BatchTransportError('INVALID_BATCH_REQUEST')
    except (TypeError, ValueError, IndexError, KeyError, AttributeError):
        raise BatchTransportError('INVALID_BATCH_REQUEST') from None
    path = Path(socket_path)
    if not _trusted_socket(path):
        raise BatchTransportError('UNTRUSTED_BATCH_SOCKET')
    request = json.dumps({'fs': 'icloud:', 'command': 'apple-sync-read-batch',
                          'arg': [drive_id for _, drive_id in batch], 'opt': {}},
                         separators=(',', ':')).encode('utf-8')
    if len(request) > MAX_REQUEST_BYTES:
        raise BatchTransportError('BATCH_REQUEST_TOO_LARGE')
    connection = _UnixConnection(path, timeout)
    try:
        connection.request('POST', '/backend/command', body=request,
                           headers={'Content-Type': 'application/json',
                                    'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise BatchTransportError('BATCH_RC_REFUSED')
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BatchTransportError('BATCH_RESPONSE_TOO_LARGE')
    except (OSError, http.client.HTTPException):
        raise BatchTransportError('BATCH_SOCKET_UNAVAILABLE') from None
    finally:
        connection.close()
    try:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or set(decoded) != {'result'}:
            raise BatchTransportError('INVALID_BATCH_RC_ENVELOPE')
        return validate_response(batch, decoded['result'])
    except (UnicodeError, ValueError, BatchContractError):
        raise BatchTransportError('INVALID_BATCH_RC_RESPONSE') from None
