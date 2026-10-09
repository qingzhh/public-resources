"""Offline storage contract tests. All writes are confined to temporary directories."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import seedkeep_storage as storage
from seedkeep_downloaders import ManagementError


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.settings = self.base / 'settings'
        self.allowed = self.base / 'inspect'
        self.local = self.allowed / 'store'
        self.local.mkdir(parents=True)
        self.roots = (str(self.allowed),)
        self.store = storage.Storage(self.settings, allowed_roots=self.roots, require_readonly=False)
        self.values = {'mappings': [self.mapping()]}

    def mapping(self, instance='qb', remote='/downloads', local=None, protected=None):
        return {'instance_id': instance, 'download_root': remote,
                'inspect_root': str(self.local if local is None else local),
                'protected_paths': [] if protected is None else protected}

    def save(self, values=None):
        return self.store.update({'revision': self.store.public()['revision'],
                                  'values': self.values if values is None else values})

    def descriptor(self, name='a', size=3, instance='qb', digest='a' * 40, directory='/downloads'):
        return {'instance_id': instance, 'type': 'qb' if instance == 'qb' else 'tr',
                'hash': digest, 'download_dir': directory, 'files': [{'name': name, 'size': size}]}

    def file(self, name='a', data=b'abc', root=None):
        path = (self.local if root is None else root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self, descriptor=None, references=None):
        descriptor = self.descriptor() if descriptor is None else descriptor
        return self.store.inspect(descriptor, [descriptor] if references is None else references)

    def assertBlocked(self, result):
        self.assertFalse(result['ok'])
        self.assertEqual(result['files'], [])
        self.assertEqual(result['logical_bytes'], 0)
        self.assertNotIn(str(self.base), result['reason'])
        self.assertNotIn('/downloads', result['reason'])

    def symlink(self, target, link, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest('Local symlinks unavailable: ' + type(error).__name__)

    def test_default_public_and_capability_do_not_write(self):
        self.assertFalse(self.settings.exists())
        public = self.store.public()
        self.assertEqual(set(public), {'values', 'revision', 'saved', 'allowed_roots'})
        self.assertEqual(public['values'], {'mappings': []})
        self.assertFalse(public['saved'])
        self.assertRegex(public['revision'], r'^[0-9a-f]{64}$')
        self.assertEqual(public['allowed_roots'], list(self.roots))
        self.assertFalse(self.store.capability('qb'))
        self.assertFalse(self.store.capability([]))
        self.assertFalse(self.settings.exists())
        public['values']['mappings'].append({})
        self.assertEqual(self.store.public()['values'], {'mappings': []})

    def test_save_revision_reload_and_mode(self):
        result = self.save()
        self.assertTrue(result['ok'])
        self.assertTrue(result['saved'])
        self.assertTrue(self.store.capability('qb'))
        self.assertFalse(self.store.capability('unknown'))
        self.assertFalse((self.settings / 'storage_mappings.json.tmp').exists())
        expected = hashlib.sha256(json.dumps(self.values, sort_keys=True, ensure_ascii=True,
                                            separators=(',', ':')).encode('utf-8')).hexdigest()
        self.assertEqual(result['revision'], expected)
        self.assertEqual(json.loads(self.store.path.read_text('utf-8')), self.values)
        fresh = storage.Storage(self.settings, allowed_roots=self.roots, require_readonly=False)
        self.assertEqual(fresh.public(), self.store.public())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.local.iterdir()), [])

    def test_stale_and_nonascii_revisions_are_409(self):
        old = self.store.public()['revision']
        self.save()
        before = self.store.path.read_bytes()
        for revision in (old, '不同版本', 'f' * 64):
            with self.subTest(revision=revision), self.assertRaises(storage.StorageError) as raised:
                self.store.update({'revision': revision, 'values': self.values})
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_update_detects_external_revision_and_corruption(self):
        self.save()
        old = self.store.public()['revision']
        self.store.path.write_text('{"mappings": []}', encoding='utf-8')
        with self.assertRaises(storage.StorageError) as raised:
            self.store.update({'revision': old, 'values': self.values})
        self.assertEqual(raised.exception.status, 409)
        self.store.path.write_text('broken-private-endpoint', encoding='utf-8')
        with self.assertRaises(storage.StorageError):
            self.store.update({'revision': old, 'values': self.values})
        self.assertEqual(self.store.path.read_text('utf-8'), 'broken-private-endpoint')
        self.assertFalse(self.store.capability('qb'))

    def test_corrupt_json_duplicate_keys_invalid_schema_fail_without_overwrite(self):
        self.settings.mkdir()
        path = self.settings / 'storage_mappings.json'
        for raw in ('{', '{"mappings": [], "mappings": []}', '{"mappings": [], "extra": 1}',
                    '[]', '{"mappings": null}', '{"mappings": ' + '[' * 2000):
            path.write_text(raw, encoding='utf-8')
            with self.subTest(raw=raw[:40]), self.assertRaises(storage.StorageError) as raised:
                storage.Storage(self.settings, allowed_roots=self.roots, require_readonly=False)
            self.assertNotIn(str(path), str(raised.exception))
            self.assertEqual(path.read_text('utf-8'), raw)
        path.write_bytes(b'\xff')
        with self.assertRaises(storage.StorageError):
            self.store.public()
        path.write_bytes(b' ' * (storage._MAX_CONFIG_BYTES + 1))
        with self.assertRaises(storage.StorageError):
            self.store.public()

    def test_strict_settings_fields_and_roots(self):
        for bad in ({}, {'mappings': (), 'extra': 1}, {'mappings': {}},
                    {'mappings': [self.mapping()] * 101}, {'mappings': [self.mapping()] * 2}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(storage.StorageError):
                storage.validate(bad, allowed_roots=self.roots)
        for key, bads in (
                ('instance_id', ('', 'a/b', 'a' * 65, 1)),
                ('download_root', ('/', '/data', '/app', 'downloads', '/downloads/',
                                   '/downloads/../x', '/downloads/./x', '//downloads', '/down\\loads')),
                ('inspect_root', (str(self.allowed), str(self.base / 'outside'),
                                  str(self.allowed) + '-other', str(self.local / '..' / 'escape'))),
                ('protected_paths', (None, ['/outside'], ['/downloads/../x'], ['/downloads/a/'],
                                     ['/downloads/a', '/downloads/a']))):
            for bad in bads:
                row = self.mapping()
                row[key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(storage.StorageError):
                    storage.validate({'mappings': [row]}, allowed_roots=self.roots)
        for key in self.mapping():
            row = self.mapping()
            del row[key]
            with self.assertRaises(storage.StorageError):
                storage.validate({'mappings': [row]}, allowed_roots=self.roots)
        row = dict(self.mapping(), secret='endpoint')
        with self.assertRaises(storage.StorageError):
            storage.validate({'mappings': [row]}, allowed_roots=self.roots)
        for roots in ((), ('relative',), (str(self.allowed), str(self.allowed))):
            with self.assertRaises(storage.StorageError):
                storage.validate({'mappings': []}, allowed_roots=roots)
        for body in ({}, {'revision': [], 'values': self.values},
                     {'revision': self.store.public()['revision'], 'values': self.values, 'extra': True}):
            with self.assertRaises(storage.StorageError):
                self.store.update(body)
        with self.assertRaises(storage.StorageError):
            storage.Storage(self.settings, require_readonly=False)

    def test_missing_inspection_directory_can_save_but_is_not_capable(self):
        self.save({'mappings': [self.mapping(local=self.allowed / 'absent')]})
        self.assertFalse(self.store.capability('qb'))
        self.assertFalse((self.allowed / 'absent').exists())
        self.assertBlocked(self.snapshot())

    def test_environment_roots_and_readonly_default(self):
        self.save()
        with patch.dict(os.environ, {'SEEDKEEP_INSPECT_ROOTS': str(self.allowed)}):
            default = storage.Storage(self.settings)
        self.assertTrue(default.require_readonly)
        self.assertEqual(default.allowed_roots, self.roots)
        self.assertFalse(default.capability('qb'))  # Actual local temporary mount is writable.
        with patch.object(storage, '_readonly', return_value=True):
            self.assertTrue(default.capability('qb'))
        with patch.object(storage, '_readonly', return_value=False):
            self.assertFalse(default.capability('qb'))
            self.assertBlocked(default.inspect(self.descriptor(), [self.descriptor()]))

    def test_readonly_mountinfo_longest_mount_and_statvfs(self):
        # Mount records are test fixtures; no mounts are made and no production is read.
        root = str(self.allowed)
        child = str(self.local)
        text = ('1 0 1:1 / ' + root + ' ro - tmpfs tmpfs rw\n'
                '2 1 1:2 / ' + child + ' rw - tmpfs tmpfs rw\n')
        with patch.object(storage.os, 'statvfs', create=True,
                          return_value=types.SimpleNamespace(f_flag=0)), \
                patch('builtins.open', return_value=io.StringIO(text)):
            self.assertFalse(storage._readonly(child))
        with patch.object(storage.os, 'statvfs', create=True,
                          return_value=types.SimpleNamespace(f_flag=0)), \
                patch('builtins.open', return_value=io.StringIO(text)):
            self.assertTrue(storage._readonly(root))
        with patch.object(storage.os, 'statvfs', create=True,
                          return_value=types.SimpleNamespace(f_flag=getattr(os, 'ST_RDONLY', 1))):
            self.assertTrue(storage._readonly(child))
        with patch.object(storage.os, 'statvfs', create=True, side_effect=PermissionError):
            self.assertFalse(storage._readonly(child))
        with patch.object(storage.os, 'statvfs', create=True,
                          return_value=types.SimpleNamespace(f_flag=0)), \
                patch('builtins.open', return_value=io.StringIO('malformed')):
            self.assertFalse(storage._readonly(child))

    def test_settings_cannot_write_in_content_roots(self):
        nested = storage.Storage(self.local / 'settings', allowed_roots=self.roots, require_readonly=False)
        with self.assertRaises(storage.StorageError):
            nested.update({'revision': nested.public()['revision'], 'values': self.values})
        self.assertFalse((self.local / 'settings').exists())

    def test_save_failure_preserves_existing_config(self):
        self.save()
        before = self.store.path.read_bytes()
        with patch.object(storage.pull, 'save_json', side_effect=PermissionError('private-path')):
            with self.assertRaises(storage.StorageError) as raised:
                self.save({'mappings': []})
        self.assertNotIn('private-path', str(raised.exception))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_exclusive_file_success_exact_private_records_and_no_content_writes(self):
        self.save()
        path = self.file()
        zero = self.file('empty', b'')
        candidate = self.descriptor()
        candidate['files'].append({'name': 'empty', 'size': 0})
        original = path.read_bytes()
        with patch.object(storage.pull, 'save_json', side_effect=AssertionError('inspection wrote settings')):
            result = self.snapshot(candidate)
            self.assertTrue(result['ok'])
            self.assertEqual(set(result), {'ok', 'reason', 'files', 'logical_bytes'})
            self.assertEqual(result['logical_bytes'], 3)
            info = path.stat()
            self.assertEqual(result['files'][0], {'path': str(path), 'remote_path': '/downloads/a',
                'size': 3, 'dev': info.st_dev, 'ino': info.st_ino, 'mtime_ns': info.st_mtime_ns, 'nlink': 1})
            self.assertTrue(self.store.recheck(result))
            self.assertFalse(self.store.gone(result))
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(zero.exists())

    def test_missing_wrong_size_directory_and_unknown_mapping_block(self):
        self.save()
        self.assertBlocked(self.snapshot())
        self.file()
        self.assertBlocked(self.snapshot(self.descriptor(size=4)))
        (self.local / 'directory').mkdir()
        self.assertBlocked(self.snapshot(self.descriptor(name='directory', size=0)))
        self.assertBlocked(self.snapshot(self.descriptor(directory='/downloads-other')))
        self.assertBlocked(self.snapshot(self.descriptor(instance='unknown')))
        reference = self.descriptor(name='missing', instance='unmapped', digest='b' * 40)
        self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))

    def test_strict_descriptor_format_and_file_traversal(self):
        self.save()
        for key, bads in (
                ('instance_id', ('a/b', 1)), ('type', ('other', [], None)),
                ('hash', ('a' * 39, 'z' * 40, [])),
                ('download_dir', ('relative', '/downloads/../x', '/downloads/', '/downloads//x')),
                ('files', ([], None, [{'name': 'a', 'size': True}], [{'name': 'a', 'size': -1}],
                           [{'name': 'a', 'size': 1.0}], [{'name': 'a', 'size': 2 ** 63}],
                           [{'name': 'a', 'size': 3, 'extra': True}])),
                ('aliases', (None, ['invalid'], ['a' * 40] * 65))):
            for bad in bads:
                candidate = self.descriptor()
                candidate[key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(storage.StorageError):
                    self.snapshot(candidate)
        for name in ('../a', 'a/../b', './a', '/a', 'a\\b', 'a//b', 'a/', '', 'a\x00', 'a\n'):
            with self.subTest(name=name), self.assertRaises(storage.StorageError):
                self.snapshot(self.descriptor(name=name))
        for key in self.descriptor():
            candidate = self.descriptor()
            del candidate[key]
            with self.assertRaises(storage.StorageError):
                self.snapshot(candidate)
        with self.assertRaises(storage.StorageError):
            self.snapshot(dict(self.descriptor(), owner='unknown'))
        with self.assertRaises(storage.StorageError):
            candidate = self.descriptor()
            candidate['files'] *= 2
            self.snapshot(candidate)

    def test_references_must_include_matching_self_and_be_valid(self):
        self.save()
        self.file()
        own = self.descriptor()
        bad_self = self.descriptor(size=4)
        for refs in ([], None, [bad_self], [own, own], [own, {}],
                     [self.descriptor(digest='b' * 40)]):
            with self.subTest(refs=refs), self.assertRaises(storage.StorageError):
                self.store.inspect(own, refs)
        upper = copy.deepcopy(own)
        upper['hash'] = upper['hash'].upper()
        self.assertTrue(self.snapshot(own, [upper])['ok'])

    def test_reference_bounds(self):
        candidate = self.descriptor()
        candidate['files'] = [{'name': str(i), 'size': 0} for i in range(20001)]
        with self.assertRaises(storage.StorageError):
            self.snapshot(candidate)
        candidate = self.descriptor()
        # Patch only the bound to exercise cumulative accounting without allocating 250k fixtures.
        reference = self.descriptor(name='b', digest='b' * 40)
        reference['files'].append({'name': 'c', 'size': 0})
        with patch.object(storage, '_MAX_REFERENCES', 2), self.assertRaises(storage.StorageError):
            self.snapshot(candidate, [candidate, reference])

    def test_shared_path_across_instances_blocks_even_wrong_reference_size(self):
        self.save({'mappings': [self.mapping(), self.mapping('tr', '/transmission')]})
        self.file()
        reference = self.descriptor(instance='tr', directory='/transmission', digest='b' * 40, size=99)
        self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))
        reference['hash'] = 'a' * 40  # Same hash on another instance is still another task.
        self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))

    def test_missing_reference_sibling_names_are_not_shared(self):
        self.save()
        self.file()
        own = self.descriptor()
        missing = self.descriptor(name='ab', digest='b' * 40)
        self.assertTrue(self.snapshot(own, [own, missing])['ok'])
        missing['files'][0]['name'] = 'absent/nested'
        self.assertTrue(self.snapshot(own, [own, missing])['ok'])
        self.file('ab')
        missing['files'][0]['name'] = 'ab'
        self.assertTrue(self.snapshot(own, [own, missing])['ok'])

    def test_alias_overlap_blocks_across_different_directories(self):
        other = self.allowed / 'other'
        other.mkdir()
        self.save({'mappings': [self.mapping(), self.mapping('tr', '/transmission', other)]})
        self.file()
        own = self.descriptor()
        own['aliases'] = ['c' * 64]
        reference = self.descriptor(instance='tr', directory='/transmission', digest='b' * 40)
        for aliases in (['c' * 64], ['A' * 40]):
            reference['aliases'] = aliases
            self.assertBlocked(self.snapshot(own, [own, reference]))
        reference['aliases'] = []
        reference['hash'] = 'c' * 64
        self.assertBlocked(self.snapshot(own, [own, reference]))

    def test_longest_prefix_and_candidate_directory_above_nested_mapping(self):
        nested = self.allowed / 'nested'
        nested.mkdir()
        self.save({'mappings': [self.mapping(), self.mapping('qb', '/downloads/special', nested)]})
        self.file('special/a', data=b'wrong')
        actual = self.file(root=nested)
        candidate = self.descriptor(directory='/downloads/special')
        result = self.snapshot(candidate)
        self.assertTrue(result['ok'])
        self.assertEqual(result['files'][0]['path'], str(actual))
        candidate = self.descriptor(name='special/a')
        self.assertTrue(self.snapshot(candidate)['ok'])
        actual.unlink()
        self.assertBlocked(self.snapshot(candidate))  # Never fall back to shorter mapping.

    def test_protected_subtree_cross_instance_and_sibling(self):
        self.save({'mappings': [self.mapping(),
            self.mapping('tr', '/transmission', protected=['/transmission/protected'])]})
        self.file('protected/a')
        self.assertBlocked(self.snapshot(self.descriptor(name='protected/a')))
        self.file('protected-other/a')
        self.assertTrue(self.snapshot(self.descriptor(name='protected-other/a'))['ok'])
        self.save({'mappings': [self.mapping(protected=['/downloads'])]})
        self.assertBlocked(self.snapshot(self.descriptor(name='protected-other/a')))

    def test_parent_protection_applies_to_nested_override_mapping(self):
        nested = self.allowed / 'nested'
        nested.mkdir()
        self.save({'mappings': [self.mapping(protected=['/downloads/protected']),
            self.mapping('qb', '/downloads/protected/nested', nested)]})
        self.file(root=nested)
        self.assertBlocked(self.snapshot(self.descriptor(directory='/downloads/protected/nested')))

    def test_external_and_shared_hardlinks_block(self):
        self.save()
        path = self.file()
        external = self.base / 'external-link'
        try:
            os.link(path, external)
        except (OSError, NotImplementedError) as error:
            self.skipTest('Local hardlinks unavailable: ' + type(error).__name__)
        self.assertGreater(path.stat().st_nlink, 1)
        self.assertBlocked(self.snapshot())
        external.unlink()
        self.assertTrue(self.snapshot()['ok'])
        sibling = self.local / 'b'
        os.link(path, sibling)
        reference = self.descriptor(name='b', digest='b' * 40)
        self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))

    def test_inode_collision_without_path_collision(self):
        # Real hardlinks are rejected before reference scanning. Emulate bind-mount inode aliasing.
        self.save()
        own_path = self.file()
        other_path = self.file('b')
        original = storage._walk
        def walk(path, **kwargs):
            result = original(path, **kwargs)
            if path == str(other_path):
                original_info = own_path.stat()
                return types.SimpleNamespace(st_mode=result.st_mode,
                    st_dev=original_info.st_dev, st_ino=original_info.st_ino)
            return result
        reference = self.descriptor(name='b', digest='b' * 40)
        with patch.object(storage, '_walk', side_effect=walk):
            self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))

    def test_protected_directory_bind_alias_blocks_without_any_other_task(self):
        alias = self.allowed / 'alias'
        protected = self.local / 'protected'
        alias.mkdir()
        protected.mkdir()
        self.file(root=alias)
        self.save({'mappings': [self.mapping(protected=['/downloads/protected']),
            self.mapping('qb', '/alternate', alias)]})
        original = storage._walk
        identity = protected.stat()
        def walk(path, **kwargs):
            result = original(path, **kwargs)
            if os.path.normcase(path) == os.path.normcase(str(alias)):
                return types.SimpleNamespace(st_mode=result.st_mode, st_dev=identity.st_dev, st_ino=identity.st_ino)
            return result
        candidate = self.descriptor(directory='/alternate')
        self.assertTrue(self.snapshot(candidate)['ok'])
        with patch.object(storage, '_walk', side_effect=walk):
            self.assertBlocked(self.snapshot(candidate))

    def test_unreadable_protected_identity_preserves_whole_candidate(self):
        self.file()
        self.save({'mappings': [self.mapping(protected=['/downloads/protected'])]})
        self.assertBlocked(self.snapshot())

    def test_file_symlink_blocks_candidate_reference_and_gone(self):
        self.save()
        target = self.file()
        link = self.local / 'linked'
        self.symlink(target, link)
        self.assertBlocked(self.snapshot(self.descriptor(name='linked')))
        reference = self.descriptor(name='linked', digest='b' * 40)
        self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))
        snapshot = self.snapshot()
        target.unlink()
        self.symlink(self.base / 'absent-target', target)
        self.assertFalse(self.store.recheck(snapshot))
        self.assertFalse(self.store.gone(snapshot))

    def test_directory_symlink_and_allowed_root_symlink_block(self):
        self.save()
        outside = self.base / 'outside'
        outside.mkdir()
        self.file(root=outside)
        linked = self.local / 'linked'
        self.symlink(outside, linked, directory=True)
        self.assertBlocked(self.snapshot(self.descriptor(name='linked/a')))
        self.save({'mappings': [self.mapping(local=linked)]})
        self.assertFalse(self.store.capability('qb'))
        self.assertBlocked(self.snapshot())
        linked_root = self.base / 'linked-root'
        self.symlink(self.allowed, linked_root, directory=True)
        injected = storage.Storage(self.base / 'other-settings', allowed_roots=(str(linked_root),),
                                   require_readonly=False)
        injected.update({'revision': injected.public()['revision'],
            'values': {'mappings': [self.mapping(local=linked_root / 'store')]}})
        self.assertFalse(injected.capability('qb'))

    @unittest.skipUnless(os.name == 'nt', 'Windows reparse points only')
    def test_actual_windows_junction_blocks(self):
        self.save()
        outside = self.base / 'outside'
        outside.mkdir()
        self.file(root=outside)
        junction = self.local / 'junction'
        command = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        if command.returncode:
            self.skipTest('Local junction creation unavailable')
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        self.assertTrue(os.lstat(junction).st_file_attributes & 0x400)
        self.assertBlocked(self.snapshot(self.descriptor(name='junction/a')))
        self.save({'mappings': [self.mapping(local=junction)]})
        self.assertFalse(self.store.capability('qb'))

    def test_reparse_attribute_detection(self):
        self.assertTrue(storage._unsafe_stat(types.SimpleNamespace(st_mode=stat.S_IFDIR,
                                                                  st_file_attributes=0x400)))

    def test_unreadable_candidate_reference_and_absence_fail_closed(self):
        self.save()
        path = self.file()
        result = self.snapshot()
        original = os.lstat
        def denied(value, *args, **kwargs):
            if os.fspath(value) == str(path):
                raise PermissionError('secret-path')
            return original(value, *args, **kwargs)
        with patch.object(storage.os, 'lstat', side_effect=denied):
            self.assertBlocked(self.snapshot())
            self.assertFalse(self.store.recheck(result))
            self.assertFalse(self.store.gone(result))
        reference_path = str(self.local / 'missing')
        def ref_denied(value, *args, **kwargs):
            if os.fspath(value) == reference_path:
                raise PermissionError('secret-reference')
            return original(value, *args, **kwargs)
        reference = self.descriptor(name='missing', digest='b' * 40)
        with patch.object(storage.os, 'lstat', side_effect=ref_denied):
            self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))
        path.unlink()
        with patch.object(storage.os, 'access', return_value=False):
            self.assertFalse(self.store.gone(result))
            self.assertFalse(self.store.capability('qb'))

    @unittest.skipIf(os.name == 'nt' or not hasattr(os, 'geteuid') or os.geteuid() == 0,
                     'POSIX permission test requires non-root POSIX user')
    def test_actual_unreadable_directory(self):
        self.save()
        self.file('private/a')
        private = self.local / 'private'
        private.chmod(0)
        self.addCleanup(lambda: private.chmod(0o700))
        self.assertBlocked(self.snapshot(self.descriptor(name='private/a')))

    def test_recheck_identity_changes_and_gone_replacements(self):
        self.save()
        path = self.file()
        original = self.snapshot()
        path.write_bytes(b'xyz')
        before = original['files'][0]['mtime_ns']
        os.utime(path, ns=(before + 1000000, before + 1000000))
        self.assertFalse(self.store.recheck(original))
        original = self.snapshot()
        path.write_bytes(b'longer')
        self.assertFalse(self.store.recheck(original))
        self.assertFalse(self.store.gone(original))
        path.unlink()
        self.assertFalse(self.store.recheck(original))
        self.assertTrue(self.store.gone(original))
        path.mkdir()
        self.assertFalse(self.store.gone(original))
        path.rmdir()
        self.file()
        self.assertFalse(self.store.gone(original))

    def test_recheck_inode_and_hardlink_changes(self):
        self.save()
        path = self.file()
        result = self.snapshot()
        path.rename(self.local / 'old')  # Retain old inode to avoid filesystem reuse.
        self.file()
        self.assertNotEqual(path.stat().st_ino, result['files'][0]['ino'])
        self.assertFalse(self.store.recheck(result))
        result = self.snapshot()
        try:
            os.link(path, self.base / 'hardlink')
        except (OSError, NotImplementedError) as error:
            self.skipTest('Local hardlinks unavailable: ' + type(error).__name__)
        self.assertFalse(self.store.recheck(result))

    def test_gone_requires_all_files_and_observable_missing_ancestors(self):
        self.save()
        first = self.file('task/a')
        second = self.file('task/b')
        candidate = self.descriptor(name='task/a')
        candidate['files'].append({'name': 'task/b', 'size': 3})
        result = self.snapshot(candidate)
        self.assertTrue(result['ok'])
        first.unlink()
        self.assertFalse(self.store.gone(result))
        second.unlink()
        (self.local / 'task').rmdir()
        self.assertTrue(self.store.gone(result))
        (self.local / 'task').write_bytes(b'not-a-directory')
        self.assertFalse(self.store.gone(result))

    def test_snapshot_corruption_raises_and_failed_snapshot_is_not_actionable(self):
        self.save()
        self.file()
        snapshot = self.snapshot()
        bad_values = []
        for key, bad in (('ok', 1), ('files', []), ('logical_bytes', True), ('logical_bytes', 4),
                         ('reason', [])):
            value = copy.deepcopy(snapshot)
            value[key] = bad
            bad_values.append(value)
        for key, bad in (('path', str(self.base / 'outside')), ('remote_path', '../bad'),
                         ('size', True), ('ino', 0), ('nlink', 2), ('mtime_ns', -1), ('dev', 'x')):
            value = copy.deepcopy(snapshot)
            value['files'][0][key] = bad
            bad_values.append(value)
        bad_values.extend(({}, dict(snapshot, extra=True), None))
        for bad in bad_values:
            for method in (self.store.recheck, self.store.gone):
                with self.subTest(method=method.__name__, bad=str(bad)[:60]), \
                        self.assertRaises(storage.StorageError):
                    method(bad)
        failed = {'ok': False, 'reason': '无法确认', 'files': [], 'logical_bytes': 0}
        self.assertFalse(self.store.recheck(failed))
        self.assertFalse(self.store.gone(failed))

    def test_mapping_changes_and_new_protection_invalidate_snapshot(self):
        self.save()
        path = self.file()
        result = self.snapshot()
        self.save({'mappings': [self.mapping(protected=['/downloads/a'])]})
        self.assertFalse(self.store.recheck(result))
        path.unlink()
        self.assertFalse(self.store.gone(result))
        self.save({'mappings': []})
        self.assertFalse(self.store.recheck(result))
        self.assertFalse(self.store.gone(result))

    def test_final_recheck_detects_mutation_during_inspection(self):
        self.save()
        path = self.file()
        self.file('b')
        original = storage._walk
        def walk(value, **kwargs):
            result = original(value, **kwargs)
            if value == str(self.local / 'b'):
                path.write_bytes(b'modified')
            return result
        reference = self.descriptor(name='b', digest='b' * 40)
        with patch.object(storage, '_walk', side_effect=walk):
            self.assertBlocked(self.snapshot(references=[self.descriptor(), reference]))

    def test_readonly_root_does_not_allow_writable_nested_file_mount(self):
        self.save()
        path = self.file()
        readonly = storage.Storage(self.settings, allowed_roots=self.roots)
        def mount_flags(value):
            return value != str(path)
        with patch.object(storage, '_readonly', side_effect=mount_flags):
            self.assertTrue(readonly.capability('qb'))
            self.assertBlocked(readonly.inspect(self.descriptor(), [self.descriptor()]))
        snapshot = self.snapshot()
        with patch.object(storage, '_readonly', side_effect=mount_flags):
            self.assertFalse(readonly.recheck(snapshot))

    def test_bounded_protected_paths_and_native_alias_names(self):
        row = self.mapping(protected=['/downloads/' + str(i) for i in range(101)])
        with self.assertRaises(storage.StorageError):
            storage.validate({'mappings': [row]}, allowed_roots=self.roots)
        if os.name == 'nt':
            for name in ('a:stream', 'a.', 'a ', 'CON', 'NUL.txt', 'COM1'):
                candidate = self.descriptor(name=name)
                self.save()
                try:
                    result = self.snapshot(candidate)
                except storage.StorageError:
                    continue
                self.assertBlocked(result)

    def test_new_longest_mapping_invalidates_old_snapshot(self):
        self.save()
        path = self.file('special/a')
        snapshot = self.snapshot(self.descriptor(name='special/a'))
        self.assertTrue(snapshot['ok'])
        nested = self.allowed / 'nested'
        nested.mkdir()
        self.save({'mappings': [self.mapping(), self.mapping('qb', '/downloads/special', nested)]})
        self.assertFalse(self.store.recheck(snapshot))
        path.unlink()
        self.assertFalse(self.store.gone(snapshot))

    def test_atomic_save_refuses_existing_temp_files_and_links(self):
        self.save()
        before = self.store.path.read_bytes()
        temporary = self.store.path.with_suffix('.json.tmp')
        temporary.write_bytes(b'stale')
        with self.assertRaises(storage.StorageError):
            self.save({'mappings': []})
        self.assertEqual(temporary.read_bytes(), b'stale')
        self.assertEqual(self.store.path.read_bytes(), before)
        temporary.unlink()
        target = self.file()
        self.symlink(target, temporary)
        with self.assertRaises(storage.StorageError):
            self.save({'mappings': []})
        self.assertEqual(target.read_bytes(), b'abc')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_readonly_gone_and_missing_reference_probe_existing_parent(self):
        self.save()
        path = self.file('task/a')
        own = self.descriptor(name='task/a')
        reference = self.descriptor(name='uncompleted/file', digest='b' * 40)
        readonly = storage.Storage(self.settings, allowed_roots=self.roots)
        with patch.object(storage, '_readonly', return_value=True) as mounts:
            snapshot = readonly.inspect(own, [own, reference])
            self.assertTrue(snapshot['ok'])
            self.assertIn(unittest.mock.call(str(self.local)), mounts.call_args_list)
        path.unlink()
        with patch.object(storage, '_readonly', return_value=True) as mounts:
            self.assertTrue(readonly.gone(snapshot))
            self.assertIn(unittest.mock.call(str(self.local / 'task')), mounts.call_args_list)
        with patch.object(storage, '_readonly', side_effect=lambda value: value == str(self.local)):
            self.assertFalse(readonly.gone(snapshot))

    def test_errors_never_echo_supplied_path_or_endpoint(self):
        error = storage.StorageError('https://private.invalid/secret')
        self.assertIsInstance(error, ManagementError)
        self.assertNotIn('private', str(error))
        with self.assertRaises(storage.StorageError) as raised:
            storage.validate({'mappings': [self.mapping(remote='https://private.invalid/secret')]},
                             allowed_roots=self.roots)
        self.assertNotIn('private', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
