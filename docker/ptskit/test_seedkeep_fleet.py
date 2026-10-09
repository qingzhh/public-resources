"""Virtual-only fleet operations, recovery, observations and native refill regressions."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse
import seedkeep_downloaders as downloads
import seedkeep_fleet as fleet_module
import seedkeep_management as management
import seedkeep_pull as pull
from test_seedkeep_instances import instance
from test_seedkeep_management import FakeAPI, NOW, SOURCE, qb_task, tr_task
from test_seedkeep_pull import torrent
from torrent_meta import torrent_metadata


class PairAPI(downloads.API):
    def __init__(self, source, settings, endpoints):
        super().__init__(source, settings)
        self.qfake = endpoints.get(self.source.get('qb_url'))
        self.tfake = endpoints.get(self.settings.get('tr_url'))

    def qget(self, operation, fields=None, *, binary=False):
        if not self.qfake or 'qb' in self.qfake.errors:
            raise RuntimeError('q-secret https://private.invalid/token=secret')
        if '?' in operation:
            operation, query = operation.split('?', 1)
            fields = {key: values[0] for key, values in urllib.parse.parse_qs(query).items()}
        if operation.startswith('rss/'):
            self.qfake.calls.append((operation, None))
            return {}
        return self.qfake.qget(operation, fields, binary=binary)

    def qpost(self, operation, fields):
        if not self.qfake or 'qb' in self.qfake.errors:
            raise RuntimeError('q-secret')
        if operation.startswith('torrents/'):
            self.qfake.calls.append((operation, copy.deepcopy(fields)))
            self.qfake.fault(operation)
            for value in fields['hashes'].split('|'):
                if operation == 'torrents/start':
                    self.qfake.qstart(value)
                elif operation == 'torrents/stop':
                    self.qfake.qstop(value)
                elif operation == 'torrents/recheck':
                    self.qfake.qb[value]['state'] = 'checkingUP'
                else:
                    raise AssertionError(operation)
            return b''
        return self.qfake.qpost(operation, fields)

    def rpc(self, method, arguments=None):
        if not self.tfake or 'tr' in self.tfake.errors:
            raise RuntimeError('tr-secret https://private.invalid')
        return self.tfake.rpc(method, arguments)

    def qtrackers(self, value):
        if hasattr(self.qfake, 'tracks'):
            self.qfake.fault('qtrackers')
            return copy.deepcopy(self.qfake.tracks)
        return self.qfake.qtrackers(value)

    def qexport(self, value):
        return self.qfake.qexport(value)

    def qstop(self, value):
        return self.qfake.qstop(value)

    def qstart(self, value):
        return self.qfake.qstart(value)

    def qremove(self, value):
        return self.qfake.qremove(value)


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = NOW
        self.source = {**SOURCE, 'qb_url': 'http://q1.invalid', 'token': 'site-secret'}
        self.settings = {'tr_url': 'http://t1.invalid/transmission/rpc',
                         'connections': {'tr_username': 'login-user', 'tr_password': 'login-secret'},
                         'downloaders': [instance('q1', default=True), instance('q2'), instance('t1', 'tr'), instance('t2', 'tr')]}
        self.data = torrent(100)
        self.endpoints = {row['url']: FakeAPI(self.data) for row in self.settings['downloaders']}
        self.q1, self.q2, self.t1, self.t2 = [self.endpoints[row['url']] for row in self.settings['downloaders']]
        self.hash = self.q1.hash
        self.t1.qb = self.t2.qb = {}
        self.t1.tr[self.hash] = tr_task(self.hash)
        self.site = {'available': True, 'stale': False, 'seeders_max': 10}
        self.batch = {'accepted': {'1': {'hash': self.hash, 'hashes': [self.hash], 'size': 100}},
                      'pending': {}, 'unconfirmed': {}, 'baseline': {'qb': {}, 'tr_hashes': [], 'historical_hashes': []},
                      'seen_hashes': ['b' * 40], 'criteria': {'target': 1, 'max_bytes': 500, 'min_seeders': 2, 'max_seeders': 6}}
        pull.save_json(self.directory / 'batch.json', self.batch)
        self.factory = lambda source, settings: PairAPI(source, settings, self.endpoints)
        self.legacy = management.Manager(self.directory, lambda: self.source, lambda: self.settings,
                                         lambda: self.site, contextlib.nullcontext, client_factory=self.factory,
                                         clock=lambda: self.now, transfer_mode='full')  # Legacy full-check fixtures.
        self.fleet = self.make_fleet()

    def make_fleet(self, guard=contextlib.nullcontext):
        return fleet_module.Fleet(self.directory, lambda: self.source, lambda: self.settings, lambda: self.site,
                                  guard, self.legacy, api_factory=self.factory, clock=lambda: self.now)

    def task(self, iid='q2', value=None):
        return {'instance_id': iid, 'hash': value or self.hash}

    def action(self, action='remove', tasks=None):
        return self.fleet.action({'action': action, 'tasks': tasks or [self.task()], 'confirm': 'REMOVE_TASKS_KEEP_DATA'})

    def row(self, iid='q2'):
        return next(row for row in self.fleet.snapshot(True)['items'] if row['instance_id'] == iid and row['hash'] == self.hash)

    def advance(self, seconds=60):
        self.now += seconds
        for api in (self.t1, self.t2):
            for task in api.tr.values():
                for tracker in task.get('trackerStats', []):
                    tracker['lastAnnounceTime'] = self.now

    def wait_ready(self, field='delete_ready'):
        self.row()
        for _ in range(60):
            self.advance()
            current = self.row()
        self.assertTrue(current[field])

    def unregistered(self, iid='q2'):
        api = self.q2 if iid == 'q2' else self.q1
        api.tracks = [{'url': 'https://tracker.ptskit.org/announce?passkey=fake-private', 'status': 4,
                       'num_seeds': -1, 'msg': 'Unregistered torrent'}]
        self.fleet.update_policy({**self.fleet.policy(), 'unregistered_enabled': True, 'unregistered_wait_hours': 1})

    def transfer(self, target='t2', source='q2'):
        return self.fleet.transfer({'tasks': [self.task(source)], 'target_instance_id': target,
                                    'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})

    def complete_verify(self, target=None):
        target = target or self.t2
        target.tr[self.hash].update(status=0, percentDone=1, leftUntilDone=0, haveValid=100, haveUnchecked=0)
        self.advance(11)

    def test_three_instances_same_hash_independent_rows_unique_managed(self):
        value = self.fleet.snapshot(True)
        self.assertEqual(len(value['items']), 3)
        self.assertEqual({row['id'] for row in value['items']}, {'q1:' + self.hash, 'q2:' + self.hash, 't1:' + self.hash})
        self.assertEqual(value['totals']['managed_active'], 1)
        self.assertEqual(value['totals']['unique_total'], 1)
        self.assertEqual(value['totals']['total'], 3)
        self.assertEqual(value['totals']['valid'], 3)

    def test_completed_qb_rows_include_queued_and_waiting_upload(self):
        self.q1.qb[self.hash]['state'] = 'queuedUP'
        self.q2.qb[self.hash]['state'] = 'stalledUP'
        value = self.fleet.snapshot(True)
        queued = next(row for row in value['items'] if row['instance_id'] == 'q1')
        waiting = next(row for row in value['items'] if row['instance_id'] == 'q2')
        self.assertTrue(queued['completed'])
        self.assertTrue(waiting['completed'])
        self.assertEqual(queued['state_text'], '排队做种')
        self.assertEqual(waiting['state_text'], '做种中（等待上传）')
        self.assertIn('completed', queued['state_groups'])
        self.assertNotIn('downloading', queued['state_groups'])
        self.assertIn('seeding', queued['state_groups'])
        self.assertEqual((queued['validity'], waiting['validity']), ('inactive', 'valid'))
        self.assertEqual(value['totals']['qb_completed'], 2)

    def test_seeder_boundary_is_unconfirmed_and_never_matures_for_deletion(self):
        self.q1.qb.clear()
        self.q2.qb.clear()
        self.t1.tr = {letter * 40: tr_task(letter * 40, seeders=seeds)
                      for letter, seeds in (('a', 9), ('b', 10), ('c', 11))}
        result = self.fleet.snapshot(True)
        summary = result['seedkeep']
        self.assertEqual((summary['total'], summary['valid'], summary['unknown'], summary['invalid']), (3, 1, 1, 1))
        boundary = next(row for row in result['items'] if row['hash'] == 'b' * 40)
        self.assertEqual(boundary['validity'], 'unknown')
        self.assertIn('边界', boundary['reason'])
        self.advance(3600)
        boundary = next(row for row in self.fleet.snapshot(True)['items'] if row['hash'] == 'b' * 40)
        self.assertFalse(boundary['delete_ready'])
        self.assertIsNone(boundary['invalid_since'])
        self.assertNotIn('invalid', self.fleet.state['observations'].get(boundary['id'], {}))
        self.assertEqual(downloads.classify(True, 10, [10], False, True)['validity'], 'downloading')
        self.assertEqual(downloads.classify(True, 10, [10], True, False)['validity'], 'inactive')
        self.assertEqual(downloads.classify(True, 10, [11], True, True)['validity'], 'invalid')
        self.settings.update(seeders_limit_mode='manual', manual_seeders_max=10)
        boundary = next(row for row in self.fleet.snapshot(True)['items'] if row['hash'] == 'b' * 40)
        self.assertEqual(boundary['validity'], 'valid')
        self.assertFalse(boundary['delete_ready'])

    def test_completed_filter_is_independent_of_pause_error_and_transfer_eligibility(self):
        for state, progress, label, group, complete in (
                ('stoppedUP', 1, '已暂停（已完成）', 'paused', True),
                ('pausedUP', 1, '已暂停（已完成）', 'paused', True),
                ('queuedDL', .5, '排队下载', 'queued', False),
                ('checkingUP', 1, '校验中（已完成）', 'checking', True),
                ('missingFiles', 1, '文件缺失', 'error', False),
                ('metaDL', 0, '获取种子元数据', 'downloading', False),
                ('unknown', .5, '状态未知', 'unknown', False)):
            with self.subTest(state=state):
                self.q2.qb[self.hash].update(state=state, progress=progress)
                row = self.row()
                self.assertEqual(row['completed'], complete)
                self.assertEqual(row['state_text'], label)
                if state == 'checkingUP':
                    self.assertIn('seeding', row['state_groups'])
                self.assertIn(group, row['state_groups'])
                self.assertEqual('completed' in row['state_groups'], complete)
                if state in ('missingFiles', 'checkingUP', 'unknown'):
                    self.assertFalse(row['can_transfer'])
        # qB's completed category uses upload-side states, not the recheck progress or old byte count.
        self.q2.qb[self.hash].update(state='checkingUP', progress=.5)
        self.assertTrue(self.row()['completed'])
        for state in ('checkingDL', 'downloading', 'error', 'moving', 'futureState'):
            with self.subTest(state=state):
                self.q2.qb[self.hash].update(state=state, progress=1)
                row = self.row()
                self.assertFalse(row['completed'])
                self.assertFalse(row['can_transfer'])

    def test_tr_numeric_zero_state_has_chinese_label_and_separate_completion(self):
        for state, progress, label, group in ((0, 1, '已暂停（已完成）', 'paused'),
                (2, 1, '校验中（已完成）', 'checking'), (3, .5, '排队下载', 'queued'),
                (4, .5, '下载中', 'downloading'), (5, 1, '排队做种', 'queued'),
                (6, 1, '做种中', 'seeding')):
            with self.subTest(state=state):
                self.t1.tr[self.hash].update(status=state, percentDone=progress)
                row = self.row('t1')
                self.assertEqual(row['state_text'], label)
                self.assertIn(group, row['state_groups'])
                self.assertEqual(row['completed'], progress == 1)

    def test_exact_remove_keeps_other_copies_and_excludes_before_request(self):
        def check_excluded():
            state = json.loads((self.directory / 'batch.json').read_text())
            self.assertIn(self.hash, state['seen_hashes'])
            self.assertEqual(state['criteria'], self.batch['criteria'])
        self.q2.before_remove = check_excluded
        result = self.action()
        self.assertTrue(result['ok'])
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertIn(self.hash, self.q1.qb)
        self.assertIn(self.hash, self.t1.tr)
        recovery = list((self.directory / 'operations').glob('*/*.json'))
        self.assertEqual(len(recovery), 1)
        self.assertEqual(json.loads(recovery[0].read_text())['instance_id'], 'q2')
        self.assertEqual(len(list((self.directory / 'operations').glob('*/*.torrent'))), 1)

    def test_tr_remove_keep_data_and_no_tracker_secret_in_recovery(self):
        self.assertTrue(self.action(tasks=[self.task('t1')])['ok'])
        self.assertIn(self.hash, self.q1.qb)
        calls = [arguments for method, arguments in self.t1.calls if method == 'torrent-remove']
        self.assertEqual(calls, [{'ids': [self.hash], 'delete-local-data': False}])
        metadata = next((self.directory / 'operations').glob('*/*.json')).read_text()
        self.assertNotIn('fake-private', metadata)
        self.assertNotIn('login-secret', metadata)

    def test_group_actions_precise_partial_failures(self):
        self.q1.errors['qb'] = 'secret'
        result = self.action('stop', [self.task('q1'), self.task('q2'), self.task('t1')])
        self.assertFalse(result['ok'])
        self.assertEqual({r['instance_id']: r['ok'] for r in result['results']}, {'q1': False, 'q2': True, 't1': True})
        self.assertEqual(self.q2.qb[self.hash]['state'], 'stoppedUP')
        self.assertEqual(self.t1.tr[self.hash]['status'], 0)
        self.assertEqual(self.q1.qb[self.hash]['state'], 'uploading')
        self.assertTrue(self.action('start')['ok'])
        self.assertTrue(self.action('verify')['ok'])
        self.assertEqual(self.q2.qb[self.hash]['state'], 'checkingUP')

    def test_failure_retains_stale_rows_and_never_deletes(self):
        self.fleet.snapshot(True)
        self.q2.errors['qb'] = 'password=q2-secret'
        row = self.row()
        self.assertTrue(row['stale'])
        self.assertEqual(row['validity'], 'unknown')
        self.assertFalse(self.action()['ok'])
        self.assertIn(self.hash, self.q2.qb)
        self.assertFalse(any(method == 'qremove' for method, _ in self.q2.calls))

    def test_export_fault_and_identity_change_never_delete(self):
        self.q2.faults['qexport'] = ['before']
        self.assertFalse(self.action()['ok'])
        self.q2.data = torrent(99)
        self.assertFalse(self.action()['ok'])
        self.assertIn(self.hash, self.q2.qb)

    def test_delete_confirmation_and_identity_required(self):
        for body in ({'action': 'remove', 'tasks': [self.task()]},
                     {'action': 'remove', 'tasks': [{'hash': self.hash}], 'confirm': 'REMOVE_TASKS_KEEP_DATA'},
                     {'action': 'stop', 'tasks': [{'instance_id': 'q2', 'hash': 'bad'}]}):
            with self.assertRaises(downloads.ManagementError):
                self.fleet.action(body)
        self.assertFalse(self.action(tasks=[self.task(value='a' * 40)])['ok'])

    def test_invalid_wait_recheck_only_selected_instance(self):
        self.q2.qb[self.hash]['num_complete'] = 11
        self.fleet.update_policy({**self.fleet.policy(), 'cleanup_wait_hours': 1})
        self.wait_ready()
        self.q1.errors['qb'] = 'unrelated outage'
        self.assertTrue(self.action('delete_expired')['ok'])
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertIn(self.hash, self.q1.qb)
        self.assertIn(self.hash, self.t1.tr)

    def test_invalid_unknown_threshold_conflict_and_long_gap_reset(self):
        self.q2.qb[self.hash]['num_complete'] = 11
        first = self.row()['invalid_since']
        self.advance(181)
        self.assertGreater(self.row()['invalid_since'], first)
        self.q2.tracker_values = [10, 11]
        self.assertIsNone(self.row()['invalid_since'])
        self.q2.tracker_values = [11]
        self.site['stale'] = True
        self.assertIsNone(self.row()['invalid_since'])

    def test_unregistered_default_off_then_wait_restore_reset_and_scope(self):
        self.assertFalse(self.fleet.policy()['unregistered_enabled'])
        self.unregistered()
        self.wait_ready('unregistered_ready')
        self.q2.tracks[0].update(status=2, msg='', num_seeds=5)
        row = self.row()
        self.assertFalse(row['unregistered'])
        self.assertIsNone(row['unregistered_since'])
        self.assertFalse(self.action('delete_expired')['ok'])
        self.assertIn(self.hash, self.q2.qb)
        self.q2.tracks[0].update(status=4, msg='Unregistered torrent')
        self.assertEqual(self.row()['unregistered_since'], self.now)
        self.fleet.update_policy({**self.fleet.policy(), 'unregistered_instances': ['t1']})
        self.assertIsNone(self.row()['unregistered_since'])

    def test_general_errors_auth_conflicts_and_non_pts_are_unknown(self):
        self.unregistered()
        for message in ('Unauthorized', 'Connection timed out', 'Unregistered torrent: invalid passkey', 'HTTP 404'):
            self.q2.tracks[0]['msg'] = message
            self.assertIsNone(self.row()['unregistered'])
            self.assertIsNone(self.row()['unregistered_since'])
        self.q2.tracks[0]['msg'] = 'Unregistered torrent'
        self.q2.tracks.append({**self.q2.tracks[0], 'status': 2, 'msg': ''})
        self.assertIsNone(self.row()['unregistered_since'])
        self.q2.tracks = [self.q2.tracks[0]]
        self.q2.tracks[0]['url'] = 'https://elsewhere.invalid/announce'
        self.assertIsNone(self.row()['unregistered'])
        self.fleet.update_policy({**self.fleet.policy(), 'unregistered_scope': 'all'})
        self.assertTrue(self.row()['unregistered'])

    def test_unregistered_delete_last_recheck_and_restart_wait_reset(self):
        self.unregistered()
        self.wait_ready('unregistered_ready')
        original = self.q2.qexport
        def export(value):
            data = original(value)
            self.q2.tracks[0]['msg'] = 'Authentication failed'
            return data
        self.q2.qexport = export
        self.assertFalse(self.action('delete_expired')['ok'])
        self.assertIn(self.hash, self.q2.qb)
        self.assertNotIn('q2:' + self.hash, self.fleet.state['observations'])
        self.q2.tracks[0]['msg'] = 'Unregistered torrent'
        self.row()
        self.advance(181)
        self.fleet = self.make_fleet()
        self.assertEqual(self.row()['unregistered_since'], self.now)
        self.assertFalse(self.row()['unregistered_ready'])

    def test_shared_delete_limit_does_not_mark_unprocessed_unregistered_cycle_complete(self):
        self.unregistered()
        self.q1.qb[self.hash]['num_complete'] = 11
        self.settings['delete_max_per_job'] = 1
        self.fleet.update_policy({**self.fleet.policy(), 'cleanup_enabled': True, 'cleanup_wait_hours': 1})
        self.wait_ready('unregistered_ready')
        self.fleet._tick()
        self.assertNotIn(self.hash, self.q1.qb)
        self.assertIn(self.hash, self.q2.qb)
        self.assertIsNone(self.fleet.state['last_unregistered_cleanup'])
        self.advance(60)
        self.fleet._tick()
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertEqual(self.fleet.state['last_unregistered_cleanup'], self.now)

    def test_unregistered_failed_instance_does_not_advance_successful_cycle(self):
        self.unregistered()
        self.wait_ready('unregistered_ready')
        self.q1.errors['qb'] = 'offline'
        self.fleet._tick()
        self.assertIsNone(self.fleet.state['last_unregistered_cleanup'])
        self.assertIsNone(json.loads(self.fleet.state_path.read_text())['last_unregistered_cleanup'])
        self.q1.errors.clear()
        self.advance(60)
        self.fleet._tick()
        self.assertEqual(self.fleet.state['last_unregistered_cleanup'], self.now)

    def test_unregistered_failed_delete_retries_without_advancing_cycle(self):
        self.unregistered()
        self.wait_ready('unregistered_ready')
        self.q2.faults['qremove'] = ['before']
        self.fleet._tick()
        self.assertIn(self.hash, self.q2.qb)
        self.assertIsNone(self.fleet.state['last_unregistered_cleanup'])
        self.assertIsNone(json.loads(self.fleet.state_path.read_text())['last_unregistered_cleanup'])
        self.wait_ready('unregistered_ready')
        self.fleet._tick()
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertEqual(self.fleet.state['last_unregistered_cleanup'], self.now)

    def test_unknown_instance_does_not_trigger_any_api(self):
        with self.assertRaises(downloads.ManagementError):
            self.action(tasks=[self.task('missing')])
        self.assertTrue(all(not api.calls for api in self.endpoints.values()))

    def test_configuration_delete_never_mutates_tasks_and_tr_only_management(self):
        self.settings['downloaders'] = self.fleet.registry.removed('q2')
        self.fleet.invalidate(True)
        self.assertIn(self.hash, self.q2.qb)
        self.settings['downloaders'] = [instance('t1', 'tr')]
        self.source = {'api_base': SOURCE['api_base']}
        self.fleet.invalidate(True)
        self.assertEqual(len(self.fleet.snapshot(True)['items']), 1)
        self.assertTrue(self.action('stop', [self.task('t1')])['ok'])
        with self.assertRaisesRegex(pull.PullError, 'enabled_default_qb_required'):
            pull.FleetClients(self.source, self.settings, self.factory)

    def test_limits_selected_tr_units_schedule_roundtrip(self):
        self.assertTrue(self.fleet.limits('t1', True)['values']['connected'])
        self.assertAlmostEqual(self.fleet.limits('t1')['values']['upload_kib'], 100000 / 1024)
        result = self.fleet.set_limits({'instance_id': 't1', 'upload_kib': 128, 'download_kib': 0, 'disable_alternative': True})
        self.assertTrue(result['ok'])
        self.assertEqual(self.t1.session['speed-limit-up'], 131)
        self.assertFalse(self.t1.session['speed-limit-down-enabled'])
        self.assertTrue(self.t1.session['alt-speed-time-enabled'])
        self.assertFalse(self.t1.session['alt-speed-enabled'])
        self.assertEqual(self.q1.calls, [])
        self.assertEqual(self.q2.calls, [])
        self.assertEqual(self.t2.calls, [])

    def test_snapshot_cache_and_public_redaction(self):
        self.q2.qb[self.hash]['name'] = 'q2-secret site-secret https://q2.invalid/passkey=fake-private'
        first = self.fleet.snapshot(True)
        calls = len(self.t1.calls)
        self.fleet.snapshot(False)
        self.assertEqual(len(self.t1.calls), calls)
        public = json.dumps(first)
        for secret in ('q2-secret', 'site-secret', 'http://q2.invalid', 'fake-private', 'login-secret', 'q2-user'):
            self.assertNotIn(secret, public)
        self.assertEqual(self.row('t1')['category'], '')
        self.assertEqual(self.row('t1')['category_kind'], 'tr_labels')

    def test_new_pair_defaults_to_native_seeding_without_full_verify(self):
        self.legacy = management.Manager(self.directory, lambda: self.source, lambda: self.settings,
            lambda: self.site, contextlib.nullcontext, client_factory=self.factory, clock=lambda: self.now)
        self.fleet = self.make_fleet()
        self.transfer()
        for _ in range(6):
            self.fleet._tick()
            self.now += 1
            if not self.fleet.busy():
                break
        self.assertEqual(self.fleet.public_job()['verification_mode'], 'native')
        self.assertEqual(self.fleet.public_job()['status'], 'completed')
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertIn(self.hash, self.q1.qb)
        self.assertEqual(self.t2.tr[self.hash]['status'], 6)
        self.assertFalse(any(name == 'torrent-verify' for api in (self.q1, self.q2, self.t1, self.t2)
                             for name, _ in api.calls))

    def test_transfer_busy_cancel_restart_and_shared_root(self):
        job = self.transfer()['job']
        self.assertEqual(job['source_instance_id'], 'q2')
        self.assertEqual(job['target_instance_id'], 't2')
        self.assertTrue(self.fleet.busy())
        with self.assertRaises(downloads.ManagementError):
            self.action('stop')
        with self.assertRaises(downloads.ManagementError):
            self.transfer('t1', 'q1')
        self.fleet._tick()
        self.assertEqual(self.q2.qb[self.hash]['state'], 'stoppedUP')
        self.assertIn(self.hash, self.t2.tr)
        self.assertEqual(self.t2.tr[self.hash]['status'], 2)
        self.assertEqual(self.q1.qb[self.hash]['state'], 'uploading')
        metadata = next((self.directory / 'fleet_pairs').glob('*/pair.json')).read_text()
        self.assertNotIn('password', metadata)
        self.assertNotIn('http', metadata)
        self.assertEqual(len(list((self.directory / 'operations').glob('*/*.torrent'))), 1)
        self.fleet = self.make_fleet()
        self.assertEqual(self.fleet.public_job()['id'], job['id'])
        self.fleet.cancel({'job_id': job['id']})
        self.fleet._tick()
        self.assertEqual(self.fleet.public_job()['status'], 'cancelled')
        self.assertEqual(self.q2.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.t2.tr[self.hash]['status'], 0)

    def test_transfer_completion_and_missing_response_resume_exact_pair(self):
        self.transfer()
        self.t2.faults['torrent-add'] = ['lost']
        self.fleet._tick()
        self.assertTrue(self.fleet.busy())
        self.fleet = self.make_fleet()
        self.fleet._tick()
        self.complete_verify()
        self.q2.faults['qremove'] = ['lost']
        self.fleet._tick()
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertTrue(self.fleet.busy())
        self.fleet = self.make_fleet()
        self.fleet._tick()
        self.assertEqual(self.fleet.public_job()['status'], 'completed')
        self.assertIn(self.hash, self.q1.qb)
        self.assertEqual(self.t2.tr[self.hash]['status'], 6)
        self.assertEqual(self.fleet.snapshot(True)['totals']['managed_active'], 1)
        with self.assertRaises(downloads.ManagementError):
            self.fleet.cancel({'job_id': self.fleet.public_job()['id']})

    def test_transfer_wrong_directory_preserves_source_and_tr_labels(self):
        self.t2.tr[self.hash] = tr_task(self.hash, downloadDir='/other')
        self.transfer()
        self.fleet._tick()
        self.assertIn(self.hash, self.q2.qb)
        self.assertEqual(self.q2.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.fleet.public_job()['status'], 'failed')
        self.t2.tr.clear()
        self.transfer()
        self.fleet._tick()
        arguments = next(args for method, args in self.t2.calls if method == 'torrent-add')
        self.assertEqual(arguments['labels'], ['pts保种组', 'PTS', 'keep'])

    def test_legacy_root_job_visible_and_resumed_before_pair_jobs(self):
        self.legacy.begin('transfer', {'hashes': [self.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.assertTrue(self.fleet.busy())
        self.assertEqual(self.fleet.public_job()['id'], self.legacy.public_job()['id'])
        self.assertEqual(self.fleet.public_job()['source_instance_id'], 'qb')
        self.fleet.cancel({'job_id': self.legacy.public_job()['id']})
        self.fleet._tick()
        self.assertEqual(self.legacy.public_job()['status'], 'cancelled')
        self.assertEqual(self.q1.qb[self.hash]['state'], 'uploading')

    def test_monitor_keeps_cleanup_disabled_and_policy_validation(self):
        self.q2.qb[self.hash]['num_complete'] = 11
        self.fleet.update_policy({**self.fleet.policy(), 'cleanup_wait_hours': 1})
        self.wait_ready()
        self.fleet._tick()
        self.assertIn(self.hash, self.q2.qb)
        for fields in ({'unregistered_scope': 'bad'}, {'unregistered_instances': ['missing']},
                       {'unregistered_wait_hours': float('nan')}, {'unregistered_max_per_run': True}):
            with self.assertRaises(downloads.ManagementError):
                self.fleet.update_policy({**self.fleet.policy(), **fields})

    def test_native_all_enabled_unique_and_failure_stops_refill(self):
        clients = pull.FleetClients(self.source, self.settings, self.factory)
        qb, tr = clients.snapshot()
        self.assertEqual(len(qb), 1)
        self.assertEqual(len(tr), 1)
        self.assertEqual(pull.managed_counts(self.batch, qb, tr)['managed_active'], 1)
        self.q2.errors['qb'] = 'failure'
        with self.assertRaisesRegex(pull.PullError, 'fleet_inventory_unconfirmed'):
            clients.snapshot()
        self.q2.errors.clear()
        self.settings['downloaders'][1]['enabled'] = False
        self.q2.errors['qb'] = 'ignored disabled'
        pull.FleetClients(self.source, self.settings, self.factory).snapshot()

    def test_native_default_destination_record_and_permanent_migration_dedup(self):
        self.settings.update(mode='maintain', target=2, max_per_run=1, max_bytes=500, min_seeders=2, max_seeders=6,
                             historical_config='unused')
        self.settings['downloaders'][0]['default'] = False
        self.settings['downloaders'][1].update(default=True, category='new', tag='dest', download_path='/downloads/new')
        clients = pull.FleetClients(self.source, self.settings, self.factory)
        new_data = torrent(80, b'new')
        meta = torrent_metadata(new_data)
        choices = {'2': {'id': '2', 'url': 'offline', 'hash': meta['hashes'][0], 'size': 80, 'seeders': 2}}
        downloader = type('Offline', (), {'get': lambda _, c: (new_data, meta, None)})()
        def add(fake_clients, data):
            task = qb_task(meta['hashes'][0], size=80, save_path='/downloads/new', category='new', tags='dest,pts保种组')
            self.q2.qb[meta['hashes'][0]] = task
        with patch.object(pull.Clients, 'add', add), patch.object(pull, 'history_hashes', return_value=set()), contextlib.redirect_stdout(io.StringIO()):
            pull.run(self.settings, self.source, self.directory, clients=clients,
                     pool_reader=lambda *args: (choices, self.now), downloader=downloader)
        state = json.loads((self.directory / 'batch.json').read_text())
        record = state['accepted']['2']
        self.assertEqual(record['instance_id'], 'q2')
        self.assertEqual(record['destination']['category'], 'new')
        self.assertNotIn(meta['hashes'][0], self.q1.qb)
        self.assertEqual(state['criteria'], self.batch['criteria'])
        self.t2.tr[meta['hashes'][0]] = tr_task(meta['hashes'][0], size=80)
        self.q2.qb.pop(meta['hashes'][0])
        qb, tr = clients.snapshot()
        self.assertEqual(pull.managed_counts(state, qb, tr)['managed_active'], 2)
        self.t2.tr.pop(meta['hashes'][0])
        self.settings['target'] = 3
        with patch.object(pull.Clients, 'add', side_effect=AssertionError('permanent hash must not re-add')), patch.object(pull, 'history_hashes', return_value=set()), contextlib.redirect_stdout(io.StringIO()):
            pull.run(self.settings, self.source, self.directory, clients=clients,
                     pool_reader=lambda *args: (choices, self.now), downloader=downloader)
        self.assertIn(meta['hashes'][0], json.loads((self.directory / 'batch.json').read_text())['seen_hashes'])

    def test_native_size_conflict_is_fatal(self):
        self.q2.qb[self.hash]['total_size'] = 101
        with self.assertRaisesRegex(pull.PullError, 'fleet_inventory_unconfirmed'):
            pull.FleetClients(self.source, self.settings, self.factory).snapshot()

    def test_monitor_auto_cleanup_only_ready_target_and_periodic_unregistered(self):
        self.unregistered()
        self.fleet.update_policy({**self.fleet.policy(), 'unregistered_interval_hours': 1})
        self.fleet._tick()
        self.wait_ready('unregistered_ready')
        self.fleet.last_monitor = None
        self.fleet._tick()
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertIn(self.hash, self.q1.qb)
        self.assertIn(self.hash, self.t1.tr)
        log = (self.directory / 'management.log').read_text()
        for secret in ('q2-secret', 'login-secret', 'site-secret', 'fake-private', 'http://'):
            self.assertNotIn(secret, log)
        self.assertIn('fleet_tasks_processed', log)

    def test_failed_delete_observation_resets_immediately(self):
        self.unregistered()
        self.wait_ready('unregistered_ready')
        self.q2.errors['qb'] = 'authentication failure'
        self.assertFalse(self.action('delete_expired')['ok'])
        self.q2.errors.clear()
        self.advance(1)
        self.assertEqual(self.row()['unregistered_since'], self.now)
        self.assertFalse(self.row()['unregistered_ready'])

    def test_malformed_instance_response_keeps_other_instances_available(self):
        self.fleet.snapshot(True)
        self.t1.tr[self.hash]['labels'] = [None]
        value = self.fleet.snapshot(True)
        summaries = {row['id']: row for row in value['instances']}
        self.assertFalse(summaries['t1']['connected'])
        self.assertTrue(summaries['q2']['connected'])
        self.assertTrue(self.row('t1')['stale'])
        self.assertTrue(self.action()['ok'])
        self.assertIn(self.hash, self.t1.tr)

    def test_native_automatic_registry_selection_uses_default_qb(self):
        self.settings.update(mode='maintain', target=1, max_bytes=500, min_seeders=2, max_seeders=6, historical_config='unused')
        with patch.object(downloads, 'API', self.factory), patch.object(pull, 'history_hashes', return_value=set()), contextlib.redirect_stdout(io.StringIO()):
            pull.run(self.settings, self.source, self.directory,
                     pool_reader=lambda *args: (_ for _ in ()).throw(AssertionError('target already satisfied')))
        status = json.loads((self.directory / 'status.json').read_text())
        self.assertEqual(status['managed_active'], 1)
        self.assertEqual(status['added_this_run'], 0)

    def test_remove_before_first_batch_keeps_permanent_exclusions_on_refill(self):
        (self.directory / 'batch.json').unlink()
        self.assertTrue(self.action()['ok'])
        self.assertIn(self.hash, json.loads((self.directory / 'batch.json').read_text())['seen_hashes'])
        self.q1.qb.clear()
        self.t1.tr.clear()
        self.settings.update(mode='maintain', target=1, max_bytes=500, min_seeders=2, max_seeders=6, historical_config='unused')
        choices = {'1': {'id': '1', 'hash': self.hash, 'size': 100, 'seeders': 2, 'url': 'offline'}}
        with patch.object(downloads, 'API', self.factory), patch.object(pull, 'history_hashes', return_value=set()), contextlib.redirect_stdout(io.StringIO()):
            pull.run(self.settings, self.source, self.directory, pool_reader=lambda *args: (choices, self.now))
        state = json.loads((self.directory / 'batch.json').read_text())
        self.assertEqual(state['accepted'], {})
        self.assertIn(self.hash, state['seen_hashes'])
        self.assertEqual(json.loads((self.directory / 'status.json').read_text())['added_this_run'], 0)




class TrackerReportTests(unittest.TestCase):
    def test_tr_requires_recent_explicit_report_no_general_failure_or_conflict(self):
        track = {'announce': 'https://tracker.ptskit.org/announce', 'lastAnnounceTime': NOW,
                 'lastAnnounceSucceeded': False, 'lastAnnounceResult': 'Torrent not registered'}
        read = lambda tracks: fleet_module.unregistered_report(tracks, 'tr', SOURCE, NOW, 7200)
        self.assertTrue(read([track]))
        for fields in ({'lastAnnounceTime': NOW - 7201}, {'lastAnnounceResult': 'Connection failed'},
                       {'lastAnnounceResult': 'Unregistered torrent, invalid passkey'}, {'lastAnnounceTime': NOW + 61},
                       {'lastAnnounceSucceeded': True}):
            self.assertIsNone(read([{**track, **fields}]))
        self.assertIsNone(read([track, {**track, 'lastAnnounceSucceeded': True, 'lastAnnounceResult': ''}]))


if __name__ == '__main__':
    unittest.main()
