"""End-to-end cleanup jobs against native-shaped virtual qB/TR and temporary content."""
import contextlib
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import seedkeep_cleanup as cleanup
import seedkeep_downloaders as downloads
import seedkeep_fleet as fleet_module
import seedkeep_management as management
import seedkeep_pull as pull
from seedkeep_storage import Storage
from test_seedkeep_cleanup import rule
from test_seedkeep_instances import instance
from test_seedkeep_management import SOURCE, NOW, qb_task, tr_task
from test_seedkeep_pull import torrent
from torrent_meta import torrent_metadata


class Content:
    def __init__(self, root, kind):
        self.root, self.kind = root, kind
        self.tasks, self.files, self.meta, self.tracks = {}, {}, {}, {}
        self.calls, self.reads = [], 0
        self.fail_read = self.fail_files = self.residual = self.lost_reply = self.reject_delete = self.delay_delete = False
        self.before_delete = None

    def add(self, index):
        name, size = 'file%d.bin' % index, 100 + index
        data = torrent(size, name.encode())
        value = torrent_metadata(data)['hashes'][0]
        path = self.root / name
        path.write_bytes(b'x' * size)
        self.files[value], self.meta[value] = [{'name': name, 'size': size}], data
        self.tasks[value] = (qb_task(value, size=size, seeders=11, category='PTS', tags='', save_path='/media/q')
                             if self.kind == 'qb' else tr_task(value, size=size, seeders=11, labels=['PTS'], downloadDir='/media/t', id=index))
        # The virtual tracker supplies a verifiable receipt clock. Ordinary qB
        # Web API snapshots omit it and must remain unknown (tested below).
        self.tracks[value] = [{'url': 'https://tracker.ptskit.org/announce?passkey=test-only',
                              'status': 2, 'num_seeds': 11, 'msg': '', '_verified_receipt_at': NOW}]
        return value

    def read(self):
        self.reads += 1
        if self.fail_read:
            raise RuntimeError('private connection detail')
        return copy.deepcopy(list(self.tasks.values()))

    def remove(self, value, delete_data):
        if self.before_delete:
            self.before_delete(value)
        if self.reject_delete:
            raise RuntimeError('permission denied with private path')
        if self.delay_delete:
            return
        if delete_data and not self.residual:
            for item in self.files[value]:
                path = self.root / item['name']
                if path.parent != self.root:
                    raise AssertionError('test content escaped temporary directory')
                path.unlink()
        self.tasks.pop(value, None)
        if self.lost_reply:
            raise RuntimeError('lost receipt')


class NativeAPI(downloads.API):
    def __init__(self, source, settings, endpoints):
        super().__init__(source, settings)
        self.q = endpoints.get(self.source.get('qb_url'))
        self.t = endpoints.get(self.settings.get('tr_url'))

    def qget(self, operation, fields=None, *, binary=False):
        if operation == 'torrents/info':
            return self.q.read()
        value = fields['hash']
        if operation == 'torrents/files':
            if self.q.fail_files:
                raise RuntimeError('private file read failure')
            return copy.deepcopy(self.q.files[value])
        if operation == 'torrents/trackers':
            return copy.deepcopy(self.q.tracks[value])
        if operation == 'torrents/export':
            return self.q.meta[value]
        raise AssertionError(operation)

    def qpost(self, operation, fields):
        self.q.calls.append((operation, copy.deepcopy(fields)))
        value = fields['hashes']
        if not downloads.HASH.fullmatch(value):
            raise AssertionError('delete/pause request must target one exact hash')
        if operation == 'torrents/stop':
            self.q.tasks[value]['state'] = 'stoppedUP'
        elif operation == 'torrents/start':
            self.q.tasks[value]['state'] = 'uploading'
        elif operation == 'torrents/setForceStart':
            self.q.tasks[value].update(force_start=fields['value'] == 'true', state='forcedUP' if fields['value'] == 'true' else 'uploading')
        elif operation == 'torrents/delete':
            if fields['deleteFiles'] not in ('true', 'false'):
                raise AssertionError('invalid native delete flag')
            self.q.remove(value, fields['deleteFiles'] == 'true')
        else:
            raise AssertionError(operation)
        return b''

    def rpc(self, method, arguments=None):
        arguments = arguments or {}
        if method == 'torrent-get':
            rows = self.t.read()
            if 'ids' in arguments:
                rows = [r for r in rows if r['hashString'] in arguments['ids']]
            if 'files' in arguments.get('fields', []):
                if self.t.fail_files:
                    raise RuntimeError('private file read failure')
                for row in rows:
                    row['files'] = [{'name': f['name'], 'length': f['size']} for f in self.t.files[row['hashString']]]
            return {'torrents': rows}
        self.t.calls.append((method, copy.deepcopy(arguments)))
        if len(arguments.get('ids', [])) != 1 or not downloads.HASH.fullmatch(arguments['ids'][0]):
            raise AssertionError('TR request must target one exact hash')
        value = arguments['ids'][0]
        if method == 'torrent-stop':
            self.t.tasks[value]['status'] = 0
        elif method == 'torrent-start':
            self.t.tasks[value]['status'] = 6
        elif method == 'torrent-remove':
            if type(arguments.get('delete-local-data')) is not bool:
                raise AssertionError('invalid native delete flag')
            self.t.remove(value, arguments['delete-local-data'])
        else:
            raise AssertionError(method)
        return {}


class CleanupRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.inspect_root = self.directory / 'inspect'
        self.inspect_root.mkdir()
        for part in ('q', 't'):
            (self.inspect_root / part).mkdir()
        self.q, self.t = Content(self.inspect_root / 'q', 'qb'), Content(self.inspect_root / 't', 'tr')
        self.hashes = [self.q.add(n) for n in range(1, 4)]
        self.now = NOW
        self.source = {**SOURCE, 'qb_url': 'http://q1.invalid'}
        self.settings = {'target': 1, 'tracker_fresh_seconds': 7200, 'pause_timeout_seconds': 120,
                         'downloaders': [instance('q1', default=True), instance('t1', 'tr')]}
        self.endpoints = {r['url']: data for r, data in zip(self.settings['downloaders'], (self.q, self.t))}
        self.factory = lambda source, settings: NativeAPI(source, settings, self.endpoints)
        self.site = {'available': True, 'stale': False, 'seeders_max': 10}
        self.legacy = management.Manager(self.directory, lambda: self.source, lambda: self.settings,
                                        lambda: self.site, contextlib.nullcontext, client_factory=self.factory, clock=lambda: self.now)
        self.storage = Storage(self.directory, allowed_roots=[str(self.inspect_root)], require_readonly=False)
        self.storage.update({'revision': self.storage.public()['revision'], 'values': {'mappings': [
            {'instance_id': iid, 'download_root': '/media/' + part, 'inspect_root': str(self.inspect_root / part), 'protected_paths': []}
            for iid, part in (('q1', 'q'), ('t1', 't'))]}})
        self.fleet = self.make_fleet()
        self.c = self.fleet.cleanup
        self.values = {'rules': [rule(target=1, start_margin=2, stop_margin=1, seeders_mode='manual', seeders_wait_hours=1, unregistered_wait_hours=1)]}

    def make_fleet(self, guard=contextlib.nullcontext):
        return fleet_module.Fleet(self.directory, lambda: self.source, lambda: self.settings, lambda: self.site,
                                  guard, self.legacy, api_factory=self.factory, clock=lambda: self.now, cleanup_storage=self.storage)

    def save(self):
        return self.c.update({'revision': self.c.public()['revision'], 'values': self.values})

    def advance(self, seconds=300):
        self.now += seconds
        for tracks in self.q.tracks.values():
            for track in tracks:
                if '_verified_receipt_at' in track:
                    track['_verified_receipt_at'] = self.now
        for task in self.t.tasks.values():
            for track in task['trackerStats']:
                track['lastAnnounceTime'] = self.now

    def observe(self):
        self.save()
        for n in range(13):
            if n:
                self.advance()
            self.c.tick()
        self.assertEqual(self.c.summary()['rules'][0]['mature'], len(self.q.tasks) + len(self.t.tasks))

    def run_cleanup(self):
        return self.c.run({'revision': self.c.public()['revision'], 'rule_id': 'r1', 'confirm': 'RUN_DELETE_TASKS_AND_DATA'})

    def drain(self):
        for _ in range(30):
            if not self.c.busy():
                break
            self.c.step()
        self.assertFalse(self.c.busy())

    def deletes(self):
        return [c for data in (self.q, self.t) for c in data.calls if c[0] in ('torrents/delete', 'torrent-remove')]

    def test_default_read_and_preview_never_create_cleanup_runtime_files(self):
        self.assertFalse(self.c.path.exists())
        self.assertFalse(self.c.state_path.exists())
        self.c.public()
        self.c.preview({'values': self.values})
        self.assertFalse(self.c.path.exists())
        self.assertFalse(self.c.state_path.exists())
        self.assertEqual(self.deletes(), [])

    def test_save_and_observe_never_pause_or_delete(self):
        self.observe()
        self.assertTrue(all((self.q.root / item['name']).exists() for rows in self.q.files.values() for item in rows))
        self.assertEqual(self.q.calls + self.t.calls, [])
        self.assertEqual(self.c.summary()['rules'][0]['inventory'], 3)

    def test_qb_native_data_delete_intent_backup_exclusion_then_successful_readback(self):
        self.observe()
        result = self.run_cleanup()
        self.assertEqual(len(result['job']['items']), 1)
        value = result['job']['items'][0]['hash']
        def check_intent(h):
            state = json.loads(self.c.state_path.read_text())
            item = next(i for i in state['job']['items'] if i['hash'] == h)
            self.assertEqual(item['phase'], 'waiting_result')
            self.assertIsNotNone(item['intent_at'])
            self.assertTrue(item['snapshot']['ok'])
            self.assertIn(h, json.loads((self.directory / 'batch.json').read_text())['seen_hashes'])
            backup = self.directory / 'operations' / state['job']['id'] / ('q1-' + h + '.torrent')
            self.assertEqual(torrent_metadata(backup.read_bytes())['hashes'][0], h)
        self.q.before_delete = check_intent
        self.drain()
        self.assertEqual(self.deletes(), [('torrents/delete', {'hashes': value, 'deleteFiles': 'true'})])
        self.assertNotIn(value, self.q.tasks)
        self.assertFalse((self.q.root / self.q.files[value][0]['name']).exists())
        self.assertEqual(self.c.public_job()['completed'], 1)
        self.assertEqual(self.c.summary()['rules'][0]['inventory'], 2)
        self.assertFalse(self.c.summary()['rules'][0]['active'])
        self.assertEqual(self.fleet.state['automation_turn'], 'refill')

    def test_transmission_uses_exact_hash_and_delete_local_data(self):
        self.q.tasks = {}
        self.hashes = [self.t.add(n) for n in range(1, 4)]
        self.values['rules'][0]['scopes'][0]['instance_id'] = 't1'
        self.observe()
        value = self.run_cleanup()['job']['items'][0]['hash']
        self.drain()
        self.assertEqual(self.deletes(), [('torrent-remove', {'ids': [value], 'delete-local-data': True})])
        self.assertEqual(self.c.public_job()['completed'], 1)
        self.assertFalse((self.t.root / self.t.files[value][0]['name']).exists())

    def test_lost_delete_receipt_is_reconciled_without_duplicate_request(self):
        self.observe()
        self.q.lost_reply = True
        self.run_cleanup()
        self.drain()
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(self.c.public_job()['status'], 'completed')

    def test_residual_file_marks_review_not_reclaimed_and_disables_automatic(self):
        self.observe()
        self.q.residual = True
        self.c.mode({'revision': self.c.public()['revision'], 'rule_id': 'r1', 'action': 'enable', 'confirm': 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'})
        value = self.run_cleanup()['job']['items'][0]['hash']
        self.drain()
        self.assertEqual(self.c.public_job()['status'], 'needs_review')
        self.assertEqual(self.c.public_job()['completed'], 0)
        self.assertEqual(self.c.public_job()['logical_bytes'], 0)
        self.assertTrue((self.q.root / self.q.files[value][0]['name']).exists())
        self.assertFalse(self.c.values['rules'][0]['enabled'])
        self.assertEqual(len(self.deletes()), 1)
        with self.assertRaises(downloads.ManagementError):
            self.run_cleanup()

    def test_delete_permission_failure_never_reissues_after_restart(self):
        self.observe()
        self.q.reject_delete = True
        self.run_cleanup()
        for _ in range(3):
            self.c.step()
        self.assertEqual(self.c.public_job()['items'][0]['phase'], 'waiting_result')
        self.fleet = self.make_fleet()
        self.c = self.fleet.cleanup
        self.advance(121)
        self.drain()
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(self.c.public_job()['status'], 'needs_review')
        self.assertEqual(len(self.q.tasks), 3)

    def test_restart_after_pausing_and_cancel_restores_force_mode(self):
        self.observe()
        for task in self.q.tasks.values():
            task.update(state='forcedUP', force_start=True)
        value = self.run_cleanup()['job']['items'][0]['hash']
        self.c.step()
        self.assertEqual(self.q.tasks[value]['state'], 'stoppedUP')
        self.fleet = self.make_fleet()
        self.c = self.fleet.cleanup
        self.c.cancel({'job_id': self.c.public_job()['id']})
        self.drain()
        self.assertEqual(self.c.public_job()['status'], 'cancelled')
        self.assertEqual(self.q.tasks[value]['state'], 'forcedUP')
        self.assertEqual(self.deletes(), [])

    def test_scope_change_after_pause_restores_and_keeps_content(self):
        self.observe()
        value = self.run_cleanup()['job']['items'][0]['hash']
        self.c.step()
        self.q.tasks[value]['category'] = 'protected'
        self.drain()
        self.assertEqual(self.q.tasks[value]['state'], 'uploading')
        self.assertTrue((self.q.root / self.q.files[value][0]['name']).exists())
        self.assertEqual(self.deletes(), [])

    def test_unselected_disabled_instance_read_failure_makes_inventory_unknown(self):
        self.observe()
        self.settings['downloaders'][1]['enabled'] = False
        self.t.fail_read = True
        self.advance()
        self.c.tick()
        report = self.c.summary()['rules'][0]
        self.assertIsNone(report['inventory'])
        self.assertEqual(report['mature'], 0)
        self.assertEqual(self.c.state['rules']['r1']['observations'], {})
        self.assertEqual(self.deletes(), [])

    def test_external_run_lock_is_checked_before_any_inventory_network_read(self):
        self.save()
        @contextlib.contextmanager
        def busy():
            raise downloads.ManagementError('busy', 409)
            yield
        self.fleet = self.make_fleet(guard=busy)
        self.c = self.fleet.cleanup
        before = self.q.reads + self.t.reads
        with self.assertRaises(downloads.ManagementError):
            self.c.preview({'values': self.values})
        self.assertEqual(self.q.reads + self.t.reads, before)
        self.assertEqual(self.deletes(), [])

    def test_shared_physical_path_through_alias_mapping_protects_entire_task(self):
        self.observe()
        other = self.t.add(50)
        candidate = self.hashes[0]
        self.t.files[other] = copy.deepcopy(self.q.files[candidate])
        self.t.tasks[other]['totalSize'] = self.t.files[other][0]['size']
        mapping = self.storage.public()
        mapping['values']['mappings'][1]['inspect_root'] = str(self.q.root)
        self.storage.update({'revision': mapping['revision'], 'values': mapping['values']})
        preview = self.c.preview({'values': self.values})
        entry = next(i for i in preview['items'] if i['hash'] == candidate)
        self.assertEqual(entry['status'], 'shared')
        self.assertEqual(self.deletes(), [])

    def test_enable_requires_saved_revision_confirmation_and_full_path_coverage(self):
        self.save()
        body = {'revision': self.c.public()['revision'], 'rule_id': 'r1', 'action': 'enable'}
        with self.assertRaises(downloads.ManagementError):
            self.c.mode(body)
        body['confirm'] = 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'
        self.q.tasks[self.hashes[0]]['save_path'] = '/unmapped'
        with self.assertRaises(downloads.ManagementError):
            self.c.mode(body)
        self.assertFalse(self.c.values['rules'][0]['enabled'])
        self.assertEqual(self.deletes(), [])

    def test_stale_storage_revision_does_not_pause_saved_rule_or_reset_evidence(self):
        self.observe()
        old = copy.deepcopy(self.c.state)
        with self.assertRaises(downloads.ManagementError):
            self.c.update_storage({'revision': '0' * 64, 'values': self.storage.public()['values']})
        self.assertEqual(self.c.state, old)
        self.assertTrue(self.c.values['rules'][0]['observe_only'])

    def test_both_task_only_native_apis_keep_existing_files(self):
        qvalue, tvalue = self.hashes[0], self.t.add(30)
        self.fleet.registry.api('q1').qremove(qvalue)
        self.fleet.registry.api('t1').rpc('torrent-remove', {'ids': [tvalue], 'delete-local-data': False})
        self.assertTrue((self.q.root / self.q.files[qvalue][0]['name']).exists())
        self.assertTrue((self.t.root / self.t.files[tvalue][0]['name']).exists())
        self.assertEqual(self.deletes(), [('torrents/delete', {'hashes': qvalue, 'deleteFiles': 'false'}),
                                         ('torrent-remove', {'ids': [tvalue], 'delete-local-data': False})])

    def test_each_rule_keeps_its_period_and_task_annotations(self):
        other = self.t.add(30)
        self.values['rules'].append(rule(id='r2', check_minutes=10,
            scopes=[{'instance_id': 't1', 'values': ['PTS'], 'include_empty': False}]))
        self.save()
        self.c.tick()
        original_second = copy.deepcopy(self.c.state['rules']['r2'])
        self.assertIn('q1:' + self.hashes[0], self.c.annotations)
        self.assertIn('t1:' + other, self.c.annotations)
        self.advance(300)
        self.c.tick()
        self.assertEqual(self.c.state['rules']['r2'], original_second)
        self.assertIn('t1:' + other, self.c.annotations)

    def test_mature_draft_preview_preserves_disk_state_and_observations(self):
        self.observe()
        state, annotations = copy.deepcopy(self.c.state), copy.deepcopy(self.c.annotations)
        disk = self.c.state_path.read_bytes()
        draft = copy.deepcopy(self.values)
        draft['rules'][0]['seeders_max'] = 12
        self.c.preview({'values': draft})
        self.assertEqual(self.c.state, state)
        self.assertEqual(self.c.annotations, annotations)
        self.assertEqual(self.c.state_path.read_bytes(), disk)
        self.assertEqual(self.deletes(), [])

    def test_refill_turn_defers_new_cleanup_then_cleanup_gets_the_next_turn(self):
        self.observe()
        self.c.mode({'revision': self.c.public()['revision'], 'rule_id': 'r1', 'action': 'enable',
                     'confirm': 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'})
        self.fleet.refill_due = lambda: True
        self.fleet.state['automation_turn'] = 'refill'
        self.advance()
        self.fleet._tick()
        self.assertIsNone(self.c.public_job())
        self.assertEqual(self.deletes(), [])
        self.fleet.state['automation_turn'] = 'cleanup'
        self.advance()
        self.fleet._tick()
        self.assertTrue(self.c.busy())
        self.drain()
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(self.fleet.state['automation_turn'], 'refill')

    def test_qb_without_verifiable_receipt_never_matures_or_deletes(self):
        for tracks in self.q.tracks.values():
            tracks[0].pop('_verified_receipt_at')
        self.save()
        for _ in range(300):
            self.c.tick()
            self.advance()
        self.assertEqual(self.c.summary()['rules'][0]['mature'], 0)
        self.assertEqual(self.c.summary()['rules'][0]['unknown'], 3)
        self.assertEqual(self.run_cleanup()['job'], None)
        self.assertEqual(self.deletes(), [])

    def test_non_reentrant_guard_scheduled_turn_gets_lock_once(self):
        self.observe()
        entered = []
        @contextlib.contextmanager
        def guard():
            if entered:
                raise downloads.ManagementError('native lock is not reentrant', 409)
            entered.append(True)
            try:
                yield
            finally:
                entered.pop()
        self.fleet = self.make_fleet(guard=guard)
        self.c = self.fleet.cleanup
        self.c.mode({'revision': self.c.public()['revision'], 'rule_id': 'r1', 'action': 'enable',
                     'confirm': 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'})
        self.advance()
        self.assertTrue(self.c.tick())
        self.drain()
        self.assertEqual(len(self.deletes()), 1)

    @unittest.skipUnless(os.name == 'posix', 'actual Controller flock requires Linux')
    def test_real_controller_lock_auto_cleanup_and_external_contention(self):
        import fcntl
        import seedkeep_web as web
        self.observe()
        controller = web.Controller.__new__(web.Controller)
        controller.directory, controller.lock, controller.process = self.directory, threading.RLock(), None
        controller._file_lock_local = threading.local()
        self.fleet = self.make_fleet(guard=controller.operation_guard)
        self.c = self.fleet.cleanup
        self.c.mode({'revision': self.c.public()['revision'], 'rule_id': 'r1', 'action': 'enable',
                     'confirm': 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'})
        self.advance()
        before = self.q.reads + self.t.reads
        with (self.directory / 'run.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(web.ApiError):
                self.c.tick()
        self.assertEqual(self.q.reads + self.t.reads, before)
        self.assertTrue(self.c.tick())
        self.drain()
        self.assertEqual(len(self.deletes()), 1)

    def test_delayed_delete_review_then_restart_readback_never_resends(self):
        self.observe()
        self.q.delay_delete = True
        job = self.run_cleanup()['job']
        value = job['items'][0]['hash']
        for _ in range(4):
            self.c.step()
        self.assertTrue(self.c.busy())
        self.advance(121)
        self.drain()
        self.assertEqual(self.c.public_job()['status'], 'needs_review')
        self.assertEqual(len(self.deletes()), 1)
        self.c.recheck({'job_id': job['id']})
        self.assertEqual(self.c.public_job()['status'], 'needs_review')
        self.q.delay_delete = False
        self.q.remove(value, True)  # Simulate eventual completion, no second native request.
        calls = copy.deepcopy(self.q.calls)
        self.fleet = self.make_fleet()
        self.c = self.fleet.cleanup
        self.c.tick()
        self.assertEqual(self.c.public_job()['status'], 'completed')
        self.assertEqual(self.c.public_job()['completed'], 1)
        self.assertEqual(self.q.calls, calls)
        self.assertFalse(self.c.values['rules'][0]['enabled'])
        self.assertEqual(self.c.summary()['rules'][0]['inventory'], 2)

    def test_residual_content_review_explicit_recheck_is_read_only(self):
        self.observe()
        self.q.residual = True
        job = self.run_cleanup()['job']
        self.drain()
        self.assertEqual(self.c.public_job()['status'], 'needs_review')
        calls = copy.deepcopy(self.q.calls)
        for item in self.q.files[job['items'][0]['hash']]:
            (self.q.root / item['name']).unlink()
        self.c.recheck({'job_id': job['id']})
        self.assertEqual(self.c.public_job()['status'], 'completed')
        self.assertEqual(self.q.calls, calls)
        self.assertFalse(self.c.values['rules'][0]['enabled'])



if __name__ == '__main__':
    unittest.main()
