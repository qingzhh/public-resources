"""Offline acceptance for observations, task-only cleanup, recoverable transfer and speed limits."""
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import seedkeep_downloaders as downloads
import seedkeep_management as management
import seedkeep_pull as pull
from test_seedkeep_pull import torrent
from torrent_meta import torrent_metadata

SOURCE = {'api_base': 'https://www.ptskit.org', 'host': 'test.invalid', 'port': 1}
NOW = 2000000


def qb_task(value, size=100, seeders=5, **fields):
    return {'hash': value, 'name': '测试任务', 'total_size': size, 'progress': 1, 'completed': size,
            'amount_left': 0, 'state': 'uploading', 'save_path': '/downloads/PTS', 'num_complete': seeders,
            'tracker': 'https://tracker.ptskit.org/announce?passkey=fake-private',
            'upspeed': 2048, 'dlspeed': 0, 'tags': 'pts保种组', **fields}


def tr_task(value, size=100, seeders=5, **fields):
    return {'id': 1, 'hashString': value, 'name': '测试任务', 'totalSize': size, 'percentDone': 1, 'status': 6,
            'downloadDir': '/downloads/PTS', 'rateUpload': 1024, 'rateDownload': 0, 'error': 0, 'labels': ['pts保种组'],
            'leftUntilDone': 0, 'recheckProgress': 0, 'haveValid': size, 'haveUnchecked': 0,
            'magnetLink': 'magnet:?xt=urn:btih:' + value + '&tr=fake-private',
            'trackerStats': [{'announce': 'https://tracker.ptskit.org/announce?passkey=fake-private',
                              'seederCount': seeders, 'lastAnnounceSucceeded': True, 'lastAnnounceTime': NOW}], **fields}


class FakeAPI(downloads.API):
    def __init__(self, data):
        self.data = data
        self.hash = torrent_metadata(data)['hashes'][0]
        self.qb = {self.hash: qb_task(self.hash)}
        self.tr = {}
        self.calls = []
        self.errors = {}
        self.tracker_values = None
        self.tracker_status = 2
        self.stop_blocked = False
        self.faults = {}
        self.before_remove = None
        self.qprefs = {'up_limit': 2048, 'dl_limit': 0, 'alt_up_limit': 1024, 'alt_dl_limit': 512, 'scheduler_enabled': True}
        self.qalt = True
        self.session = {'units': {'speed-bytes': 1000}, 'speed-limit-up': 100, 'speed-limit-down': 200,
                        'speed-limit-up-enabled': True, 'speed-limit-down-enabled': True,
                        'alt-speed-enabled': True, 'alt-speed-up': 50, 'alt-speed-down': 60, 'alt-speed-time-enabled': True}

    def fault(self, operation):
        values = self.faults.get(operation, [])
        mode = values.pop(0) if values else None
        if mode == 'before':
            raise downloads.ManagementError('模拟连接失败', 502)
        return mode

    def inventory(self):
        self.calls.append(('inventory', None))
        return (copy.deepcopy(list(self.qb.values())) if 'qb' not in self.errors else [],
                copy.deepcopy(list(self.tr.values())) if 'tr' not in self.errors else [], dict(self.errors))

    def qget(self, operation, fields=None, *, binary=False):
        self.fault(operation)
        if operation == 'torrents/info':
            values = fields.get('hashes', '').split('|') if fields else list(self.qb)
            return copy.deepcopy([t for h, t in self.qb.items() if h in values])
        if operation == 'app/preferences':
            return copy.deepcopy(self.qprefs)
        if operation == 'transfer/speedLimitsMode':
            return b'1' if self.qalt else b'0'
        raise AssertionError(operation)

    def qpost(self, operation, fields):
        self.calls.append((operation, copy.deepcopy(fields)))
        mode = self.fault(operation)
        if operation == 'app/setPreferences':
            changes = json.loads(fields['json'])
            if mode == 'partial':
                changes = {'up_limit': changes['up_limit']}
            if mode != 'ignored':
                self.qprefs.update(changes)
        elif operation == 'transfer/toggleSpeedLimitsMode':
            self.qalt = not self.qalt
        else:
            raise AssertionError(operation)
        if mode == 'lost':
            raise downloads.ManagementError('模拟回执丢失', 502)
        return b''

    def qtrackers(self, value):
        self.fault('qtrackers')
        values = self.tracker_values if self.tracker_values is not None else [self.qb[value]['num_complete']]
        return [{'url': 'https://tracker.ptskit.org/announce?passkey=fake-private',
                 'status': self.tracker_status, 'num_seeds': count} for count in values]

    def qexport(self, value):
        self.calls.append(('qexport', value))
        self.fault('qexport')
        return self.data

    def qstop(self, value):
        self.calls.append(('qstop', value))
        mode = self.fault('qstop')
        if not self.stop_blocked and value in self.qb:
            self.qb[value]['state'] = 'stoppedUP'
        if mode == 'lost':
            raise downloads.ManagementError('模拟暂停回执丢失', 502)

    def qstart(self, value):
        self.calls.append(('qstart', value))
        self.fault('qstart')
        if value in self.qb:
            self.qb[value]['state'] = 'uploading'

    def qremove(self, value):
        self.calls.append(('qremove', value))
        if self.before_remove:
            self.before_remove()
        mode = self.fault('qremove')
        self.qb.pop(value, None)
        if mode == 'lost':
            raise downloads.ManagementError('模拟移除回执丢失', 502)

    def rpc(self, method, arguments=None):
        arguments = arguments or {}
        self.calls.append((method, copy.deepcopy(arguments)))
        mode = self.fault(method)
        if method == 'session-get':
            return copy.deepcopy(self.session)
        if method == 'session-set':
            if mode != 'ignored':
                self.session.update(arguments)
            result = {}
        elif method == 'torrent-get':
            result = {'torrents': copy.deepcopy([t for h, t in self.tr.items() if 'ids' not in arguments or h in arguments['ids']])}
        elif method == 'torrent-add':
            meta = torrent_metadata(base64.b64decode(arguments['metainfo']))
            value = meta['hashes'][0]
            duplicate = value in self.tr
            if not duplicate:
                self.tr[value] = tr_task(value, meta['size'], status=0, downloadDir=arguments['download-dir'])
                self.tr[value]['labels'] = list(arguments.get('labels', []))
            result = {'torrent-duplicate' if duplicate else 'torrent-added': {'hashString': value}}
        elif method == 'torrent-set':
            for value in arguments['ids']:
                if value in self.tr:
                    self.tr[value]['labels'] = list(arguments['labels'])
            result = {}
        elif method == 'torrent-remove':
            assert arguments['delete-local-data'] is False
            for value in arguments['ids']:
                self.tr.pop(value, None)
            result = {}
        else:
            for value in arguments['ids']:
                t = self.tr.get(value)
                if not t:
                    continue
                if method == 'torrent-stop':
                    t['status'] = 0
                elif method == 'torrent-start':
                    t['status'] = 6
                elif method == 'torrent-verify':
                    t.update(status=2, recheckProgress=0, haveValid=0, haveUnchecked=t['totalSize'])
                else:
                    raise AssertionError(method)
            result = {}
        if mode == 'lost':
            raise downloads.ManagementError('模拟操作回执丢失', 502)
        return result


