import importlib.util
import pathlib
import unittest

SRC = pathlib.Path(__file__).parents[1] / 'scripts' / 'archive_batch.py'
spec = importlib.util.spec_from_file_location('archive_batch', SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PlanTests(unittest.TestCase):
    def test_bounds_and_already_present(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            dest = pathlib.Path(temp)
            (dest / 'a').write_text('existing')
            data = [{'Path': 'a', 'Size': 1}, {'Path': 'b', 'Size': 3},
                    {'Path': 'c', 'Size': 2}, {'Path': 'd', 'Size': 2}]
            chosen, _ = mod.plan(data, dest, max_files=2, max_bytes=5)
            self.assertEqual(chosen, [('b', 3), ('c', 2)])

    def test_oversized_deferred_and_path_guarded(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            dest = pathlib.Path(temp)
            data = [{'Path': '../bad', 'Size': 1}, {'Path': '/bad', 'Size': 1},
                    {'Path': 'big', 'Size': 101}, {'Path': 'okay', 'Size': 4},
                    {'Path': 'unknown', 'Size': -1}]
            chosen, over = mod.plan(data, dest, max_files=2, max_bytes=100)
            self.assertEqual(chosen, [('okay', 4)])
            self.assertEqual(over, 1)

    def test_symlink_parent_skipped(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            dest = pathlib.Path(temp)
            (dest / 'link').symlink_to('/tmp', target_is_directory=True)
            chosen, _ = mod.plan([{'Path': 'link/child', 'Size': 1}], dest)
            self.assertEqual(chosen, [])

    def test_unknown_type_skipped(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            chosen, _ = mod.plan([{'Path': 'x', 'Size': None}, {'Path': 'y', 'Size': True}], pathlib.Path(temp))
            self.assertEqual(chosen, [])


if __name__ == '__main__':
    unittest.main()
