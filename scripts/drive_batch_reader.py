#!/usr/bin/env python3
"""Callback-injected buffered batch reader for offline resumable-walk tests.

This module has no CLI, no Apple network client, no service installation,
and no automatic checkpoint writes. Production still uses the original
one-folder reader. The explicit field-parity gate is deliberately false
until independently tested against real rclone records including timestamps.
A failed batch may be resumed by the unmodified original reader.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

import diagnose_drive_downloads as diagnosis
import drive_inventory as inventory
from drive_batch_contract import BatchContractError, plan_batches
from drive_batch_checkpoint import (BatchCheckpointError, MAX_COMMIT_FOLDERS,
                                    convert_batch)


class BatchReaderError(Exception):
    """Sanitized, path-free rejection of unsafe batch reader execution."""


class BufferedBatchReader:
    def __init__(self, *,
                 load_checkpoint: Callable[[], dict | None],
                 read_native: Callable[..., object],
                 backup_busy: Callable[[], bool] = diagnosis.backup_busy,
                 size: int = MAX_COMMIT_FOLDERS,
                 verified_field_parity: bool = False):
        if type(size) is not int or not 1 <= size <= MAX_COMMIT_FOLDERS:
            raise BatchReaderError('INVALID_BUFFERED_BATCH_SIZE')
        self.load_checkpoint = load_checkpoint
        self.read_native = read_native
        self.backup_busy = backup_busy
        self.size = size
        self.verified_field_parity = verified_field_parity is True
        self.buffer: list[tuple[str, list[dict]]] = []

    def _require_idle(self) -> None:
        try:
            state = self.backup_busy()
        except Exception:
            raise inventory.InventoryDeferred() from None
        if type(state) is not bool or state:
            raise inventory.InventoryDeferred()

    def __call__(self, folder: str, *, timeout: float) -> list[dict]:
        """Satisfy the existing walk_resumable reader interface.

        The persisted checkpoint is re-read before returning each buffered
        folder. This preserves the original per-folder commit ordering and
        verifies no other process changed its pending head. An archive lock
        must be held by the calling metered controller in any future live use.
        """
        if not self.verified_field_parity:
            raise BatchReaderError('BATCH_FIELD_PARITY_NOT_AUTHORIZED')
        if not inventory.safe_folder(folder) or not folder:
            raise BatchReaderError('INVALID_BATCH_READER_FOLDER')
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise BatchReaderError('INVALID_BATCH_READER_TIMEOUT')
        self._require_idle()
        try:
            saved = self.load_checkpoint()
            if (not isinstance(saved, dict) or
                    not isinstance(saved.get('pending'), list) or
                    not saved['pending'] or saved['pending'][0] != folder):
                raise BatchReaderError('BATCH_CHECKPOINT_HEAD_CHANGED')
            if not self.buffer:
                requests = plan_batches(deepcopy(saved), size=self.size)
                if not requests or requests[0][0][0] != folder:
                    raise BatchReaderError('BATCH_CHECKPOINT_PLAN_CHANGED')
                try:
                    # The callback is not allowed to mutate the trusted
                    # request used for validating its response.
                    native = self.read_native(deepcopy(requests[0]),
                                              timeout=min(timeout, 30))
                except inventory.InventoryDeferred:
                    raise
                except Exception:
                    raise BatchReaderError('BATCH_NATIVE_REQUEST_FAILED') from None
                converted = convert_batch(requests[0], native)
                if [path for path, _ in converted] != [
                        path for path, _ in requests[0]]:
                    raise BatchReaderError('BATCH_RESPONSE_ORDER_MISMATCH')
                self.buffer = converted
            if self.buffer[0][0] != folder:
                raise BatchReaderError('BATCH_BUFFER_ORDER_MISMATCH')
            self._require_idle()
            children = deepcopy(self.buffer[0][1])
            self.buffer.pop(0)
            return children
        except (BatchContractError, BatchCheckpointError,
                inventory.InventoryError, OSError, ValueError, TypeError):
            self.buffer.clear()
            raise BatchReaderError('BATCH_READ_NOT_VERIFIED') from None
        except BatchReaderError:
            self.buffer.clear()
            raise
