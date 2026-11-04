#!/usr/bin/env python3
"""Private, immutable attestations for newly verified iCloud Drive publications.

A receipt records SHA-256 equality of two independent whole-file remote reads
and a matching local publication. It is not a provider-signed checksum, nor
evidence the entire source inventory is complete. Never create receipts for
pre-existing archive files without independently verifying them first.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive

RECEIPT_DIR = 'drive-verification-receipts'
METHOD = 'two-whole-sha256'
SOURCE = 'icloud:'
SHA_PATTERN = re.compile(r'^[0-9a-f]{64}$')


class ReceiptError(Exception):
    """Filename-free receipt validation/storage failure."""


def receipt_path(state_root: Path, remote_path: str) -> Path:
    if archive.safe_path(remote_path) is None:
        raise ReceiptError('INVALID_RECEIPT_FILE_PATH')
    key = archive.key_for('drive-verified-receipt', remote_path)
    return state_root / RECEIPT_DIR / key[:2] / (key + '.json')


def _private_directory(path: Path, *, create: bool = False) -> bool:
    """Reject symlinks; private receipt roots may be created for write only."""
    if path.is_symlink():
        raise ReceiptError('UNSAFE_RECEIPT_DIRECTORY')
    if not path.exists():
        if not create:
            return False
        path.mkdir(mode=0o700)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise ReceiptError('UNSAFE_RECEIPT_DIRECTORY')
    return True


def _parent(state_root: Path, remote_path: str, *, create: bool) -> Path | None:
    # The state directory may predate this feature and have more permissive
    # directory mode. Require that it is a real directory; all newly created
    # receipt-specific directories themselves must be private mode 0700.
    if state_root.is_symlink():
        raise ReceiptError('UNSAFE_RECEIPT_DIRECTORY')
    if not state_root.is_dir():
        if create:
            raise ReceiptError('MISSING_STATE_DIRECTORY')
        return None
    receipt = receipt_path(state_root, remote_path)
    root = state_root / RECEIPT_DIR
    if not _private_directory(root, create=create):
        return None
    if not _private_directory(receipt.parent, create=create):
        return None
    return receipt


def _digest_file(path: Path) -> str:
    # O_NOFOLLOW prevents the terminal component being replaced with a link.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ReceiptError('NOT_A_REGULAR_ARCHIVE_FILE')
        sha = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as data:
            for block in iter(lambda: data.read(1024 * 1024), b''):
                sha.update(block)
        return sha.hexdigest()
    finally:
        os.close(fd)


def create_receipt(state_root: Path, source_entry: dict,
                   sha256: str) -> bool:
    """Publish immutable private record; never replace an existing receipt.

    Caller must have performed the two whole remote reads and *new*
    atomic publication, with SHA/size validation of the actual final file.
    Returns True only for a newly published receipt; old records are immutable.
    """
    path = source_entry.get('Path')
    size = source_entry.get('Size')
    if (archive.safe_path(path) is None or type(size) is not int or size < 0
            or not isinstance(sha256, str) or not SHA_PATTERN.fullmatch(sha256)
            or source_entry.get('IsDir') is not False):
        raise ReceiptError('INVALID_VERIFICATION_INPUT')
    receipt = _parent(state_root, path, create=True)
    if receipt is None:
        raise ReceiptError('INVALID_RECEIPT_ROOT')
    record = {'version': 1, 'remote': SOURCE, 'method': METHOD,
              'identity': archive.source_identity(source_entry),
              'size': size, 'sha256': sha256, 'created': time.time()}
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')
    fd, name = tempfile.mkstemp(prefix='.verified-', dir=receipt.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, receipt, follow_symlinks=False)
            # Crash durability: the content was fsynced above, and the
            # directory entry is now fsynced before claiming success.
            dirfd = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except FileExistsError:
            # We do not accept existing records as permission to overwrite.
            # An old/tampered record is reported by the audit.
            raise ReceiptError('RECEIPT_ALREADY_EXISTS')
        return True
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_receipt(state_root: Path, source_entry: dict,
                  destination_file: Path) -> str:
    """Return missing|verified|invalid|hash_mismatch; never access Apple."""
    path = source_entry.get('Path')
    if archive.safe_path(path) is None:
        return 'invalid'
    try:
        receipt = _parent(state_root, path, create=False)
        if receipt is None:
            return 'missing'
        if receipt.is_symlink():
            return 'invalid'
        if not receipt.exists():
            return 'missing'
        fd = os.open(receipt, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_size > 256 * 1024):
                return 'invalid'
            encoded = stream.read(256 * 1024 + 1)
        if len(encoded) > 256 * 1024:
            return 'invalid'
        record = json.loads(encoded)
        if (not isinstance(record, dict)
                or record.get('version') != 1
                or record.get('remote') != SOURCE
                or record.get('method') != METHOD
                or record.get('identity') != archive.source_identity(source_entry)
                or type(record.get('size')) is not int
                or record['size'] != source_entry.get('Size')
                or not isinstance(record.get('sha256'), str)
                or not SHA_PATTERN.fullmatch(record['sha256'])):
            return 'invalid'
        if destination_file.stat(follow_symlinks=False).st_size != record['size']:
            return 'hash_mismatch'
        if _digest_file(destination_file) != record['sha256']:
            return 'hash_mismatch'
        return 'verified'
    except (ReceiptError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return 'invalid'
