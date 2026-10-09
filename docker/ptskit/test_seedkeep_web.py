"""Offline web, scheduler, authentication and persistence acceptance tests."""
import contextlib
import copy
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import seedkeep_web as web
import seedkeep_pull as pull
from test_seedkeep_pts import sample_payload
from test_seedkeep_management import FakeAPI, torrent, NOW, qb_task


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = self.directory / 'docker_settings.json'
        source = self.directory / 'source.json'
        source.write_text(json.dumps({'username': 'test-user', 'password': 'test-password', 'token': 'test-secret', 'category': 'keep', 'tag': 'keep', 'download_path': '/download'}))
        settings = {'source_config': str(source), 'target': 700, 'max_per_run': 50, 'max_bytes': 524288000,
                    'min_seeders': 2, 'max_seeders': 6, 'mode': 'maintain', 'interval_hours': 2, 'cron_minute': 17}
        self.config.write_text(json.dumps(settings))
        self.now = time.mktime(time.strptime('2026-10-02 22:18:00', '%Y-%m-%d %H:%M:%S'))
        self.spawned = []
        self.process = type('FakeProcess', (), {'poll': lambda _: None})()
        def spawn(*args, **kwargs):
            self.spawned.append(args[0])
            return self.process
        self.controller = web.Controller(self.config, spawn=spawn, clock=lambda: self.now)
        self.lock_patch = patch.object(self.controller, 'file_lock', side_effect=contextlib.nullcontext)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

    def test_filter_save_keeps_all_dedup_records_and_internal_paths(self):
        records = {'accepted': {'1': {'hash': 'a' * 40}}, 'seen_hashes': ['b' * 40], 'baseline': {'tr_hashes': ['c' * 40]}}
        batch = self.directory / 'batch.json'
        batch.write_text(json.dumps(records))
        before = batch.read_bytes()
        self.controller.update_settings({'min_seeders': 3, 'max_seeders': 8, 'max_size_mib': 250.5})
        settings = self.controller.settings()
        self.assertEqual(settings['max_bytes'], int(250.5 * 1048576))
        self.assertEqual((settings['min_seeders'], settings['max_seeders']), (3, 8))
        self.assertTrue(settings['allow_filter_changes'])
        self.assertEqual(batch.read_bytes(), before)
        self.assertEqual(settings['source_config'], str(self.directory / 'source.json'))

    def test_invalid_filters_are_atomic_and_cannot_change_credentials(self):
        before = self.config.read_bytes()
        for values in ({'min_seeders': 7, 'max_seeders': 6}, {'max_size_mib': 0}, {'max_size_mib': float('nan')}, {'target': True}, {'source_config': '/elsewhere'}, {'interval_hours': 5}):
            with self.assertRaises(web.ApiError):
                self.controller.update_settings(values)
            self.assertEqual(self.config.read_bytes(), before)

    def test_only_one_runner_and_filter_change_rejected_during_run(self):
        self.controller.start_run()
        with self.assertRaises(web.ApiError) as error:
            self.controller.start_run()
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(web.ApiError):
            self.controller.update_settings({'target': 701})
        self.assertEqual(len(self.spawned), 1)

    def test_disable_future_runs_does_not_interrupt_current_run(self):
        self.controller.automation(True)
        self.controller.start_run()
        self.controller.automation(False)
        self.assertTrue(self.controller.running())
        self.assertIsNone(self.controller.runtime['next_run_at'])

    def test_restart_keeps_automatic_toggle_and_next_run(self):
        self.controller.automation(True)
        expected = copy.deepcopy(self.controller.runtime)
        restarted = web.Controller(self.config, clock=lambda: self.now)
        self.assertEqual(restarted.runtime, expected)
        self.assertEqual(time.strftime('%H:%M', time.localtime(expected['next_run_at'])), '00:17')
        self.controller.automation(False)
        self.assertFalse(web.Controller(self.config).runtime['automatic_enabled'])

    def test_due_scheduler_runs_once_and_advances_persistent_time(self):
        self.controller.automation(True)
        self.now = self.controller.runtime['next_run_at']
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(len(self.spawned), 1)
        self.assertGreater(self.controller.runtime['next_run_at'], self.now)
        self.assertEqual(web.load_json(self.controller.runtime_path), self.controller.runtime)

    def test_schedule_changes_recompute_next_slot(self):
        self.controller.automation(True)
        self.controller.update_settings({'interval_hours': 1, 'cron_minute': 30})
        self.assertEqual(time.strftime('%H:%M', time.localtime(self.controller.runtime['next_run_at'])), '22:30')

    def test_signed_session_tampering_expiry_and_login_limits(self):
        token = self.controller.login({'username': 'test-user', 'password': 'test-password'}, 'test')
        self.assertTrue(self.controller.authenticated(token))
        self.assertFalse(self.controller.authenticated(token[:-2] + '00'))
        self.now += 43201
        self.assertFalse(self.controller.authenticated(token))
        for _ in range(10):
            with self.assertRaises(web.ApiError):
                self.controller.login({'username': 'bad', 'password': 'bad'}, 'other')
        with self.assertRaises(web.ApiError) as error:
            self.controller.login({}, 'other')
        self.assertEqual(error.exception.status, 429)

    def test_transmission_credentials_file_is_the_actual_web_account(self):
        credentials = self.directory / 'tr_credentials.json'
        credentials.write_text(json.dumps({'username': 'tr-user', 'password': 'tr-password'}))
        settings = self.controller.settings()
        settings['tr_credentials_file'] = str(credentials)
        pull.save_json(self.config, settings)
        token = self.controller.login({'username': 'tr-user', 'password': 'tr-password'}, 'tr-test')
        self.assertTrue(self.controller.authenticated(token))
        with self.assertRaises(web.ApiError):
            self.controller.login({'username': 'test-user', 'password': 'test-password'}, 'tr-test')

    def test_tasks_include_transmission_name_progress_and_removed_records(self):
        state = {'accepted': {'1': {'hash': 'a' * 40, 'size': 100, 'seeders_at_pull': 2, 'added_at': 1},
                              '2': {'hash': 'b' * 40, 'size': 200, 'seeders_at_pull': 3, 'added_at': 2}}}
        pull.save_json(self.directory / 'batch.json', state)
        with patch.object(self.controller, 'snapshot', return_value=([], {'a' * 40: {'name': 'TR task', 'percentDone': 1}}, {})):
            rows = {row['id']: row for row in self.controller.tasks()['items']}
        self.assertEqual((rows['1']['name'], rows['1']['location'], rows['1']['progress']), ('TR task', 'TR', 1))
        self.assertEqual(rows['2']['location'], '已移除')

    def test_status_inventory_failure_never_reports_zero_or_stale_totals(self):
        value = 'a' * 40
        pull.save_json(self.directory / 'batch.json', {'accepted': {'1': {'hash': value}}})
        before = (self.directory / 'batch.json').read_bytes()
        good = ([{'hash': value}], {value: {'hashString': value}},
                {'qb': True, 'tr': True, 'error': None})
        for cache in (None, good):
            with self.subTest(cached=cache is not None):
                self.controller.cache = cache
                self.controller.cache_at = self.now - 3600
                with patch('seedkeep_web.pull.Clients') as clients:
                    clients.return_value.snapshot.side_effect = pull.PullError('offline')
                    result = self.controller.status()
                self.assertIsNotNone(result['connection']['error'])
                self.assertIsNone(result['qb_total'])
                self.assertIsNone(result['tr_total'])
                self.assertIsNone(result['managed_active'])
                self.assertIsNone(result['seedkeep']['total'])
                self.assertIsNone(result['remaining'])
                self.assertEqual((self.directory / 'batch.json').read_bytes(), before)

    def test_status_and_logs_never_disclose_credentials(self):
        with patch.object(self.controller, 'snapshot', return_value=([], {}, {'qb': True, 'tr': True, 'error': None})):
            value = self.controller.status()
        self.assertNotIn('test-secret', json.dumps(value))
        self.assertNotIn('test-password', json.dumps(value))
        (self.directory / 'run.log').write_text(json.dumps({'event': 'progress', 'token': 'test-secret', 'url': 'private'}) + '\n')
        self.assertEqual(self.controller.logs()['items'], [{'event': 'progress', 'level': 'info'}])

    def test_site_statistics_monitor_keeps_frequency_and_automation_unchanged(self):
        settings = self.controller.settings()
        settings['pts_cache_seconds'] = 1800
        pull.save_json(self.config, settings)
        runtime = copy.deepcopy(self.controller.runtime)
        with patch.object(self.controller.pts, 'fetcher', return_value=sample_payload()) as fetcher, \
                patch.object(self.controller.pts, 'rss_fetcher', side_effect=AssertionError('RSS must not be queried')), \
                patch.object(self.controller, 'snapshot', side_effect=AssertionError('Inventory must not be queried')), \
                patch.object(self.controller.fleet, '_tick', side_effect=AssertionError('Automation must not run')):
            self.controller.refresh_site_statistics()
            self.controller.refresh_site_statistics()
            self.assertEqual(fetcher.call_count, 1)
            self.now += 1799
            self.controller.refresh_site_statistics()
            self.assertEqual(fetcher.call_count, 1)
            self.now += 1
            self.controller.refresh_site_statistics()
            self.assertEqual(fetcher.call_count, 2)
            self.assertEqual(self.controller.pts.statistics()['current'], 439)
        self.assertEqual(self.controller.runtime, runtime)
        self.assertEqual(self.spawned, [])
        restarted = web.Controller(self.config, clock=lambda: self.now)
        self.assertEqual(restarted.pts.statistics()['current'], 439)
        self.assertEqual(restarted.runtime, runtime)

    def test_actual_http_auth_csrf_validation_and_static_whitelist(self):
        server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        server.controller = self.controller
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        def request(method, path, value=None, cookie=None, csrf=True, origin=None):
            headers = {'Content-Type': 'application/json'}
            if csrf:
                headers['X-Seedkeep-Request'] = '1'
            if cookie:
                headers['Cookie'] = cookie
            if origin:
                headers['Origin'] = origin
            connection.request(method, path, json.dumps(value) if value is not None else None, headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data, response.getheader('Set-Cookie')
        self.assertEqual(request('GET', '/healthz')[0], 200)
        self.assertEqual(request('GET', '/api/status')[0], 401)
        self.assertEqual(request('GET', '/api/pts')[0], 401)
        self.assertEqual(request('GET', '/api/pts/statistics')[0], 401)
        self.assertEqual(request('POST', '/api/pts/refresh', {})[0], 401)
        self.assertEqual(request('GET', '/seedkeep_config.json')[0], 401)
        self.assertEqual(request('POST', '/api/login', {}, csrf=False)[0], 403)
        code, _, cookie = request('POST', '/api/login', {'username': 'test-user', 'password': 'test-password'})
        self.assertEqual(code, 200)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        with patch.object(self.controller, 'snapshot', return_value=([], {}, {'qb': True, 'tr': True, 'error': None})):
            code, data, _ = request('GET', '/api/status', cookie=cookie)
            self.assertEqual(code, 200)
            self.assertNotIn(b'test-secret', data)
        with patch.object(self.controller, 'snapshot', side_effect=AssertionError('Cache endpoint must not read inventory')), \
                patch.object(self.controller.pts, 'fetcher', side_effect=AssertionError('Cache endpoint must not query PTS')):
            code, data, _ = request('GET', '/api/pts/statistics', cookie=cookie)
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(data)['available'])
            self.assertNotIn(b'test-secret', data)
        with patch.object(self.controller.pts, 'fetcher', return_value=sample_payload()) as fetcher:
            code, data, _ = request('GET', '/api/pts', cookie=cookie)
            self.assertEqual(code, 200)
            site = json.loads(data)
            self.assertEqual((site['current'], site['target'], site['missing']), (439, 1000, 561))
            self.assertNotIn(b'download_url', data)
            self.assertNotIn(b'private-token', data)
            request('GET', '/api/pts', cookie=cookie)
            self.assertEqual(fetcher.call_count, 1)
            code, data, _ = request('GET', '/api/pts/statistics', cookie=cookie)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(data)['current'], 439)
            self.assertNotIn(b'items', data)
            self.assertEqual(fetcher.call_count, 1)
            self.assertEqual(request('POST', '/api/pts/refresh', {}, cookie=cookie, csrf=False)[0], 403)
            self.assertEqual(request('POST', '/api/pts/refresh', {}, cookie=cookie, origin='http://other.example')[0], 403)
            self.assertEqual(request('POST', '/api/pts/refresh', {}, cookie=cookie)[0], 200)
            self.assertEqual(fetcher.call_count, 2)
            request('POST', '/api/settings', {'min_seeders': 3}, cookie=cookie)
            _, data, _ = request('GET', '/api/pts', cookie=cookie)
            self.assertEqual(json.loads(data)['filter_matches'], 1)
            self.assertEqual(fetcher.call_count, 2)
            fetcher.side_effect = TimeoutError('test-secret')
            _, data, _ = request('POST', '/api/pts/refresh', {}, cookie=cookie)
            self.assertTrue(json.loads(data)['stale'])
            self.assertEqual(json.loads(data)['current'], 439)
            self.assertNotIn(b'test-secret', data)
        self.assertEqual(request('POST', '/api/settings', {'max_size_mib': 0}, cookie=cookie)[0], 400)
        self.assertEqual(request('POST', '/api/settings', {}, cookie=cookie, origin='http://other.example')[0], 403)
        self.assertEqual(request('POST', '/api/automation', {'enabled': True}, cookie=cookie)[0], 200)
        self.assertEqual(request('POST', '/api/logout', {}, cookie=cookie)[0], 200)

    def test_management_http_endpoints_require_auth_csrf_origin_and_confirmations(self):
        api = FakeAPI(torrent(100))
        self.controller.management.client_factory = lambda *args: api
        self.controller.management.pts = lambda: {'available': True, 'seeders_max': 10}
        server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        server.controller = self.controller
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        cookie = None
        def request(method, path, body=None, csrf=True, origin=None, authenticated=True):
            headers = {'Content-Type': 'application/json'}
            if csrf:
                headers['X-Seedkeep-Request'] = '1'
            if cookie and authenticated:
                headers['Cookie'] = cookie
            if origin:
                headers['Origin'] = origin
            connection.request(method, '/api/' + path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader('Set-Cookie')
        for path in ('downloaders', 'limits'):
            self.assertEqual(request('GET', path)[0], 401)
        endpoints = ('downloaders/refresh', 'limits/refresh', 'cleanup/settings', 'cleanup/delete', 'transfer', 'transfer/cancel', 'limits')
        for path in endpoints:
            self.assertEqual(request('POST', path, {})[0], 401)
        code, _, cookie = request('POST', 'login', {'username': 'test-user', 'password': 'test-password'})
        self.assertEqual(code, 200)
        for path in endpoints:
            self.assertEqual(request('POST', path, {}, csrf=False)[0], 403)
            self.assertEqual(request('POST', path, {}, origin='http://elsewhere.invalid')[0], 403)
        for path in ('downloaders', 'limits'):
            self.assertEqual(request('GET', path)[0], 200)
            self.assertEqual(request('POST', path + '/refresh', {})[0], 200)
        self.assertEqual(request('POST', 'cleanup/settings', {'cleanup_enabled': False, 'cleanup_wait_hours': 36, 'cleanup_max_per_run': 5})[0], 200)
        self.assertEqual(request('POST', 'limits', {'client': 'qb', 'upload_kib': 123, 'download_kib': 0})[0], 200)
        self.assertEqual(request('POST', 'cleanup/delete', {'hashes': [api.hash], 'confirm': 'REMOVE_TASKS_KEEP_DATA'})[0], 409)
        self.assertEqual(request('POST', 'transfer', {'hashes': [api.hash], 'confirm': 'wrong'})[0], 400)
        code, result, _ = request('POST', 'transfer', {'hashes': [api.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.assertEqual(code, 200)
        self.assertNotIn('source', result['job']['items'][0])
        self.assertEqual(request('POST', 'transfer', {'hashes': [api.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})[0], 409)
        self.assertEqual(request('POST', 'transfer/cancel', {'job_id': result['job']['id']})[0], 200)
        self.controller.management.tick()
        self.assertEqual(self.controller.management.state['job']['status'], 'cancelled')
        self.assertEqual(len(api.qb), 1)
        self.assertFalse(api.tr)

    def test_management_mutation_respects_active_runner(self):
        api = FakeAPI(torrent(100))
        self.controller.management.client_factory = lambda *args: api
        self.controller.management.pts = lambda: {'available': True, 'seeders_max': 10}
        self.controller.start_run()
        with self.assertRaises(web.ApiError):
            self.controller.management.update_policy({'cleanup_enabled': False, 'cleanup_wait_hours': 24, 'cleanup_max_per_run': 20})
        with self.assertRaises(web.ApiError):
            self.controller.management.set_limits({'client': 'qb', 'upload_kib': 10, 'download_kib': 0})
        with self.assertRaises(web.ApiError):
            self.controller.management.begin('transfer', {'hashes': [api.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.assertIsNone(self.controller.management.state['job'])

    @unittest.skipUnless(os.name == 'posix', 'Linux lock')
    def test_native_run_lock_blocks_web_setting_updates(self):
        import fcntl
        self.lock_patch.stop()
        with (self.directory / 'run.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(web.ApiError):
                self.controller.update_settings({'target': 701})

    def effective_settings(self, **values):
        self.controller.update_settings({'refill_count_basis': 'site_effective', 'target': 1200,
            'refill_trigger': 1100, 'refill_floor': 1000, 'refill_check_minutes': 5,
            'refill_max_inflight': 500, 'refill_retry_seconds': 60,
            'refill_site_max_age_minutes': 120, 'refill_reservation_hours': 72, **values})

    def site_receipt(self, current=742, **values):
        return {'available': True, 'stale': False, 'current': current, 'seeders_max': 10,
                'synced_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.now - 60)), **values}

    def test_long_term_policy_is_editable_and_invalid_relations_are_atomic(self):
        self.effective_settings(target=1500, refill_trigger=1300, refill_floor=1200,
            refill_check_minutes=3, refill_max_inflight=200, refill_retry_seconds=30,
            refill_site_max_age_minutes=90, refill_reservation_hours=48)
        values = self.controller.settings()
        self.assertEqual((values['target'], values['refill_trigger'], values['refill_floor']), (1500, 1300, 1200))
        self.assertEqual((values['refill_check_minutes'], values['refill_max_inflight'], values['refill_retry_seconds']), (3, 200, 30))
        before = self.config.read_bytes()
        for invalid in ({'refill_trigger': 1500}, {'refill_floor': 1301}, {'target': 1200},
                        {'refill_floor': True}, {'refill_count_basis': 'invalid'}, {'refill_check_minutes': 0}):
            with self.subTest(invalid=invalid), self.assertRaises(web.ApiError):
                self.controller.update_settings(invalid)
            self.assertEqual(self.config.read_bytes(), before)

    def test_old_configuration_remains_compatible_and_status_is_read_only(self):
        before = self.config.read_bytes()
        self.assertEqual(self.controller.settings()['refill_count_basis'], 'managed_tasks')
        with patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})):
            value = self.controller.status()
            self.controller.status()
        self.assertEqual(value['refill_strategy']['basis'], 'managed_tasks')
        self.assertTrue(all(set(row) == {'id', 'name', 'type', 'enabled', 'default'} for row in value['downloaders']))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.directory / 'refill_state.json').exists())

    def test_effective_scheduler_checks_minutes_and_passes_a_bounded_allowance(self):
        self.effective_settings()
        self.controller.automation(True)
        self.assertEqual(self.controller.runtime['next_run_at'], self.now)
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt()) as site, \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})):
            self.controller.tick()
            self.controller.tick()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][-2:], ['--refill-allowance', '50'])
        self.assertEqual(self.controller.runtime['next_run_at'], self.now + 300)
        site.assert_called_once_with(force=True, include_rss=False)
        self.assertEqual(self.controller.settings()['target'], 1200)

    def test_effective_manual_run_waits_for_existing_downloads_instead_of_spawning(self):
        self.effective_settings()
        hashes = ['a' * 40, 'b' * 40]
        pull.save_json(self.directory / 'batch.json', {'accepted': {str(i): {'hash': value} for i, value in enumerate(hashes)}})
        rows = [qb_task(value, progress=.5, amount_left=50, state='downloading') for value in hashes]
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt(1198)), \
             patch.object(self.controller, 'snapshot', return_value=(rows, {}, {'error': None})):
            result = self.controller.start_run()
        self.assertTrue(result['ok'])
        self.assertFalse(result['started'])
        self.assertEqual(result['reason'], 'waiting_downloads')
        self.assertEqual(self.spawned, [])

    def test_effective_unknown_or_expired_receipt_retries_without_starting(self):
        self.effective_settings()
        self.controller.automation(True)
        for receipt in (self.site_receipt(stale=True), self.site_receipt(current=None),
                        self.site_receipt(synced_at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.now - 7201)))):
            self.controller.runtime['next_run_at'] = self.now
            with patch.object(self.controller.pts, 'snapshot', return_value=receipt), \
                 patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})), patch.object(pull, 'event'):
                self.controller.tick()
            self.assertEqual(self.controller.runtime['next_run_at'], self.now + 60)
            self.assertEqual(self.spawned, [])

    def test_failed_inventory_never_creates_a_false_shortage(self):
        self.effective_settings()
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt()), \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': 'unavailable'})):
            with self.assertRaises(web.ApiError) as error:
                self.controller.start_run()
        self.assertEqual(error.exception.status, 502)
        self.assertFalse(self.spawned)

    def test_busy_refill_short_retry_does_not_skip_a_six_hour_slot(self):
        self.controller.automation(True)
        self.now = self.controller.runtime['next_run_at']
        with patch.object(self.controller.fleet, 'busy', return_value=True), patch.object(pull, 'event'):
            self.controller.tick()
        self.assertFalse(self.spawned)
        self.assertEqual(self.controller.runtime['next_run_at'], self.now + 60)
        self.now += 60
        self.controller.tick()
        self.assertEqual(len(self.spawned), 1)

    def test_effective_busy_skips_network_and_retries_after_job_is_idle(self):
        self.effective_settings()
        self.controller.automation(True)
        with patch.object(self.controller.fleet, 'busy', return_value=True), \
             patch.object(self.controller.pts, 'snapshot', side_effect=AssertionError('unexpected query')), patch.object(pull, 'event'):
            self.controller.tick()
        self.assertEqual(self.controller.runtime['next_run_at'], self.now + 60)
        self.assertEqual(self.spawned, [])
        self.now += 60
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt()), \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})):
            self.controller.tick()
        self.assertEqual(len(self.spawned), 1)

    def test_spawn_failure_keeps_effective_refill_active_and_retryable(self):
        self.effective_settings()
        self.controller.automation(True)
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt()), \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})), \
             patch.object(self.controller, 'spawn', side_effect=OSError('test failure')), patch.object(pull, 'event'):
            self.controller.tick()
        self.assertEqual(self.controller.runtime['next_run_at'], self.now + 60)
        summary = self.controller.refill_strategy.public(self.controller.settings(), True, self.now + 60)
        self.assertTrue(summary['active'])
        self.assertEqual(summary['status'], 'unknown')

    def test_disable_effective_strategy_preserves_records_and_stops_future_checks(self):
        self.effective_settings()
        records = {'accepted': {'1': {'hash': 'a' * 40}}, 'seen_hashes': ['b' * 40]}
        pull.save_json(self.directory / 'batch.json', records)
        self.controller.automation(True)
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt()), \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})):
            self.controller.check_effective_refill()
        self.controller.automation(False)
        self.controller.tick()
        self.assertIsNone(self.controller.runtime['next_run_at'])
        self.assertEqual(web.load_json(self.directory / 'batch.json'), records)
        self.assertEqual(self.controller.refill_strategy.public(self.controller.settings(), False, None)['status'], 'disabled')

    def test_legacy_tr_rpc_projection_keeps_unmanaged_pts_inflight(self):
        import io
        self.effective_settings(refill_max_inflight=1)
        source = self.controller.source()
        source['api_base'] = 'https://www.ptskit.org'
        pull.save_json(pull.Path(self.controller.settings()['source_config']), source)
        raw = dict(hashString='d' * 40, name='fixture', totalSize=100, status=4, percentDone=.5,
                   trackerStats=[{'announce': 'https://tracker.ptskit.org/announce'}],
                   error=0, doneDate=0, addedDate=self.now - 10)
        requested = []
        def response(request, **kwargs):
            fields = json.loads(request.data)['arguments']['fields']
            requested.append(fields)
            return io.BytesIO(json.dumps({'result': 'success', 'arguments': {'torrents':
                [{key: value for key, value in raw.items() if key in fields}]}}).encode())
        client = pull.Clients.__new__(pull.Clients)
        client.settings = {'tr_url': 'http://fixture.invalid/rpc'}
        client.tr_headers = {}
        client.tr = type('Opener', (), {'open': staticmethod(response)})()
        client.qb_get = lambda _: []
        with patch.object(pull, 'Clients', return_value=client), \
             patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt(1199)):
            result = self.controller.check_effective_refill(force=True)
        self.assertEqual((result['inflight'], result['reserved'], result['allowance']), (1, 1, 0))
        self.assertTrue(all({'trackerStats', 'error', 'doneDate', 'addedDate'} <= set(fields) for fields in requested))


    def test_timing_edits_preserve_refill_latch_until_target(self):
        self.effective_settings()
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt(1099)), \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})):
            self.assertTrue(self.controller.check_effective_refill()['active'])
        self.controller.update_settings({'refill_check_minutes': 3, 'refill_retry_seconds': 30})
        with patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt(1150)), \
             patch.object(self.controller, 'snapshot', return_value=([], {}, {'error': None})):
            result = self.controller.check_effective_refill()
        self.assertTrue(result['active'])
        self.assertEqual(result['allowance'], 50)

    @unittest.skipUnless(os.name == 'posix', 'Linux lock')
    def test_external_run_lock_skips_effective_network_and_short_retries(self):
        import fcntl
        self.effective_settings()
        self.controller.automation(True)
        self.lock_patch.stop()
        with (self.directory / 'run.lock').open('a') as stream, \
             patch.object(self.controller.pts, 'snapshot') as site, \
             patch.object(self.controller, 'refill_inventory') as inventory:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.controller.tick()
        site.assert_not_called()
        inventory.assert_not_called()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.controller.runtime['next_run_at'], self.now + 60)


    def test_effective_inventory_preserves_duplicate_instance_states(self):
        self.effective_settings()
        settings = self.controller.settings()
        settings['downloaders'] = []
        pull.save_json(self.config, settings)
        value = 'c' * 40
        source = self.controller.source()
        source['api_base'] = 'https://www.ptskit.org'
        pull.save_json(pull.Path(settings['source_config']), source)
        active = qb_task(value, progress=.5, amount_left=50, state='downloading')
        paused = qb_task(value, progress=.5, amount_left=50, state='pausedDL')
        APIs = {'q1': type('Client', (), {'inventory_one': lambda _: [active]})(),
                'q2': type('Client', (), {'inventory_one': lambda _: [paused]})()}
        instances = [{'id': key, 'type': 'qb', 'enabled': True} for key in APIs]
        with patch.object(self.controller.registry, 'items', return_value=instances), \
             patch.object(self.controller.registry, 'api', side_effect=lambda key: APIs[key]), \
             patch.object(self.controller.pts, 'snapshot', return_value=self.site_receipt(1199)):
            rows, _, connection = self.controller.refill_inventory()
            self.assertEqual(len(rows), 2)
            self.assertIsNone(connection['error'])
            decision = self.controller.check_effective_refill(force=True)
        self.assertEqual((decision['inflight'], decision['reserved'], decision['allowance']), (1, 1, 0))


    def test_single_seed_target_allows_zero_trigger_and_floor(self):
        compatible = web.configuration.validate_runtime({'target': 1, 'refill_count_basis': 'site_effective'})
        self.assertEqual((compatible['refill_trigger'], compatible['refill_floor']), (0, 0))
        self.controller.update_settings({'target': 1, 'refill_count_basis': 'site_effective',
                                         'refill_trigger': 0, 'refill_floor': 0})
        self.assertEqual(self.controller.settings()['target'], 1)



if __name__ == '__main__':
    unittest.main()