class InventoryTests(unittest.TestCase):
    def rows(self, q=None, t=None, batch=None, limit=10):
        return downloads.inventory_rows(q or [], t or [], SOURCE, batch or {}, limit, NOW)

    def test_site_boundary_is_unconfirmed_and_strictly_above_is_invalid(self):
        for seeders, expected in ((9, 'valid'), (10, 'unknown'), (11, 'invalid'), (-1, 'unknown'), (True, 'unknown')):
            self.assertEqual(self.rows([qb_task('a' * 40, seeders=seeders)])[0]['validity'], expected)

    def test_partial_paused_queued_error_and_forced_states(self):
        for fields, expected in (({'progress': .5}, 'downloading'), ({'state': 'queuedUP'}, 'inactive'),
                                 ({'state': 'stoppedUP'}, 'inactive'), ({'state': 'error'}, 'inactive'),
                                 ({'state': 'forcedUP'}, 'valid')):
            self.assertEqual(self.rows([qb_task('a' * 40, **fields)])[0]['validity'], expected)

    def test_other_sites_and_unknown_threshold_are_not_invalid(self):
        self.assertEqual(self.rows([qb_task('a' * 40, seeders=999, tracker='https://elsewhere.invalid/announce')])[0]['validity'], 'other')
        self.assertEqual(self.rows([qb_task('a' * 40)], limit=None)[0]['validity'], 'unknown')
        self.assertFalse(downloads.pts_url('https://ptskit.org.evil.invalid/announce', SOURCE))

    def test_tr_uses_recent_successful_matching_tracker_only(self):
        t = tr_task('a' * 40)
        self.assertEqual(downloads.tr_seeders(t, SOURCE, NOW), 5)
        for fields in ({'lastAnnounceTime': NOW - 7201}, {'lastAnnounceSucceeded': False},
                       {'announce': 'https://elsewhere.invalid'}, {'seederCount': -1}, {'lastAnnounceTime': NOW + 61}):
            value = copy.deepcopy(t)
            value['trackerStats'][0].update(fields)
            self.assertIsNone(downloads.tr_seeders(value, SOURCE, NOW))

    def test_conflicting_client_or_tracker_counts_are_unknown(self):
        q, t = qb_task('a' * 40, seeders=11), tr_task('a' * 40, seeders=10)
        self.assertEqual(self.rows([q], [t])[0]['validity'], 'unknown')
        t['trackerStats'].append({**t['trackerStats'][0], 'seederCount': 11})
        self.assertEqual(self.rows(t=[t])[0]['validity'], 'unknown')

    def test_aliases_merge_and_client_states_are_separate(self):
        q = qb_task('a' * 40, state='queuedUP', infohash_v2='b' * 64)
        t = tr_task('b' * 40)
        batch = {'accepted': {'1': {'hash': 'a' * 40, 'hashes': ['a' * 40, 'b' * 40, 'b' * 64]}}}
        rows = self.rows([q], [t], batch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['locations'], ['qb', 'tr'])
        self.assertEqual(rows[0]['client_states']['qb']['validity'], 'inactive')
        self.assertEqual(rows[0]['client_states']['tr']['validity'], 'valid')
        self.assertTrue(rows[0]['managed'])

    def test_transfer_requires_full_data_supported_directory_and_no_checking(self):
        q = qb_task('a' * 40)
        self.assertTrue(self.rows([q])[0]['can_transfer'])
        for fields in ({'progress': .99}, {'amount_left': 1}, {'completed': 99}, {'save_path': '/downloads3'},
                       {'save_path': '/downloads/../secret'}, {'state': 'checkingUP'}, {'state': 'missingFiles'}):
            self.assertFalse(self.rows([{**q, **fields}])[0]['can_transfer'])
        self.assertFalse(self.rows([q], [tr_task('a' * 40, status=2)])[0]['can_transfer'])

    def test_hash_validation_and_shared_path_boundaries(self):
        self.assertEqual(downloads.hashes(['A' * 40, 'a' * 40, 'b' * 64]), ['a' * 40, 'b' * 64])
        for value in ([], ['bad'], ['a' * 40] * 21, [None]):
            with self.assertRaises(downloads.ManagementError):
                downloads.hashes(value, 20)
        self.assertIsNone(downloads.transfer_path('/downloads-other/a'))
        self.assertEqual(downloads.transfer_path('/downloads2/path/'), '/downloads2/path')


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = NOW
        self.site = {'available': True, 'stale': False, 'seeders_max': 10}
        self.api = FakeAPI(torrent(100))
        self.hash = self.api.hash
        self.batch = {'accepted': {'1': {'hash': self.hash, 'hashes': [self.hash]}},
                      'baseline': {'tr_hashes': ['b' * 40]}, 'criteria': {'max_bytes': 500}, 'seen_hashes': ['b' * 40]}
        pull.save_json(self.directory / 'batch.json', self.batch)
        self.manager = self.make_manager()

    def make_manager(self, guard=contextlib.nullcontext):
        return management.Manager(self.directory, lambda: dict(SOURCE), lambda: {}, lambda: dict(self.site), guard,
                                  client_factory=lambda *args: self.api, clock=lambda: self.now,
                                  transfer_mode='full')  # Original full-check recovery contract.

    def snapshot(self):
        return self.manager.snapshot(force=True)

    def row(self):
        return self.snapshot()['items'][0]

    def advance(self, seconds):
        self.now += seconds
        for t in self.api.tr.values():
            for track in t['trackerStats']:
                track['lastAnnounceTime'] = self.now

    def mature(self):
        self.manager.update_policy({**management.DEFAULT_POLICY, 'cleanup_wait_hours': 1})
        self.api.qb[self.hash]['num_complete'] = 11
        self.snapshot()
        for _ in range(60):
            self.advance(60)
            self.snapshot()
        self.assertTrue(self.row()['delete_ready'])

    def begin(self, kind):
        return self.manager.begin(kind, {'hashes': [self.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA' if kind == 'transfer' else 'REMOVE_TASKS_KEEP_DATA'})

    def start_transfer(self):
        self.begin('transfer')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'verifying')
        self.assertIn(self.hash, self.api.qb)

    def finish_verify(self, **fields):
        self.advance(20)
        self.api.tr[self.hash].update(status=0, percentDone=1, leftUntilDone=0, recheckProgress=0,
                                     haveValid=self.api.tr[self.hash]['totalSize'], haveUnchecked=0, **fields)
        self.manager.tick()

    def test_defaults_and_policy_validation(self):
        self.assertEqual(self.manager.config, management.DEFAULT_POLICY)
        for change in ({'cleanup_enabled': 1}, {'cleanup_wait_hours': .5}, {'cleanup_wait_hours': float('nan')},
                       {'cleanup_max_per_run': True}, {'cleanup_max_per_run': 51}):
            with self.assertRaises(downloads.ManagementError):
                self.manager.update_policy({**management.DEFAULT_POLICY, **change})
        self.assertFalse(self.manager.policy_path.exists())

    def test_wait_continuity_and_restart_persist(self):
        self.api.qb[self.hash]['num_complete'] = 11
        first = self.row()['invalid_since']
        self.advance(60)
        self.assertEqual(self.row()['invalid_since'], first)
        self.manager = self.make_manager()
        self.advance(60)
        self.assertEqual(self.row()['invalid_since'], first)
        self.advance(181)
        self.assertEqual(self.row()['invalid_since'], self.now)

    def test_recovery_unknown_threshold_changes_and_old_pts_reset_wait(self):
        self.api.qb[self.hash]['num_complete'] = 11
        self.row()
        for change in ('recovered', 'unknown', 'threshold', 'stale'):
            self.advance(60)
            if change == 'recovered':
                self.api.qb[self.hash]['num_complete'] = 10
            elif change == 'unknown':
                self.api.qb[self.hash]['num_complete'] = -1
            elif change == 'threshold':
                self.site['seeders_max'] = 20
                self.api.qb[self.hash]['num_complete'] = 11
            else:
                self.site.update(seeders_max=10, stale=True)
            self.assertFalse(self.row()['delete_ready'])
            self.assertEqual(self.manager.state['observations'], {})
        self.site['stale'] = False
        self.assertEqual(self.row()['invalid_since'], self.now)

    def test_tracker_failure_conflict_and_client_disconnect_reset_wait(self):
        self.api.qb[self.hash]['num_complete'] = 11
        self.row()
        for values in ([10], [], [11, 10]):
            self.api.tracker_values = values
            self.advance(60)
            self.assertEqual(self.row()['validity'], 'unknown')
            self.assertFalse(self.manager.state['observations'])
        self.api.tracker_values = None
        self.api.tracker_status = 4
        self.assertEqual(self.row()['validity'], 'unknown')
        self.api.tracker_status = 2
        self.row()
        self.api.errors = {'tr': 'failure'}
        self.assertEqual(self.row()['validity'], 'unknown')
        self.assertFalse(self.manager.state['observations'])
        self.api.errors = {'qb': 'failure'}
        self.assertEqual(self.row()['validity'], 'unknown')

    def test_public_results_do_not_include_tracker_paths_metainfo_or_credentials(self):
        self.start_transfer()
        text = json.dumps(self.snapshot(), ensure_ascii=False)
        for value in ('fake-private', 'trackerStats', 'save_path', 'downloadDir', 'metainfo', 'tr_hash', 'tr_was_active', '/downloads/PTS'):
            self.assertNotIn(value, text)
        self.assertIn('verifying', text)

    def test_client_summary_uses_own_activity(self):
        self.api.qb[self.hash]['state'] = 'queuedUP'
        self.api.tr[self.hash] = tr_task(self.hash)
        value = self.snapshot()
        self.assertEqual(value['clients']['qb']['valid'], 0)
        self.assertEqual(value['clients']['qb']['inactive'], 1)
        self.assertEqual(value['clients']['tr']['valid'], 1)
        self.assertEqual(value['totals']['total'], 1)

    def test_delete_rechecks_maturity_preserves_files_and_excludes_before_remove(self):
        self.mature()
        self.api.tr[self.hash] = tr_task(self.hash, seeders=11)
        self.api.before_remove = lambda: self.assertIn(self.hash, management.load(self.directory / 'batch.json', {})['seen_hashes'])
        self.begin('delete')
        self.manager.tick()
        self.assertFalse(self.api.qb)
        self.assertFalse(self.api.tr)
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        saved = management.load(self.directory / 'batch.json', {})
        for key in ('accepted', 'baseline', 'criteria'):
            self.assertEqual(saved[key], self.batch[key])
        job = self.manager.state['job']
        self.assertTrue((self.directory / 'operations' / job['id'] / (self.hash + '.torrent')).exists())
        self.assertIn(('torrent-remove', {'ids': [self.hash], 'delete-local-data': False}), self.api.calls)

    def test_delete_skips_recovered_or_unconfirmed_tasks(self):
        self.mature()
        self.begin('delete')
        self.api.qb[self.hash]['num_complete'] = 10
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.manager.state['job']['status'], 'failed')
        self.assertNotIn(self.hash, management.load(self.directory / 'batch.json', {})['seen_hashes'])

    def test_automatic_cleanup_is_off_and_enabled_batch_is_bounded(self):
        self.mature()
        self.manager.tick()
        self.assertIsNone(self.manager.state['job'])
        self.manager.update_policy({**self.manager.config, 'cleanup_enabled': True, 'cleanup_max_per_run': 1})
        self.advance(60)
        self.manager.tick()
        self.assertEqual(len(self.manager.state['job']['items']), 1)
        self.assertEqual(self.manager.state['job']['kind'], 'delete')

    def test_restart_interrupts_delete_without_blindly_removing(self):
        self.mature()
        self.begin('delete')
        self.manager = self.make_manager()
        self.assertEqual(self.manager.state['job']['status'], 'interrupted')
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)

    def test_begin_checks_confirmation_eligibility_and_exclusion_lock(self):
        for body in ({'hashes': [self.hash], 'confirm': 'wrong'}, {'hashes': [self.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA', 'extra': True}):
            with self.assertRaises(downloads.ManagementError):
                self.manager.begin('transfer', body)
        self.api.qb[self.hash]['progress'] = .9
        with self.assertRaises(downloads.ManagementError):
            self.begin('transfer')
        self.api.qb[self.hash]['progress'] = 1
        @contextlib.contextmanager
        def busy():
            raise downloads.ManagementError('运行锁忙', 409)
            yield
        self.manager.guard = busy
        with self.assertRaises(downloads.ManagementError):
            self.begin('transfer')
        self.assertIsNone(self.manager.state['job'])

    def queue_transfer(self, count=2):
        payloads = [torrent(100, name=f'queue-{n}'.encode()) for n in range(count)]
        metadata = {torrent_metadata(data)['hashes'][0]: data for data in payloads}
        self.api.qb = {value: qb_task(value) for value in metadata}
        self.api.qexport = lambda value: metadata[value]
        selected = list(metadata)
        self.manager.begin('transfer', {'hashes': selected, 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        return selected

    def checked(self, value):
        self.api.tr[value].update(status=0, percentDone=1, leftUntilDone=0, haveValid=100, haveUnchecked=0)

    def test_manual_large_queue_ignores_legacy_cap_but_delete_remains_limited(self):
        selected = self.queue_transfer(150)
        self.assertEqual([item['hash'] for item in self.manager.state['job']['items']], selected)
        self.assertFalse(any(kind in ('qstop', 'qstart', 'qremove', 'torrent-add', 'torrent-verify') for kind, _ in self.api.calls))
        with self.assertRaises(downloads.ManagementError):
            self.manager.begin('delete', {'hashes': selected, 'confirm': 'REMOVE_TASKS_KEEP_DATA'})

    def test_large_serial_queue_fully_verifies_every_task_without_a_batch_boundary(self):
        selected = self.queue_transfer(120)
        self.manager.tick()
        for index, value in enumerate(selected):
            self.assertEqual(self.manager.state['job']['items'][index]['phase'], 'verifying')
            self.assertEqual(len(self.api.tr), index + 1)
            self.checked(value)
            self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertFalse(self.api.qb)
        self.assertEqual(sum(kind == 'torrent-verify' for kind, _ in self.api.calls), 120)
        self.assertEqual([value for kind, value in self.api.calls if kind == 'qremove'], selected)

    def test_completion_starts_next_immediately_and_waiting_check_never_touches_later_sources(self):
        first, second, third = self.queue_transfer(3)
        self.manager.tick()
        self.manager.tick()
        self.assertEqual([value for kind, value in self.api.calls if kind == 'qstop'], [first])
        self.assertEqual(self.api.qb[second]['state'], 'uploading')
        self.checked(first)
        self.manager.tick()
        self.assertEqual([item['phase'] for item in self.manager.state['job']['items']], ['completed', 'verifying', 'queued'])
        self.assertNotIn(first, self.api.qb)
        self.assertEqual(self.api.qb[third]['state'], 'uploading')
        self.assertEqual([value for kind, value in self.api.calls if kind == 'qstop'], [first, second])
        self.checked(second)
        self.manager.tick()
        self.checked(third)
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertEqual([value for kind, value in self.api.calls if kind == 'qremove'], [first, second, third])

    def test_late_queue_item_retries_from_its_own_persisted_start_time(self):
        first, second = self.queue_transfer()
        job = self.manager.state['job']
        job['started_at'] -= management.VERIFY_TIMEOUT + 100
        self.manager.tick()
        self.checked(first)
        self.api.faults['qstop'] = ['before']
        self.manager.tick()
        item = job['items'][1]
        self.assertEqual((item['phase'], job['status']), ('prepared', 'waiting'))
        self.assertEqual(item['started_at'], self.now)
        self.manager = self.make_manager()
        self.assertEqual(self.manager.state['job']['items'][1]['started_at'], self.now)
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][1]['phase'], 'verifying')
        self.checked(second)
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'completed')

    def test_legacy_queued_snapshot_gets_fresh_item_timer_after_restart(self):
        first, second = self.queue_transfer()
        self.manager.state['job']['started_at'] -= management.VERIFY_TIMEOUT + 100
        self.manager.save()
        self.manager = self.make_manager()
        self.api.faults['qstop'] = ['before']
        self.manager.tick()
        item = self.manager.state['job']['items'][0]
        self.assertEqual((item['started_at'], item['phase']), (self.now, 'prepared'))
        self.assertEqual(self.manager.state['job']['status'], 'waiting')
        self.assertEqual(self.api.qb[second]['state'], 'uploading')

    def test_failed_check_recovers_first_before_starting_next_and_cancel_restores_current_only(self):
        first, second, third = self.queue_transfer(3)
        self.manager.tick()
        self.api.tr[first].update(status=0, percentDone=.5, leftUntilDone=50, haveValid=50, haveUnchecked=0)
        self.manager.tick()
        self.assertEqual([item['phase'] for item in self.manager.state['job']['items']], ['failed', 'verifying', 'queued'])
        calls = [kind for kind, value in self.api.calls if value in (first, second) and kind in ('qstop', 'qstart')]
        self.assertEqual(calls, ['qstop', 'qstart', 'qstop'])
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        while self.manager.state['job']['status'] in management.ACTIVE:
            self.manager.tick()
        self.assertEqual([item['phase'] for item in self.manager.state['job']['items']], ['failed', 'cancelled', 'cancelled'])
        self.assertEqual([self.api.qb[value]['state'] for value in (first, second, third)], ['uploading'] * 3)
        self.assertFalse(any(kind == 'qstop' and value == third for kind, value in self.api.calls))

    def test_transfer_pause_verify_then_remove_and_restart_target(self):
        before = (self.directory / 'batch.json').read_bytes()
        self.start_transfer()
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)
        self.finish_verify()
        self.assertFalse(self.api.qb)
        self.assertEqual(self.api.tr[self.hash]['status'], 6)
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertEqual((self.directory / 'batch.json').read_bytes(), before)

    def test_transfer_keeps_paused_source_paused_on_target(self):
        self.api.qb[self.hash]['state'] = 'stoppedUP'
        self.start_transfer()
        self.finish_verify()
        self.assertEqual(self.api.tr[self.hash]['status'], 0)

    def test_transfer_existing_active_tr_is_restored_on_success_and_cancel(self):
        self.api.qb[self.hash]['state'] = 'stoppedUP'
        self.api.tr[self.hash] = tr_task(self.hash)
        self.start_transfer()
        self.finish_verify()
        self.assertEqual(self.api.tr[self.hash]['status'], 6)
        self.api.qb[self.hash] = qb_task(self.hash, state='stoppedUP')
        self.start_transfer()
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        self.manager.tick()
        self.assertEqual(self.api.qb[self.hash]['state'], 'stoppedUP')
        self.assertEqual(self.api.tr[self.hash]['status'], 6)
        self.assertEqual(self.manager.state['job']['status'], 'cancelled')

    def test_cancel_new_target_keeps_it_paused_and_restores_qb(self):
        self.start_transfer()
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        self.manager.tick()
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.api.tr[self.hash]['status'], 0)
        self.assertEqual(self.manager.state['job']['status'], 'cancelled')
        with self.assertRaises(downloads.ManagementError):
            self.manager.cancel({'job_id': 'old-job'})

    def test_incomplete_or_timed_out_verify_never_removes_qb(self):
        self.start_transfer()
        self.api.tr[self.hash].update(status=0, percentDone=.5, leftUntilDone=50, recheckProgress=1)
        self.advance(20)
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'failed')
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertFalse(any(call[0] == 'qremove' for call in self.api.calls))
        self.start_transfer()
        self.advance(management.VERIFY_TIMEOUT + 1)
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.manager.state['job']['status'], 'failed')

    def test_identity_size_path_and_pause_confirmation_protect_source(self):
        self.api.data = torrent(101)
        self.begin('transfer')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'failed')
        self.assertFalse(any(call[0] == 'qstop' for call in self.api.calls))
        self.api.data = torrent(100)
        self.api.tr[self.hash] = tr_task(self.hash, downloadDir='/downloads/other')
        self.begin('transfer')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'failed')
        self.assertIn(self.hash, self.api.qb)
        self.api.tr.clear()
        self.api.stop_blocked = True
        self.begin('transfer')
        self.manager.tick()
        self.assertFalse(self.api.tr)
        self.advance(121)
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'failed')

    def test_changed_source_before_handoff_is_not_removed(self):
        self.start_transfer()
        self.api.qb[self.hash]['save_path'] = '/downloads/changed'
        self.finish_verify()
        self.assertIn(self.hash, self.api.qb)
        self.assertNotEqual(self.manager.state['job']['status'], 'completed')

    def test_lost_add_response_is_reconciled_without_duplicate_add(self):
        self.api.faults['torrent-add'] = ['lost']
        self.begin('transfer')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'waiting')
        self.assertIn(self.hash, self.api.qb)
        self.manager.tick()
        self.finish_verify()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertEqual(sum(call[0] == 'torrent-add' for call in self.api.calls), 1)

    def test_lost_remove_response_and_restart_reconcile_actual_handoff(self):
        self.start_transfer()
        self.api.faults['qremove'] = ['lost']
        self.finish_verify()
        self.assertFalse(self.api.qb)
        self.assertEqual(self.manager.state['job']['status'], 'waiting')
        self.manager = self.make_manager()
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertEqual(sum(call[0] == 'qremove' for call in self.api.calls), 1)
        self.assertEqual(self.api.tr[self.hash]['status'], 6)

    def test_start_failure_after_handoff_retries_without_recreating_source(self):
        self.start_transfer()
        self.api.faults['torrent-start'] = ['before']
        self.finish_verify()
        self.assertFalse(self.api.qb)
        self.assertEqual(self.manager.state['job']['status'], 'waiting')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'completed')

    def test_failed_source_recovery_is_persisted_and_retried(self):
        self.start_transfer()
        self.api.tr[self.hash].update(status=0, percentDone=.5, leftUntilDone=50, recheckProgress=1)
        self.api.faults['qstart'] = ['before']
        self.advance(20)
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'recovering')
        self.manager = self.make_manager()
        self.manager.tick()
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.manager.state['job']['status'], 'failed')

    def test_unchecked_bytes_are_not_a_verified_transfer(self):
        self.start_transfer()
        self.api.tr[self.hash].update(status=0, percentDone=1, leftUntilDone=0, haveValid=0, haveUnchecked=100)
        self.advance(20)
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.manager.state['job']['status'], 'failed')

    def test_restart_during_verify_resumes_and_cancellation_before_add_does_not_mutate(self):
        self.begin('transfer')
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        self.manager.tick()
        self.assertFalse(self.api.tr)
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.start_transfer()
        self.manager = self.make_manager()
        self.finish_verify()
        self.assertEqual(self.manager.state['job']['status'], 'completed')


    def test_lost_verify_response_resumes_observed_check(self):
        self.api.faults['torrent-verify'] = ['lost']
        self.begin('transfer')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'verify_requested')
        self.manager = self.make_manager()
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'verifying')
        self.assertEqual(sum(call[0] == 'torrent-verify' for call in self.api.calls), 1)
        self.finish_verify()
        self.assertEqual(self.manager.state['job']['status'], 'completed')

    def test_verify_request_not_executed_is_resent_before_handoff(self):
        self.api.faults['torrent-verify'] = ['before']
        self.begin('transfer')
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'verify_requested')
        self.assertIn(self.hash, self.api.qb)
        self.manager = self.make_manager()
        self.manager.tick()
        self.assertEqual(self.api.tr[self.hash]['status'], 2)
        self.finish_verify()
        self.assertEqual(self.manager.state['job']['status'], 'completed')

    def test_crash_after_verify_ack_before_phase_save_resumes(self):
        self.begin('transfer')
        original = self.manager.save
        def crash_at_verifying():
            if self.manager.state['job']['items'][0]['phase'] == 'verifying':
                raise SystemExit('simulated crash')
            original()
        with patch.object(self.manager, 'save', side_effect=crash_at_verifying), self.assertRaises(SystemExit):
            self.manager.tick()
        self.assertEqual(management.load(self.manager.state_path, {})['job']['items'][0]['phase'], 'verify_requested')
        self.manager = self.make_manager()
        self.manager.tick()
        self.finish_verify()
        self.assertEqual(self.manager.state['job']['status'], 'completed')

    def test_crash_after_last_transfer_item_before_job_finish_recovers(self):
        self.start_transfer()
        with patch.object(self.manager, 'finish_job', side_effect=SystemExit('simulated crash')), self.assertRaises(SystemExit):
            self.finish_verify()
        self.manager = self.make_manager()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertFalse(self.api.qb)

    def test_crash_after_last_cancelled_item_before_job_finish_recovers(self):
        self.start_transfer()
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        with patch.object(self.manager, 'finish_job', side_effect=SystemExit('simulated crash')), self.assertRaises(SystemExit):
            self.manager.tick()
        self.manager = self.make_manager()
        self.assertEqual(self.manager.state['job']['status'], 'cancelled')
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.begin('transfer')

    def test_crash_after_last_deleted_item_before_job_finish_recovers(self):
        self.mature()
        self.begin('delete')
        with patch.object(self.manager, 'finish_job', side_effect=SystemExit('simulated crash')), self.assertRaises(SystemExit):
            self.manager.tick()
        self.manager = self.make_manager()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertFalse(self.api.qb)


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI(torrent(100))

    def save(self, chosen, **values):
        return self.api.set_limits({'client': chosen, 'upload_kib': 10, 'download_kib': 0, 'disable_alternative': False, **values})

    def test_qb_bytes_and_alternative_mode_preserved(self):
        result = self.save('qb')
        self.assertEqual((self.api.qprefs['up_limit'], self.api.qprefs['dl_limit']), (10240, 0))
        self.assertEqual(result['qb']['upload_kib'], 10)
        self.assertTrue(self.api.qalt)
        self.assertTrue(self.api.qprefs['scheduler_enabled'])
        self.save('qb', disable_alternative=True)
        self.assertFalse(self.api.qalt)
        self.assertTrue(self.api.qprefs['scheduler_enabled'])

    def test_transmission_native_units_roundtrip_and_zero_unlimited(self):
        for unit in (1000, 1024):
            self.api.session['units']['speed-bytes'] = unit
            self.save('tr')
            self.assertEqual(self.api.session['speed-limit-up'], round(10240 / unit))
            self.assertEqual(self.api.limits()['tr']['upload_kib'], self.api.session['speed-limit-up'] * unit / 1024)
            self.assertFalse(self.api.session['speed-limit-down-enabled'])
            self.assertTrue(self.api.session['alt-speed-enabled'])
            self.assertTrue(self.api.session['alt-speed-time-enabled'])
        self.save('tr', disable_alternative=True)
        self.assertFalse(self.api.session['alt-speed-enabled'])
        self.assertTrue(self.api.session['alt-speed-time-enabled'])

    def test_invalid_values_are_rejected_without_mutation(self):
        for values in ({'upload_kib': True}, {'upload_kib': -.1}, {'upload_kib': .1}, {'upload_kib': float('nan')},
                       {'download_kib': 1048577}, {'client': 'other'}, {'disable_alternative': 1}, {'extra': 1}):
            with self.assertRaises(downloads.ManagementError):
                self.save('qb', **values)
        self.assertFalse(self.api.calls)

    def test_partial_or_lost_save_rolls_back_both_directions_and_reports_safely(self):
        for client, operation, mode in (('qb', 'app/setPreferences', 'partial'), ('qb', 'app/setPreferences', 'lost'), ('tr', 'session-set', 'lost')):
            before = (copy.deepcopy(self.api.qprefs), copy.deepcopy(self.api.session), self.api.qalt)
            self.api.faults[operation] = [mode]
            with self.assertRaisesRegex(downloads.ManagementError, '已恢复原设置'):
                self.save(client, download_kib=20, disable_alternative=True)
            self.assertEqual((self.api.qprefs, self.api.session, self.api.qalt), before)

    def test_rollback_failure_is_not_reported_as_restored(self):
        self.api.faults['app/setPreferences'] = ['lost', 'before']
        with self.assertRaisesRegex(downloads.ManagementError, '请刷新检查'):
            self.save('qb')

    def test_task_removal_protocol_always_keeps_files(self):
        api = object.__new__(downloads.API)
        with patch.object(api, 'qpost') as post:
            api.qremove('a' * 40)
        post.assert_called_once_with('torrents/delete', {'hashes': 'a' * 40, 'deleteFiles': 'false'})

    def test_rpc_session_conflict_retries_closes_error_and_hides_external_detail(self):
        api = object.__new__(downloads.API)
        api.settings = {'tr_url': 'http://test.invalid/rpc'}
        api.tr_headers = {'Content-Type': 'application/json'}
        api.tr = type('Opener', (), {})()
        error = urllib.error.HTTPError('http://test.invalid/rpc', 409, 'fake-private', {'X-Transmission-Session-Id': 'fake-session'}, io.BytesIO(b'private'))
        response = io.BytesIO(json.dumps({'result': 'success', 'arguments': {'ok': True}}).encode())
        with patch.object(api.tr, 'open', create=True, side_effect=[error, response]) as opened:
            self.assertEqual(api.rpc('session-get'), {'ok': True})
            self.assertEqual(opened.call_count, 2)
        self.assertTrue(error.fp.closed)
        with patch.object(api.tr, 'open', create=True, side_effect=ValueError('fake-private')):
            with self.assertRaises(downloads.ManagementError) as raised:
                api.rpc('session-get')
        self.assertNotIn('fake-private', str(raised.exception))


