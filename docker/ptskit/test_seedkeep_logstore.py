"""Offline tests for bounded private logs and nondestructive cleanup."""
from concurrent.futures import ThreadPoolExecutor
import datetime
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import seedkeep_logstore as logs


NOW = datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc).timestamp()


def line(**values):
    return (json.dumps(values, ensure_ascii=False) + '\n').encode('utf-8')


class LogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = NOW
        self.store = logs.Store(self.directory, clock=lambda: self.now)

    def write(self, name, raw):
        path = self.directory / name
        path.write_bytes(raw)
        return path

    def auto_policy(self, **values):
        return self.store.update_policy({**logs.DEFAULT_POLICY, 'auto_cleanup_enabled': True, **values})

    def symlink(self, target, name, directory=False):
        path = self.directory / name
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest('OS does not permit test symlinks: ' + str(exc.winerror if os.name == 'nt' else exc.errno))
        return path

    def test_defaults_and_policy_persist_independently(self):
        self.assertEqual(self.store.policy(), logs.DEFAULT_POLICY)
        returned = self.store.policy()
        returned['retention_days'] = 1
        self.assertEqual(self.store.policy()['retention_days'], 30)
        expected = self.auto_policy(retention_days=3650, cleanup_interval_hours=8760)
        self.assertEqual(logs.Store(self.directory).policy(), expected)
        self.assertFalse((self.directory / 'docker_settings.json').exists())
        self.assertFalse((self.directory / 'log_cleanup_meta.json').exists())
        expected = self.auto_policy(retention_days=1, cleanup_interval_hours=1)
        self.assertEqual(self.store.policy(), expected)

    def test_policy_requires_exact_fields_strict_types_and_bounds(self):
        path = self.directory / 'log_policy.json'
        self.auto_policy()
        before = path.read_bytes()
        invalid = [None, [], {}, {'retention_days': 30},
                   {**logs.DEFAULT_POLICY, 'unexpected': 0}]
        for key, values in (
            ('auto_cleanup_enabled', [0, 1, 'false', None]),
            ('retention_days', [0, 3651, True, False, 1.0, '30', None]),
            ('cleanup_interval_hours', [0, 8761, True, False, 1.0, '24', None])):
            invalid.extend({**logs.DEFAULT_POLICY, key: value} for value in values)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update_policy(value)
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_or_oversized_policy_is_not_silently_reset(self):
        for raw in (b'broken', b'[]', b'{"auto_cleanup_enabled": true}', b' ' * 5000):
            with self.subTest(raw_length=len(raw)):
                path = self.write('log_policy.json', raw)
                with self.assertRaises(ValueError):
                    self.store.policy()
                self.assertEqual(path.read_bytes(), raw)

    def test_read_merges_all_four_files_and_sorts_absolute_time(self):
        entries = [('run.log.1', '2026-10-02T23:00:00-0100', 'a'),
                   ('run.log', '2026-10-03T03:00:01+03:00', 'b'),
                   ('management.log.1', NOW + 2, 'c'),
                   ('management.log', '2026-10-03T00:00:03Z', 'd')]
        for name, stamp, event in entries:
            self.write(name, line(time=stamp, event=event))
        result = self.store.read(limit=2)
        self.assertEqual(result['total'], 4)
        self.assertEqual([item['event'] for item in result['items']], ['d', 'c'])

    def test_read_arguments_are_strict_and_bounded(self):
        for value in (0, -1, 1001, True, 1.0, '10', None):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.store.read(limit=value)
        for kind in ('errors', '', None, [], {}):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.store.read(kind=kind)
        self.assertEqual(self.store.read(), {'items': [], 'total': 0})

    def test_error_classification_does_not_treat_stop_reason_as_failure(self):
        errors = [{'event': 'failed'}, {'event': 'download_failed'}, {'event': 'request_error'},
                  {'event': 'progress', 'errors': 1}, {'event': 'progress', 'errors': 0.5},
                  {'event': 'progress', 'error': 'private message'},
                  {'event': 'progress', 'error': {'token': 'synthetic-secret'}}]
        normal = [{'event': 'run_complete', 'reason': 'stop requested'},
                  {'event': 'run_complete', 'reason': 'download_failed'},
                  {'event': 'failure'}, {'event': 'failed_count'},
                  {'event': 'progress', 'errors': 0}, {'event': 'progress', 'errors': '1'},
                  {'event': 'progress', 'errors': True}, {'event': 'progress', 'error': ''},
                  {'event': 'progress', 'error': '   '}, {'event': 'progress', 'error': 0},
                  {'event': 'progress', 'error': []}, {'event': 'progress', 'level': 'error'}]
        raw = b''.join(line(time=NOW, **value) for value in errors + normal)
        self.write('run.log', raw)
        result = self.store.read(kind='error')
        self.assertEqual(result['total'], len(errors))
        self.assertTrue(all(item['level'] == 'error' for item in result['items']))
        counts = self.store.cleanup('errors')
        self.assertEqual(counts['removed'], len(errors))
        self.assertEqual(counts['kept'], len(normal))
        self.assertEqual((self.directory / 'run.log').read_bytes(),
                         b''.join(line(time=NOW, **value) for value in normal))

    def test_read_whitelist_safe_scalars_levels_and_secret_redaction(self):
        sensitive = {'connection': {'password': 'synthetic-password'}, 'password': 'synthetic-password',
                     'token': 'synthetic-token', 'unknown': {'token': 'synthetic-nested'},
                     'time': NOW, 'event': 'request_failed', 'errors': 1,
                     'reason': 'password=synthetic-password token="synthetic-token" '
                               'https://synthetic-user:synthetic-password@private.invalid/path '
                               'Bearer synthetic-bearer authorization: Bearer synthetic-header 192.0.2.8:1234',
                     'level': {'token': 'synthetic-level'}, 'processed': {'password': 'synthetic-counter'},
                     'managed_active': True}
        self.write('management.log', line(**sensitive))
        item = self.store.read()['items'][0]
        self.assertEqual(set(item), {'time', 'event', 'reason', 'errors', 'level'})
        public = json.dumps(item)
        for secret in ('synthetic-password', 'synthetic-token', 'synthetic-nested', 'synthetic-user',
                       'private.invalid', 'synthetic-bearer', 'synthetic-header', '192.0.2.8', 'synthetic-counter', 'synthetic-level'):
            self.assertNotIn(secret, public)
        self.assertEqual(item['level'], 'error')
        self.write('management.log', line(time={'token': 'synthetic-time'},
                                          event={'token': 'synthetic-event'},
                                          reason={'password': 'synthetic-reason'}, level='<script>'))
        self.assertEqual(self.store.read()['items'], [{'reason': None, 'level': 'info'}])
        self.write('management.log', line(event='progress', level='warning'))
        self.assertEqual(self.store.read()['items'][0]['level'], 'warning')

    def test_expired_uses_epoch_offsets_and_strict_cutoff_preserves_unknown(self):
        cutoff = NOW - 30 * 86400
        expired = [line(time=cutoff - 1, event='old'),
                   line(time='2026-09-02T19:59:59-04:00', event='old_offset')]
        preserved = [line(time=cutoff, event='boundary'), line(time=NOW + 86400, event='future'),
                     line(time='2020-01-01T00:00:00', event='naive'),
                     line(time='unknown', event='unknown'), line(event='missing'),
                     line(time=True, event='bool_time'), line(time=10 ** 600, event='huge_time'),
                     b'bad JSON with synthetic-token\r\n', b'\xff\xfe\n', b'[]\n',
                     b'{"time": 1, "counter": NaN}\n', b'{"time": Infinity}\n',
                     b'{"event":"unterminated"}']
        path = self.write('run.log', b''.join(expired + preserved))
        counts = self.store.cleanup('expired')
        self.assertEqual(counts['removed'], 2)
        self.assertEqual(counts['kept'], len(preserved))
        self.assertEqual(path.read_bytes(), b''.join(preserved))
        self.assertEqual(counts['bytes_removed'], sum(map(len, expired)))

    def test_error_cleanup_preserves_bad_and_sensitive_normal_lines_byte_for_byte(self):
        keep = [b'not json token=synthetic-secret\r\n',
                b'{ "event" : "run_complete", "connection": {"password":"synthetic-secret"} }\n',
                b'{"event":"failed", BROKEN}\n', b'{"event":"stop","reason":"user"}']
        remove = line(event='failed', error='synthetic-private-error')
        path = self.write('management.log', remove + b''.join(keep))
        result = self.store.cleanup('errors')
        self.assertEqual(result['removed'], 1)
        self.assertEqual(path.read_bytes(), b''.join(keep))
        self.assertNotIn('synthetic-secret', json.dumps(self.store.read()))

    def test_oversized_lines_stream_and_are_preserved_for_targeted_cleanup(self):
        huge = line(time=1, event='failed', connection='x' * (logs.MAX_LINE_BYTES * 5))
        tail = line(time=NOW, event='ok')
        raw = huge + tail

        class BoundedStream(io.BytesIO):
            def readline(inner, size=-1):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, logs.MAX_LINE_BYTES + 1)
                return super().readline(size)

            def read(inner, size=-1):
                raise AssertionError('unbounded whole-file read')

        records = list(logs._records(BoundedStream(raw)))
        self.assertEqual(sum(int(start) for _, _, start in records), 2)
        self.assertEqual(sum(row is not None for _, row, _ in records), 1)
        path = self.write('run.log', raw)
        self.assertEqual(self.store.read()['total'], 1)
        for mode in ('errors', 'expired'):
            result = self.store.cleanup(mode)
            self.assertEqual(result['removed'], 0)
            self.assertEqual(result['kept'], 2)
            self.assertEqual(path.read_bytes(), raw)
        result = self.store.cleanup('all')
        self.assertEqual(result['removed'], 2)
        self.assertEqual(result['bytes_removed'], len(raw))
        self.assertEqual(path.read_bytes(), b'')

    def test_deep_invalid_json_and_huge_counters_do_not_break_read(self):
        nested = b'{"event":"failed","unknown":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}BROKEN\n'
        ordinary = line(time=NOW, event='ok', errors=10 ** 600, processed=10 ** 600)
        self.write('run.log', nested + ordinary)
        self.assertEqual(self.store.read(), {'items': [{'time': NOW, 'event': 'ok', 'level': 'error'}], 'total': 1})
        self.store.cleanup('errors')
        self.assertEqual((self.directory / 'run.log').read_bytes(), nested)

    def test_large_valid_input_keeps_only_limit_items_but_reports_total(self):
        self.write('run.log', b''.join(line(time=NOW + index, event='progress', processed=index)
                                     for index in range(4000)))
        original_push = logs.heapq.heappush

        def bounded_push(heap, entry):
            self.assertLess(len(heap), 3)
            return original_push(heap, entry)

        with patch.object(logs.heapq, 'heappush', side_effect=bounded_push):
            result = self.store.read(limit=3)
        self.assertEqual(result['total'], 4000)
        self.assertEqual([row['processed'] for row in result['items']], [3999, 3998, 3997])
    def test_read_byte_budget_discards_partial_first_line_without_parsing_it(self):
        prefix = line(time=NOW, event='prefix')
        middle = line(time=NOW + 1, event='middle')
        tail = line(time=NOW + 2, event='tail')
        path = self.write('run.log', prefix + middle + tail)
        with patch.object(logs, 'MAX_READ_BYTES', len(tail) + 5):
            result = self.store.read()
        self.assertEqual(result, {'items': [{'time': NOW + 2, 'event': 'tail', 'level': 'info'}],
                                  'total': 1, 'truncated': True})
        with patch.object(logs, 'MAX_READ_BYTES', len(middle + tail)):
            result = self.store.read()
        self.assertEqual(result['total'], 2)
        self.assertEqual([row['event'] for row in result['items']], ['tail', 'middle'])
        self.assertTrue(result['truncated'])
        path.write_bytes(b'x' * 100 + b'{"event":"failed"}')
        with patch.object(logs, 'MAX_READ_BYTES', 20):
            self.assertEqual(self.store.read(), {'items': [], 'total': 0, 'truncated': True})


    def test_whitelist_does_not_touch_operations_backups_or_other_logs(self):
        unmanaged = ['operations.json', 'batch.json', 'seen_hashes.json', 'run.log.2',
                     'management.log.backup', 'log_policy.json.backup', 'run.lock']
        for name in unmanaged:
            self.write(name, b'synthetic recovery data')
        (self.directory / 'backups').mkdir()
        backup = self.write('backups/run.log', b'synthetic backup')
        for name in logs.LOG_NAMES:
            self.write(name, b'broken\n' + line(time=1, event='failed'))
        result = self.store.cleanup('all')
        self.assertEqual(result['files'], 4)
        self.assertEqual(result['removed'], 8)
        self.assertEqual(result['kept'], 0)
        for name in unmanaged:
            self.assertEqual((self.directory / name).read_bytes(), b'synthetic recovery data')
        self.assertEqual(backup.read_bytes(), b'synthetic backup')
        for name in ('operations.json', 'run.log.2', '../run.log', '/run.log', 'backups/run.log', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                logs.append(self.directory, name, {'event': 'ok'})
        with self.assertRaises(ValueError):
            logs.append(self.directory, 'run.log', [])
        with self.assertRaises(ValueError):
            self.store.cleanup('invalid')

    def test_append_sanitizes_disk_and_retains_error_classification(self):
        raw = line(time=NOW, event='existing').rstrip(b'\n')
        path = self.write('management.log', raw)
        logs.append(self.directory, 'management.log', {'event': 'progress', 'error': {'token': 'synthetic-secret'},
                                                     'connection': 'synthetic-private', 'password': 'synthetic-secret',
                                                     'reason': 'password=synthetic-secret'})
        self.assertTrue(path.read_bytes().startswith(raw + b'\n'))
        self.assertNotIn(b'synthetic-secret', path.read_bytes())
        self.assertNotIn(b'synthetic-private', path.read_bytes())
        self.assertEqual(self.store.read()['total'], 2)
        self.assertEqual(self.store.read(kind='error')['total'], 1)

    def test_tick_disabled_then_due_and_interval_survives_new_store(self):
        path = self.write('run.log', line(time=1, event='old'))
        self.assertIsNone(self.store.tick())
        self.assertTrue(path.read_bytes())
        self.auto_policy(cleanup_interval_hours=1)
        self.assertEqual(self.store.tick()['removed'], 1)
        self.assertIsNone(logs.Store(self.directory, clock=lambda: self.now).tick())
        self.now += 3599
        self.assertIsNone(self.store.tick())
        self.now += 1
        self.assertIsNotNone(self.store.tick())
        self.now -= 100
        self.assertIsNotNone(self.store.tick())  # Clock rollback does not stall forever.

    def test_tick_failure_keeps_last_success_and_retries(self):
        self.auto_policy(cleanup_interval_hours=1)
        self.store.tick()
        meta = self.directory / 'log_cleanup_meta.json'
        before = meta.read_bytes()
        self.now += 3600
        original = line(time=1, event='old')
        path = self.write('run.log', original)
        with patch.object(logs.os, 'replace', side_effect=OSError('simulated replace failure')):
            with self.assertRaises(OSError):
                self.store.tick()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(meta.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('.logstore-*.tmp')), [])
        self.assertEqual(self.store.tick()['removed'], 1)
        self.assertNotEqual(meta.read_bytes(), before)

    def test_first_tick_failure_does_not_create_success_metadata(self):
        self.auto_policy()
        original = line(time=1, event='old')
        path = self.write('run.log', original)
        with patch.object(logs.os, 'fsync', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                self.store.tick()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.directory / 'log_cleanup_meta.json').exists())
        self.assertEqual(list(self.directory.glob('.logstore-*.tmp')), [])
        self.assertEqual(self.store.tick()['removed'], 1)

    def test_policy_replace_failure_retains_old_policy(self):
        self.auto_policy()
        path = self.directory / 'log_policy.json'
        before = path.read_bytes()
        with patch.object(logs.os, 'replace', side_effect=OSError('simulated policy failure')):
            with self.assertRaises(OSError):
                self.auto_policy(retention_days=1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('.logstore-*.tmp')), [])

    def test_meta_failure_never_advances_success_time(self):
        self.auto_policy(cleanup_interval_hours=1)
        self.store.tick()
        path = self.directory / 'log_cleanup_meta.json'
        before = path.read_bytes()
        self.now += 3600
        self.write('run.log', line(time=1, event='old'))
        original_replace = logs.os.replace

        def fail_meta(source, destination):
            if Path(destination).name == 'log_cleanup_meta.json':
                raise OSError('simulated metadata failure')
            return original_replace(source, destination)

        with patch.object(logs.os, 'replace', side_effect=fail_meta):
            with self.assertRaises(OSError):
                self.store.tick()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.tick()['removed'], 0)

    def test_corrupt_meta_prevents_automatic_cleanup(self):
        self.auto_policy()
        for raw in (b'broken', b' ' * 5000, b'{"last_cleanup_at": true}', b'{"last_cleanup_at": NaN}'):
            with self.subTest(raw_length=len(raw)):
                self.write('log_cleanup_meta.json', raw)
                path = self.write('run.log', line(time=1, event='old'))
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.tick()
                self.assertEqual(path.read_bytes(), before)

    def test_threaded_append_and_cleanup_do_not_lose_success_events(self):
        def writer(worker):
            for index in range(30):
                logs.append(self.directory, 'management.log', {'time': NOW, 'event': 'progress',
                                                              'processed': worker * 100 + index})
                logs.append(self.directory, 'management.log', {'time': NOW, 'event': 'failed'})

        def cleaner():
            for _ in range(15):
                logs.Store(self.directory).cleanup('errors')

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(writer, worker) for worker in range(4)] + [pool.submit(cleaner)]
            for future in futures:
                future.result(timeout=30)
        self.store.cleanup('errors')
        result = self.store.read(limit=1000)
        self.assertEqual(result['total'], 120)
        self.assertEqual({row['processed'] for row in result['items']},
                         {worker * 100 + index for worker in range(4) for index in range(30)})

    def test_append_waits_for_atomic_cleanup_replace(self):
        self.write('run.log', line(time=1, event='old'))
        replacing, release = threading.Event(), threading.Event()
        started, appended = threading.Event(), threading.Event()
        original_replace = logs.os.replace

        def pause_replace(source, destination):
            if Path(destination).name == 'run.log':
                replacing.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release replacement')
            return original_replace(source, destination)

        def writer():
            started.set()
            logs.append(self.directory, 'run.log', {'time': NOW, 'event': 'fresh'})
            appended.set()

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(logs.os, 'replace', side_effect=pause_replace):
            cleaning = pool.submit(self.store.cleanup, 'expired')
            self.assertTrue(replacing.wait(5))
            writing = pool.submit(writer)
            try:
                self.assertTrue(started.wait(5))
                self.assertFalse(appended.wait(0.05))
            finally:
                release.set()
            self.assertEqual(cleaning.result(timeout=5)['removed'], 1)
            writing.result(timeout=5)
        self.assertEqual(self.store.read()['items'][0]['event'], 'fresh')

    def test_process_append_uses_shared_os_lock(self):
        script = ('import sys; import seedkeep_logstore as logs; '
                  '[(logs.append(sys.argv[1], "management.log", '
                  '{"time": 1, "event": "progress", "processed": int(sys.argv[2])*100+i})) '
                  'for i in range(25)]')
        processes = [subprocess.Popen([sys.executable, '-B', '-c', script, str(self.directory), str(index)],
                                      cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for index in range(3)]
        for process in processes:
            self.addCleanup(lambda process=process: process.kill() if process.poll() is None else None)
        for process in processes:
            _, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error.decode('utf-8', errors='replace'))
        result = self.store.read(limit=1000)
        self.assertEqual(result['total'], 75)
        self.assertEqual(len({row['processed'] for row in result['items']}), 75)

    def test_symlink_log_and_rotated_log_are_rejected_before_cleanup(self):
        victim = self.write('outside.txt', b'synthetic protected data')
        self.write('run.log', line(time=1, event='old'))
        self.symlink(victim, 'management.log.1')
        before = (self.directory / 'run.log').read_bytes()
        for action in (self.store.read, lambda: self.store.cleanup('all'),
                       lambda: logs.append(self.directory, 'management.log.1', {'event': 'ok'})):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(victim.read_bytes(), b'synthetic protected data')
        self.assertEqual((self.directory / 'run.log').read_bytes(), before)

    def test_symlink_policy_meta_lock_and_directory_are_rejected(self):
        victim = self.write('outside.txt', b'{}')
        for name, action in (('log_policy.json', self.store.policy),
                             ('log_cleanup_meta.json', lambda: self.store.cleanup('all')),
                             ('.logstore.lock', self.store.read)):
            with self.subTest(name=name):
                target = self.directory / name
                if target.exists():
                    target.unlink()
                self.symlink(victim, name)
                with self.assertRaises(ValueError):
                    action()
                target.unlink()
                self.assertEqual(victim.read_bytes(), b'{}')
        actual = self.directory / 'actual'
        actual.mkdir()
        linked = self.symlink(actual, 'linked', directory=True)
        with self.assertRaises(ValueError):
            logs.Store(linked / 'child').policy()
        self.assertFalse((actual / 'child').exists())

    def test_hardlink_is_rejected_without_modifying_original(self):
        victim = self.write('outside.txt', b'synthetic protected data')
        os.link(victim, self.directory / 'run.log')
        with self.assertRaises(ValueError):
            self.store.cleanup('all')
        with self.assertRaises(ValueError):
            logs.append(self.directory, 'run.log', {'event': 'ok'})
        self.assertEqual(victim.read_bytes(), b'synthetic protected data')

    def test_file_replaced_between_open_checks_is_rejected(self):
        path = self.write('run.log', line(time=NOW, event='original'))
        original_open = logs.os.open

        def changed_open(name, flags, mode=0o777, **kwargs):
            fd = original_open(name, flags, mode, **kwargs)
            if Path(name).name == 'run.log':
                path.rename(self.directory / 'original.log')
                path.write_bytes(line(time=NOW, event='replacement'))
            return fd

        # Windows denies renaming open files; the identity check itself is tested
        # with an altered stat result there, and real inode replacement on POSIX.
        if os.name == 'nt':
            original_check = self.store._check
            calls = 0

            def changed_check(name):
                nonlocal calls
                info = original_check(name)
                if name == 'run.log':
                    calls += 1
                    if calls == 3:
                        fields = list(info)
                        fields[1] += 1
                        return os.stat_result(fields)
                return info

            with patch.object(self.store, '_check', side_effect=changed_check), self.assertRaises(ValueError):
                self.store.read()
        else:
            with patch.object(logs.os, 'open', side_effect=changed_open), self.assertRaises(ValueError):
                self.store.read()

    def test_append_rotates_both_active_logs_and_keeps_new_event_readable(self):
        for name, maximum in (('management.log', 512 * 1024), ('run.log', 5 * 1024 * 1024)):
            with self.subTest(name=name):
                original = b'x' * maximum
                self.write(name, original)
                logs.append(self.directory, name, {'time': NOW, 'event': 'after_rotation'})
                self.assertEqual((self.directory / (name + '.1')).read_bytes(), original)
                self.assertEqual(json.loads((self.directory / name).read_text())['event'], 'after_rotation')
        self.assertEqual(self.store.read()['total'], 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits are not implemented by Windows chmod')
    def test_private_permissions_on_logs_policy_meta_and_lock(self):
        for name in logs.LOG_NAMES:
            path = self.write(name, line(time=1, event='old'))
            path.chmod(0o666)
        self.auto_policy()
        self.store.cleanup('expired')
        logs.append(self.directory, 'management.log', {'event': 'ok'})
        for name in (*logs.LOG_NAMES, 'log_policy.json', 'log_cleanup_meta.json', '.logstore.lock'):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
