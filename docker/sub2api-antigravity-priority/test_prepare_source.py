import hashlib
import io
import json
import pathlib
import tarfile
import tempfile
import unittest

import prepare_source


def archive_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, value in files.items():
            item = tarfile.TarInfo('sub2api-fixture/' + name)
            item.size = len(value)
            archive.addfile(item, io.BytesIO(value))
    return output.getvalue()


class SourcePreparationTests(unittest.TestCase):
    def fixture(self, root):
        package = root / 'package'
        package.mkdir()
        (package / 'overlay/backend').mkdir(parents=True)
        (package / 'engine').mkdir()
        (package / 'overlay/backend/new.go').write_bytes(b'package changed\n')
        (package / 'engine/go.mod').write_bytes(b'module fixture\n')
        manifest = {'upstream_commit': 'fixture', 'overlay': {'backend/new.go': {'upstream_sha256': None, 'sha256': hashlib.sha256(b'package changed\n').hexdigest()}}, 'engine': {'go.mod': hashlib.sha256(b'module fixture\n').hexdigest()}}
        (package / 'source-manifest.json').write_text(json.dumps(manifest))
        for name in ['build.sh', 'Dockerfile', '.dockerignore', 'sources.json']:
            (package / name).write_bytes(b'fixture\n')
        return package

    def test_reconstructs_new_file_parents_and_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            destination = root / 'result'
            prepare_source.prepare(package, destination, archive_bytes({'LICENSE': b'fixture license'}))
            self.assertEqual((destination / 'sub2api/backend/new.go').read_bytes(), b'package changed\n')
            self.assertEqual((destination / 'engine/go.mod').read_bytes(), b'module fixture\n')

    def test_changed_overlay_stops_before_creating_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            (package / 'overlay/backend/new.go').write_bytes(b'tampered')
            with self.assertRaises(RuntimeError):
                prepare_source.prepare(package, root / 'result', archive_bytes({'LICENSE': b'license'}))
            self.assertFalse((root / 'result').exists())

    def test_upstream_contract_change_stops_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            with self.assertRaises(RuntimeError):
                prepare_source.prepare(package, root / 'result', archive_bytes({'backend/new.go': b'unexpected upstream file'}))
            self.assertFalse((root / 'result').exists())

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            with self.assertRaises(RuntimeError):
                prepare_source.prepare(package, root / 'result', archive_bytes({'../escape': b'bad'}))
            self.assertFalse((root / 'escape').exists())
            self.assertFalse((root / 'result').exists())

    def test_existing_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            destination = root / 'result'
            destination.mkdir()
            (destination / 'user-file').write_bytes(b'preserve')
            with self.assertRaises(RuntimeError):
                prepare_source.prepare(package, destination, archive_bytes({}))
            self.assertEqual((destination / 'user-file').read_bytes(), b'preserve')

    def test_unlisted_overlay_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            (package / 'overlay/backend/unlisted.go').write_bytes(b'new change')
            with self.assertRaises(RuntimeError):
                prepare_source.prepare(package, root / 'result', archive_bytes({}))
            self.assertFalse((root / 'result').exists())

    def test_manifest_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = self.fixture(root)
            metadata = json.loads((package / 'source-manifest.json').read_text())
            metadata['engine']['../escape'] = 'not-used'
            (package / 'source-manifest.json').write_text(json.dumps(metadata))
            with self.assertRaises(RuntimeError):
                prepare_source.prepare(package, root / 'result', archive_bytes({}))
            self.assertFalse((root / 'result').exists())


if __name__ == '__main__':
    unittest.main()
