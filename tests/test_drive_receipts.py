import hashlib
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'drive_receipts.py'
spec = importlib.util.spec_from_file_location('drive_receipts_unit', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def entry(path='private/important.bin', contents=b'abc'):
    return {'Path': path, 'Size': len(contents),
            'IsDir': False, 'ID': 'source-id', 'ModTime': 'stable'}


class DriveReceiptTests(unittest.TestCase):
    def test_new_receipt_private_immutable_and_sha_matches_local_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, dest = root / 'state', root / 'archive'
            state.mkdir(mode=0o700)
            (dest / 'private').mkdir(parents=True)
            data = b'checked from two whole remote reads'
            original = entry(contents=data)
            target = dest / original['Path']
            target.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.assertTrue(mod.create_receipt(state, original, digest))
            path = mod.receipt_path(state, original['Path'])
            self.assertTrue(path.is_file())
            self.assertFalse(path.stat().st_mode & 0o077)
            self.assertFalse(path.parent.stat().st_mode & 0o077)
            self.assertEqual(mod.check_receipt(state, original, target), 'verified')
            original_bytes = path.read_bytes()
            with self.assertRaisesRegex(mod.ReceiptError,
                                        'RECEIPT_ALREADY_EXISTS'):
                mod.create_receipt(state, original, digest)
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertNotIn(b'checked from two', original_bytes)

    def test_existing_unreceipted_file_is_not_mistaken_for_verified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, dest = root / 'state', root / 'archive'
            state.mkdir(mode=0o700)
            dest.mkdir()
            data = b'legacy'
            target = dest / 'old.bin'
            target.write_bytes(data)
            self.assertEqual(mod.check_receipt(
                state, entry('old.bin', data), target), 'missing')
            self.assertFalse((state / mod.RECEIPT_DIR).exists())

    def test_same_length_tamper_fails_hash_verification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, dest = root / 'state', root / 'archive'
            state.mkdir(mode=0o700)
            dest.mkdir()
            data = b'AAAA'
            original = entry('same.bin', data)
            target = dest / 'same.bin'
            target.write_bytes(data)
            mod.create_receipt(state, original,
                               hashlib.sha256(data).hexdigest())
            target.write_bytes(b'BBBB')
            self.assertEqual(mod.check_receipt(state, original, target),
                             'hash_mismatch')

    def test_changed_source_identity_invalidates_receipt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, dest = root / 'state', root / 'archive'
            state.mkdir(mode=0o700)
            dest.mkdir()
            data = b'content'
            original = entry('same.bin', data)
            target = dest / 'same.bin'
            target.write_bytes(data)
            mod.create_receipt(state, original,
                               hashlib.sha256(data).hexdigest())
            self.assertEqual(mod.check_receipt(
                state, {**original, 'ModTime': 'changed'}, target), 'invalid')

    def test_dangling_and_non_dangling_receipt_symlink_invalid(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.mkdir(mode=0o700)
            original = entry('file.bin', b'x')
            path = mod.receipt_path(state, original['Path'])
            path.parent.mkdir(mode=0o700, parents=True)
            path.symlink_to(root / 'nonexistent')
            self.assertEqual(mod.check_receipt(
                state, original, root / 'file.bin'), 'invalid')
            path.unlink()
            target = root / 'normal.json'
            target.write_text('{}')
            path.symlink_to(target)
            self.assertEqual(mod.check_receipt(
                state, original, root / 'file.bin'), 'invalid')

    def test_symlink_root_or_subdirectory_is_not_followed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            outside = root / 'outside'
            outside.mkdir(mode=0o700)
            state.symlink_to(outside, target_is_directory=True)
            original = entry('file.bin', b'x')
            with self.assertRaisesRegex(mod.ReceiptError,
                                        'UNSAFE_RECEIPT_DIRECTORY'):
                mod.create_receipt(
                    state, original, hashlib.sha256(b'x').hexdigest())
            self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_size_digest_or_directory_metadata_rejected(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            for original, sha in [
                (entry('file.bin', b'x'), 'bogus'),
                ({**entry('file.bin', b'x'), 'Size': True},
                 hashlib.sha256(b'x').hexdigest()),
                ({**entry('file.bin', b'x'), 'IsDir': True},
                 hashlib.sha256(b'x').hexdigest()),
                ({**entry('file.bin', b'x'), 'Path': '../danger'},
                 hashlib.sha256(b'x').hexdigest()),
            ]:
                with self.assertRaisesRegex(mod.ReceiptError,
                                            'INVALID_VERIFICATION_INPUT'):
                    mod.create_receipt(state, original, sha)
            self.assertFalse((state / mod.RECEIPT_DIR).exists())

    def test_receipt_json_corruption_and_permissions_are_invalid(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, dest = root / 'state', root / 'archive'
            state.mkdir(mode=0o700)
            dest.mkdir()
            data = b'abc'
            original = entry('file.bin', data)
            target = dest / 'file.bin'
            target.write_bytes(data)
            mod.create_receipt(state, original,
                               hashlib.sha256(data).hexdigest())
            path = mod.receipt_path(state, original['Path'])
            path.chmod(0o644)
            self.assertEqual(mod.check_receipt(state, original, target),
                             'invalid')
            path.chmod(0o600)
            path.write_text(json.dumps({'version': 99}))
            self.assertEqual(mod.check_receipt(state, original, target),
                             'invalid')


if __name__ == '__main__':
    unittest.main()
