"""Offline regressions. Run: python -m unittest -v test_seedkeep_pull."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
import unittest
from unittest.mock import patch
import seedkeep_pull as pull
from torrent_meta import MetadataError, torrent_metadata


def encode(value):
    if type(value) is int:
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(encode(v) for v in value) + b'e'
    return b'd' + b''.join(encode(k) + encode(value[k]) for k in sorted(value)) + b'e'


def torrent(size, name=b'x'):
    return encode({b'info': {b'name': name, b'length': size, b'piece length': 16384, b'pieces': b'p' * 20}})


class FakeClients:
    def __init__(self, hashes):
        self.qb = {hashes['1']: {'hash': hashes['1'], 'total_size': 20, 'tags': 'old', 'category': 'old', 'save_path': '/old'}}
        self.tr = {hashes['2']: {'hashString': hashes['2'], 'totalSize': 30, 'labels': []}}
        self.added = []
        self.fault = None
        self.move_on_add = False
        self.rpc_failure = False

    def snapshot(self):
        if self.rpc_failure:
            raise pull.PullError('simulated_rpc_failure')
        return list(copy.deepcopy(self.qb).values()), copy.deepcopy(self.tr)

    def add(self, data):
        meta = torrent_metadata(data)
        value = meta['hashes'][0][:40]
        if self.fault != 'missing':
            self.qb[value] = {'hash': value, 'total_size': meta['size'], 'tags': 'new,pts保种组', 'category': 'keep', 'save_path': '/download'}
        self.added.append(value)
        if self.move_on_add:
            self.tr[value] = {'hashString': value, 'totalSize': self.qb.pop(value)['total_size'], 'labels': ['new', 'pts保种组']}
        if self.fault:
            self.fault = None
            raise pull.PullError('simulated_lost_add_response')

    def qb_get(self, operation):
        return [copy.deepcopy(v) for v in self.qb.values() if v['hash'] in operation]


class PullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.data = {str(i): torrent(n, str(i).encode()) for i, n in enumerate((20, 30, 40, 50, 60, 100, 70, 80), 1)}
        self.hashes = {i: torrent_metadata(v)['hashes'][0] for i, v in self.data.items()}
        self.clients = FakeClients(self.hashes)
        self.settings = {'mode': 'maintain', 'target': 2, 'max_per_run': 2, 'max_bytes': 100,
                         'min_seeders': 2, 'max_seeders': 6, 'historical_config': 'unused'}
        self.source = {'download_path': '/download', 'category': 'keep', 'tag': 'new', 'keep_torrent': False}
        # Empty GUID deliberately exercises metainfo dedup, not just advertised hashes.
        self.choices = {i: {'id': i, 'url': i, 'hash': '', 'size': 1, 'seeders': 2} for i in self.data}
        self.downloader = type('Download', (), {'get': lambda _, c: (self.data[c['url']], torrent_metadata(self.data[c['url']]), None)})()

    def run_pull(self, pool_reader=None, *, allowance=None):
        with patch.object(pull, 'history_hashes', return_value={self.hashes['3']}), patch.object(pull.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            pull.run(self.settings, self.source, self.directory, clients=self.clients,
                     pool_reader=pool_reader or (lambda *args: (copy.deepcopy(self.choices), time.time())), downloader=self.downloader, allowance=allowance)
        return json.loads((self.directory / 'status.json').read_text())

    def test_effective_allowance_adds_past_local_task_target_without_rewriting_target(self):
        self.run_pull()
        self.assertEqual(len(self.state()['accepted']), 2)
        self.settings['target'] = 1
        result = self.run_pull(allowance=1)
        self.assertEqual(result['added_this_run'], 1)
        self.assertEqual(len(self.state()['accepted']), 3)
        self.assertEqual((result['target'], result['refill_allowance']), (1, 1))
        self.assertEqual(self.settings['target'], 1)

    def test_effective_allowance_still_respects_per_run_limit(self):
        result = self.run_pull(allowance=5)
        self.assertEqual((result['added_this_run'], result['refill_allowance']), (2, 5))

    def test_invalid_effective_allowance_is_rejected_before_any_add_or_state_write(self):
        for allowance in (True, 0, -1, 1001, 1.5, '1'):
            with self.subTest(allowance=allowance), self.assertRaises(pull.PullError):
                self.run_pull(allowance=allowance)
        self.assertEqual(self.clients.added, [])
        self.assertFalse((self.directory / 'batch.json').exists())

    def state(self):
        return json.loads((self.directory / 'batch.json').read_text())

    def test_size_and_seeder_boundaries(self):
        settings = {**self.settings, 'max_bytes': 500 * 1024 * 1024}
        for seeders in range(10):
            self.assertEqual(pull.qualified(settings['max_bytes'] - 1, seeders, settings), 2 <= seeders <= 6)
        for size in (0, -1, settings['max_bytes'], settings['max_bytes'] + 1, '50', True):
            self.assertFalse(pull.qualified(size, 2, settings))
        self.assertFalse(pull.qualified(50, True, settings))

    def test_full_metainfo_size_and_hash(self):
        info = {b'name': b'multi', b'files': [{b'length': 300, b'path': [b'a']}, {b'length': 300, b'path': [b'b']}], b'pieces': b'x' * 20}
        result = torrent_metadata(encode({b'info': info}))
        self.assertEqual(result['size'], 600)
        self.assertEqual(result['hashes'][0], hashlib.sha1(encode(info)).hexdigest())
        for value in (torrent(-1), torrent(5) + b'garbage'):
            with self.assertRaises(MetadataError):
                torrent_metadata(value)

    def test_v2_metainfo_size_and_alias_count(self):
        info = {b'meta version': 2, b'file tree': {b'a': {b'': {b'length': 90}}, b'b': {b'': {b'length': 80}}}}
        result = torrent_metadata(encode({b'info': info}))
        self.assertEqual(result['size'], 170)
        state = {'accepted': {'1': {'hash': result['hashes'][1], 'hashes': result['hashes']}}}
        counts = pull.managed_counts(state, [{'hash': result['hashes'][0]}], {result['hashes'][1]: {}})
        self.assertEqual(counts['managed_active'], 1)
        self.assertEqual(counts['managed_both'], 1)

    def test_full_target_skips_pool_and_remembers_seen_jobs(self):
        self.run_pull()
        self.clients.qb[self.hashes['8']] = {'hash': self.hashes['8'], 'total_size': 80, 'tags': ''}
        result = self.run_pull(lambda *args: self.fail('full target must skip site requests'))
        self.assertEqual(result['added_this_run'], 0)
        self.assertIn(self.hashes['8'], self.state()['seen_hashes'])
        self.assertEqual(self.clients.added, [self.hashes['4'], self.hashes['5']])

    def test_refill_budget_per_run_and_no_overshoot(self):
        self.settings.update(target=3, max_per_run=1)
        for expected in (1, 2, 3):
            result = self.run_pull()
            self.assertEqual(result['added_this_run'], 1)
            self.assertEqual(result['managed_active'], expected)
        self.run_pull(lambda *args: self.fail('filled target'))
        self.assertEqual(self.clients.added, [self.hashes['4'], self.hashes['5'], self.hashes['7']])

    def test_migrated_tr_jobs_and_overlap_count_once(self):
        self.run_pull()
        h = self.hashes['4']
        self.clients.tr[h] = {'hashString': h, 'totalSize': self.clients.qb[h]['total_size'], 'labels': ['pts保种组']}
        result = self.run_pull(lambda *args: self.fail('overlap should count once'))
        self.assertEqual(result['managed_active'], 2)
        self.assertEqual(result['managed_both'], 1)
        self.clients.qb.pop(h)
        result = self.run_pull(lambda *args: self.fail('TR must continue counting'))
        self.assertEqual((result['managed_qb'], result['managed_tr']), (1, 1))

    def test_deleted_jobs_refill_with_new_hashes_only(self):
        self.run_pull()
        self.clients.qb.pop(self.hashes['4'])
        self.choices['0'] = {**self.choices['4'], 'id': '0', 'url': '4'}
        result = self.run_pull()
        self.assertEqual(result['managed_active'], 2)
        self.assertEqual(len(self.state()['accepted']), 3)
        self.assertEqual(self.clients.added, [self.hashes['4'], self.hashes['5'], self.hashes['7']])
        self.assertEqual(len(set(self.clients.added)), 3)

    def test_changed_filters_keep_removed_hashes_permanently_excluded(self):
        self.run_pull()
        original = self.state()
        self.clients.qb.pop(self.hashes['4'])
        self.settings.update(max_bytes=90, min_seeders=1, max_seeders=8, allow_filter_changes=True)
        self.run_pull()
        state = self.state()
        self.assertEqual(state['criteria'], original['criteria'])
        self.assertEqual(state['active_criteria']['max_bytes'], 90)
        self.assertEqual(state['baseline'], original['baseline'])
        self.assertTrue(set(original['seen_hashes']) <= set(state['seen_hashes']))
        self.assertEqual(self.clients.added.count(self.hashes['4']), 1)
        self.assertIn(self.hashes['7'], self.clients.added)

    def test_credentials_file_avoids_docker_access(self):
        path = self.directory / 'tr.json'
        path.write_text(json.dumps({'username': 'test-tr', 'password': 'test-password'}))
        with patch.object(pull.subprocess, 'run', side_effect=AssertionError('docker must not be used')):
            client = pull.Clients({'host': 'localhost', 'port': 8080}, {**self.settings, 'tr_credentials_file': str(path)})
        self.assertTrue(client.tr_headers['Authorization'].startswith('Basic '))

    def test_baseline_history_survives_history_file_changes(self):
        self.run_pull()
        self.clients.qb.pop(self.hashes['4'])
        with patch.object(pull, 'history_hashes', return_value=set()), contextlib.redirect_stdout(io.StringIO()):
            pull.run(self.settings, self.source, self.directory, clients=self.clients,
                     pool_reader=lambda *args: (self.choices, time.time()), downloader=self.downloader)
        self.assertNotIn(self.hashes['3'], self.clients.added)

    def test_seen_unmanaged_task_removed_from_clients_stays_excluded(self):
        self.run_pull()
        self.clients.qb[self.hashes['7']] = {'hash': self.hashes['7'], 'total_size': 70, 'tags': ''}
        self.run_pull()
        self.clients.qb.pop(self.hashes['7'])
        self.clients.qb.pop(self.hashes['4'])
        self.run_pull()
        self.assertEqual(self.clients.added[-1], self.hashes['8'])
        self.assertNotIn(self.hashes['7'], self.clients.added)

    def test_lost_response_recovers_from_tr_without_duplicate(self):
        self.clients.fault = 'after_add'
        with self.assertRaises(pull.PullError):
            self.run_pull()
        h = self.hashes['4']
        self.clients.tr[h] = {'hashString': h, 'totalSize': self.clients.qb.pop(h)['total_size'], 'labels': ['pts保种组']}
        self.run_pull()
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual(self.clients.added, [self.hashes['4'], self.hashes['5']])

    def test_missing_uncertain_add_expires_but_hash_stays_excluded(self):
        self.settings['pending_grace_seconds'] = 0
        self.clients.fault = 'missing'
        with self.assertRaises(pull.PullError):
            self.run_pull()
        result = self.run_pull()
        self.assertEqual(result['managed_active'], 2)
        self.assertIn('4', self.state()['unconfirmed'])
        self.assertEqual(self.clients.added, [self.hashes['4'], self.hashes['5'], self.hashes['7']])

    def test_recent_pending_reserves_capacity(self):
        self.settings['target'] = 1
        self.clients.fault = 'missing'
        with self.assertRaises(pull.PullError):
            self.run_pull()
        result = self.run_pull(lambda *args: self.fail('pending must reserve the slot'))
        self.assertEqual(result['pending_reserved'], 1)
        self.assertEqual(len(self.clients.added), 1)

    def test_pending_wrong_size_and_rpc_failure_stop_adds(self):
        self.clients.fault = 'after_add'
        with self.assertRaises(pull.PullError):
            self.run_pull()
        self.clients.qb[self.hashes['4']]['total_size'] = 99
        with self.assertRaisesRegex(pull.PullError, 'size_mismatch'):
            self.run_pull()
        self.clients.rpc_failure = True
        with self.assertRaisesRegex(pull.PullError, 'rpc_failure'):
            self.run_pull()
        self.assertEqual(len(self.clients.added), 1)

    def test_failed_pending_validation_preserves_newly_seen_hashes(self):
        self.clients.fault = 'after_add'
        with self.assertRaises(pull.PullError):
            self.run_pull()
        self.clients.qb[self.hashes['7']] = {'hash': self.hashes['7'], 'total_size': 70}
        self.clients.qb[self.hashes['4']]['total_size'] = 99
        with self.assertRaisesRegex(pull.PullError, 'size_mismatch'):
            self.run_pull()
        self.assertIn(self.hashes['7'], self.state()['seen_hashes'])
        self.clients.qb.pop(self.hashes['7'])
        self.clients.qb[self.hashes['4']]['total_size'] = 50
        self.run_pull()
        self.clients.qb.pop(self.hashes['4'])
        self.run_pull()
        self.assertEqual(self.clients.added[-1], self.hashes['8'])
        self.assertNotIn(self.hashes['7'], self.clients.added)

    def test_immediate_qb_to_tr_transfer_is_verified(self):
        self.clients.move_on_add = True
        result = self.run_pull()
        self.assertEqual(result['managed_tr'], 2)
        self.assertEqual(result['managed_qb'], 0)
        self.assertEqual(self.state()['pending'], {})

    def test_target_can_change_without_losing_original_batch(self):
        self.run_pull()
        baseline = copy.deepcopy(self.state()['baseline'])
        original = copy.deepcopy(self.state()['accepted'])
        self.settings['target'] = 3
        self.run_pull()
        self.assertEqual(self.state()['criteria']['target'], 2)
        self.assertEqual(self.state()['baseline'], baseline)
        for key, record in original.items():
            self.assertEqual(self.state()['accepted'][key], record)
        self.settings['max_bytes'] += 1
        with self.assertRaisesRegex(pull.PullError, 'criteria_changed'):
            self.run_pull()

    def test_empty_pool_finishes_for_next_scheduled_retry(self):
        result = self.run_pull(lambda *args: ({}, time.time()))
        self.assertEqual(result['stop_reason'], 'candidate_pool_exhausted')
        self.assertEqual(result['run_outcome'], 'success')
        self.assertEqual(self.clients.added, [])

    def test_batch_mode_preserves_cumulative_completion(self):
        self.settings['mode'] = 'batch'
        self.run_pull()
        self.clients.qb.pop(self.hashes['4'])
        result = self.run_pull(lambda *args: self.fail('batch completion is cumulative'))
        self.assertTrue(result['finished'])
        self.assertEqual(len(self.clients.added), 2)

    def test_metadata_identity_mismatch_prevents_add(self):
        self.choices = {'4': {**self.choices['4'], 'hash': 'a' * 40}}
        with self.assertRaisesRegex(pull.PullError, 'identity_mismatch'):
            self.run_pull()
        self.assertEqual(self.clients.added, [])
        self.assertEqual(self.state()['pending'], {})

    def test_invalid_budgets_and_schedule(self):
        for changed in ({'max_per_run': 0}, {'target': True}, {'min_seeders': 7}, {'interval_hours': 5}, {'cron_minute': 60}):
            with self.assertRaises(pull.PullError):
                pull.validate_settings({**self.settings, **changed})

    @unittest.skipUnless(os.name == 'posix', 'fnOS file locking')
    def test_actual_file_lock_prevents_second_runner(self):
        import fcntl
        with (self.directory / 'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(pull.main(['--config', str(self.directory / 'settings.json')]), 0)
            self.assertIn('already_running', output.getvalue())
            self.assertFalse((self.directory / 'batch.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'fnOS entrypoint')
    def test_entrypoint_records_failure_and_rotates_log(self):
        config = self.directory / 'settings.json'
        source = self.directory / 'source.json'
        source.write_text('{}')
        config.write_text(json.dumps({**self.settings, 'source_config': str(source)}))
        (self.directory / 'run.log').write_bytes(b'x' * (5 * 1024 * 1024))
        with patch.object(pull, 'run', side_effect=pull.PullError('simulated_failure')):
            self.assertEqual(pull.main(['--config', str(config), '--log']), 1)
        self.assertEqual(json.loads((self.directory / 'status.json').read_text())['last_error'], 'simulated_failure')
        self.assertTrue((self.directory / 'run.log.1').exists())
        self.assertLess((self.directory / 'run.log').stat().st_size, 1024)


if __name__ == '__main__':
    unittest.main()