class NativeSeedingAPI(FakeAPI):
    """Native-shaped add result for already complete data; no extra verify request."""
    native_status = 6
    native_progress = 1

    def rpc(self, method, arguments=None):
        previous = set(self.tr)
        try:
            return super().rpc(method, arguments)
        finally:
            if method == 'torrent-add':
                for value in set(self.tr) - previous:
                    target = self.tr[value]
                    target.update(status=self.native_status, percentDone=self.native_progress,
                                  leftUntilDone=0 if self.native_progress == 1 else 1,
                                  haveValid=0, haveUnchecked=target['totalSize'], error=0)


class NativeTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = NOW
        self.api = NativeSeedingAPI(torrent(100))
        self.hash = self.api.hash
        self.runtime = {}
        self.manager = self.make_manager()

    def make_manager(self):
        return management.Manager(self.directory, lambda: dict(SOURCE), lambda: dict(self.runtime),
                                  lambda: {'available': True, 'stale': False, 'seeders_max': 10},
                                  contextlib.nullcontext, client_factory=lambda *args: self.api,
                                  clock=lambda: self.now)

    def begin(self):
        return self.manager.begin('transfer', {'hashes': [self.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})

    def drain(self, rounds=6):
        for _ in range(rounds):
            self.manager.tick()
            if self.manager.state['job']['status'] not in management.ACTIVE:
                break
            self.now += 1
        return self.manager.state['job']

    def test_mapped_content_path_fast_takeover_survives_restart(self):
        self.runtime['transfer_path_mappings'] = [{'qb': '/downloads', 'tr': '/media/seed'}]
        self.begin()
        self.manager.tick()
        self.assertEqual(self.api.tr[self.hash]['downloadDir'], '/media/seed/PTS')
        self.manager = self.make_manager()
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertNotIn(self.hash, self.api.qb)
        self.assertEqual(self.api.tr[self.hash]['status'], 6)
        self.assertFalse(any(method == 'torrent-verify' for method, _ in self.api.calls))

    def test_completed_data_seeds_without_a_second_full_verify(self):
        self.begin()
        job = self.drain()
        self.assertFalse(any(method == 'torrent-verify' for method, _ in self.api.calls))
        self.assertEqual(job['verification_mode'], 'native')
        self.assertEqual(job['status'], 'completed')
        self.assertNotIn(self.hash, self.api.qb)
        self.assertEqual(self.api.tr[self.hash]['status'], 6)
        self.assertEqual(self.api.tr[self.hash]['haveUnchecked'], 100)
        added = next(arguments for method, arguments in self.api.calls if method == 'torrent-add')
        self.assertFalse(added['paused'])

    def test_incomplete_and_malformed_source_never_enters_transfer(self):
        for change in ({'progress': .99}, {'completed': 99}, {'amount_left': 1},
                       {'progress': True}, {'completed': True}, {'state': 'checkingUP'},
                       {'state': 'moving'}, {'state': 'pausedDL'}):
            with self.subTest(change=change):
                self.api.qb[self.hash] = qb_task(self.hash, size=100, **change)
                with self.assertRaises(downloads.ManagementError):
                    self.begin()
                self.assertFalse(any(method in ('qstop', 'qremove', 'torrent-add') for method, _ in self.api.calls))

    def test_pause_readback_rechecks_source_completion_before_adding(self):
        stop = self.api.qstop
        def changed_after_pause(value):
            stop(value)
            self.api.qb[value].update(progress=.9, completed=90, amount_left=10, state='stoppedDL')
        self.api.qstop = changed_after_pause
        self.begin()
        job = self.drain()
        self.assertEqual(job['status'], 'failed')
        self.assertIn(self.hash, self.api.qb)
        self.assertFalse(any(method in ('torrent-add', 'qremove') for method, _ in self.api.calls))

    def test_native_initial_check_keeps_source_and_does_not_request_verify(self):
        self.api.native_status = 2
        self.begin()
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'waiting_target')
        self.assertFalse(any(method == 'torrent-verify' for method, _ in self.api.calls))
        self.api.tr[self.hash]['status'] = 6
        self.assertEqual(self.drain()['status'], 'completed')

    def test_download_state_is_not_reported_as_success_or_source_removed(self):
        self.api.native_status, self.api.native_progress = 4, .5
        self.begin()
        job = self.drain()
        self.assertEqual(job['status'], 'failed')
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.api.tr[self.hash]['status'], 0)
        self.assertFalse(any(method in ('qremove', 'torrent-verify') for method, _ in self.api.calls))

    def test_seed_queue_waits_for_actual_seeding_before_handoff(self):
        self.api.native_status = 5
        self.begin()
        self.manager.tick()
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'waiting_target')
        self.api.tr[self.hash]['status'] = 6
        self.assertEqual(self.drain()['status'], 'completed')

    def test_lost_add_receipt_reads_existing_target_without_readding(self):
        self.api.faults['torrent-add'] = ['lost']
        self.begin()
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertEqual(sum(method == 'torrent-add' for method, _ in self.api.calls), 1)
        self.assertFalse(any(method == 'torrent-verify' for method, _ in self.api.calls))

    def test_restart_preserves_native_mode_and_does_not_start_full_verify(self):
        self.api.native_status = 2
        self.begin()
        self.manager.tick()
        self.manager = self.make_manager()
        self.assertEqual(self.manager.state['job']['verification_mode'], 'native')
        self.api.tr[self.hash]['status'] = 6
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertEqual(sum(method == 'torrent-add' for method, _ in self.api.calls), 1)
        self.assertFalse(any(method == 'torrent-verify' for method, _ in self.api.calls))

    def test_lost_source_remove_receipt_is_reconciled_without_repeating_remove(self):
        self.api.faults['qremove'] = ['lost']
        self.begin()
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertEqual(sum(method == 'qremove' for method, _ in self.api.calls), 1)
        self.assertEqual(self.api.tr[self.hash]['status'], 6)

    def test_paused_completed_source_still_starts_target_seeding(self):
        self.api.qb[self.hash]['state'] = 'stoppedUP'
        self.begin()
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertEqual(self.api.tr[self.hash]['status'], 6)

    def test_cancel_during_native_check_restores_source_and_pauses_new_target(self):
        self.api.native_status = 2
        self.begin()
        self.manager.tick()
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        job = self.drain()
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.api.tr[self.hash]['status'], 0)
        self.assertFalse(any(method in ('qremove', 'torrent-verify') for method, _ in self.api.calls))

    def test_native_timeout_keeps_source_and_recovers_original_state(self):
        self.api.native_status = 2
        self.begin()
        self.manager.tick()
        self.now += management.VERIFY_TIMEOUT + 1
        job = self.drain()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.api.tr[self.hash]['status'], 0)
        self.assertFalse(any(method in ('qremove', 'torrent-verify') for method, _ in self.api.calls))

    def test_source_completion_change_before_handoff_prevents_removal(self):
        self.begin()
        self.manager.tick()
        self.api.qb[self.hash].update(progress=.5, completed=50, amount_left=50)
        job = self.drain()
        self.assertEqual(job['status'], 'failed')
        self.assertIn(self.hash, self.api.qb)
        self.assertFalse(any(method == 'qremove' for method, _ in self.api.calls))

    def test_target_queue_after_source_removal_waits_for_actual_seeding(self):
        remove = self.api.qremove
        def removed_then_queued(value):
            remove(value)
            self.api.tr[value]['status'] = 5
        self.api.qremove = removed_then_queued
        self.begin()
        job = self.drain()
        self.assertEqual(job['status'], 'waiting')
        self.assertNotIn(self.hash, self.api.qb)
        self.assertEqual(sum(method == 'qremove' for method, _ in self.api.calls), 1)
        self.now += management.VERIFY_TIMEOUT + 1  # After handoff, recovery must keep reading past the initial timeout.
        self.api.tr[self.hash]['status'] = 6
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertEqual(sum(method == 'qremove' for method, _ in self.api.calls), 1)

    def test_delayed_lost_remove_receipt_and_restart_never_resends_removal(self):
        def delayed_remove(value):
            self.api.calls.append(('qremove', value))
            raise downloads.ManagementError('virtual lost removal receipt', 502)
        self.api.qremove = delayed_remove
        self.begin()
        self.drain()
        self.assertIn(self.hash, self.api.qb)
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'removing_source')
        self.manager = self.make_manager()
        self.now += management.VERIFY_TIMEOUT + 1
        self.drain()
        self.assertEqual(self.manager.state['job']['status'], 'waiting')
        self.assertEqual(sum(method == 'qremove' for method, _ in self.api.calls), 1)
        self.api.qb.pop(self.hash)
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertEqual(sum(method == 'qremove' for method, _ in self.api.calls), 1)
        self.assertFalse(any(method == 'torrent-verify' for method, _ in self.api.calls))

    def test_reappearing_source_after_confirmed_handoff_is_never_touched(self):
        remove = self.api.qremove
        def removed_then_queued(value):
            remove(value)
            self.api.tr[value]['status'] = 5
        self.api.qremove = removed_then_queued
        self.begin()
        self.assertEqual(self.drain()['status'], 'waiting')
        self.assertTrue(self.manager.state['job']['items'][0]['source_removed'])
        reappeared = qb_task(self.hash, size=100, save_path='/downloads/another', progress=.5,
                            completed=50, amount_left=50, state='downloading')
        self.api.qb[self.hash] = reappeared
        read = self.api.qget
        def no_source_read(endpoint, arguments=None):
            if endpoint == 'torrents/info' and arguments and arguments.get('hashes') == self.hash:
                self.fail('A completed handoff must not read or act on a later same-hash source')
            return read(endpoint, arguments)
        self.api.qget = no_source_read
        self.manager = self.make_manager()
        self.now += management.VERIFY_TIMEOUT + 1
        self.api.tr[self.hash]['status'] = 6
        before = list(self.api.calls)
        self.assertEqual(self.drain()['status'], 'completed')
        self.assertIs(self.api.qb[self.hash], reappeared)
        self.assertFalse(any(method in ('qremove', 'qstop', 'qstart') for method, _ in self.api.calls[len(before):]))
        self.assertEqual(sum(method == 'qremove' for method, _ in self.api.calls), 1)

    def omit_target_error(self):
        read = self.api.rpc
        def missing_error(method, arguments=None):
            result = read(method, arguments)
            if method == 'torrent-get':
                for row in result['torrents']:
                    row.pop('error', None)
            return result
        self.api.rpc = missing_error

    def test_missing_error_during_initial_takeover_keeps_source(self):
        self.omit_target_error()
        self.begin()
        job = self.drain(rounds=20)
        self.assertEqual(job['status'], 'failed')
        self.assertIn(self.hash, self.api.qb)
        self.assertFalse(any(method == 'qremove' for method, _ in self.api.calls))

    def test_missing_error_before_handoff_keeps_source(self):
        self.begin()
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'verified')
        self.omit_target_error()
        job = self.drain()
        self.assertEqual(job['status'], 'failed')
        self.assertIn(self.hash, self.api.qb)
        self.assertFalse(any(method == 'qremove' for method, _ in self.api.calls))

    def test_legacy_saved_job_continues_its_original_full_check(self):
        self.begin()
        self.manager.state['job'].pop('verification_mode', None)
        self.manager.save()
        self.manager = self.make_manager()
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['items'][0]['phase'], 'verifying')
        self.assertEqual(sum(method == 'torrent-verify' for method, _ in self.api.calls), 1)
        self.assertIn(self.hash, self.api.qb)


if __name__ == '__main__':
    unittest.main()
