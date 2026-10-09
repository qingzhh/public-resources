"""Offline acceptance for editable runtime and private connection configuration."""
import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import seedkeep_configuration as config
import seedkeep_downloaders as downloads
import seedkeep_management as management
import seedkeep_pts as pts
import seedkeep_pull as pull
import seedkeep_web as web
from test_seedkeep_management import FakeAPI, NOW, qb_task, tr_task
from test_seedkeep_pts import sample_payload
from test_seedkeep_pull import torrent


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = {'api_base': 'https://www.ptskit.org', 'token': 'private-site-token',
                       'host': 'qb.invalid', 'port': 8080, 'username': 'qb-user', 'password': 'private-qb-pass',
                       'download_path': '/downloads/PTS', 'category': 'keep', 'tag': 'keep', 'keep_torrent': True}
        pull.save_json(self.directory / 'source.json', self.source)
        pull.save_json(self.directory / 'tr.json', {'username': 'tr-user', 'password': 'private-tr-pass'})
        self.path = self.directory / 'docker_settings.json'
        settings = {'source_config': str(self.directory / 'source.json'), 'tr_credentials_file': str(self.directory / 'tr.json'),
                    'tr_url': 'http://tr.invalid:9091/transmission/rpc', 'mode': 'maintain', 'target': 700,
                    'max_per_run': 50, 'max_bytes': 524288000, 'min_seeders': 2, 'max_seeders': 6,
                    'interval_hours': 2, 'cron_minute': 17}
        pull.save_json(self.path, settings)
        self.now = NOW
        self.controller = web.Controller(self.path, clock=lambda: self.now)
        self.controller.file_lock = contextlib.nullcontext
        self.api = FakeAPI(torrent(100))
        self.controller.management.client_factory = lambda *_: self.api
        self.controller.management.pts = lambda: {'available': True, 'stale': False, 'seeders_max': 10}

    def save(self, values, revision=None):
        return self.controller.update_configuration({'values': values,
            'revision': self.controller.configuration()['revision'] if revision is None else revision})

    def test_runtime_defaults_and_all_numeric_boundaries(self):
        defaults = config.validate_runtime({})
        self.assertEqual(defaults['transfer_path_mappings'], config.DEFAULT_MAPPINGS)
        defaults['transfer_path_mappings'].clear()
        self.assertEqual(len(config.DEFAULT_MAPPINGS), 3)
        for key, (_, lower, upper, integer) in config.NUMBERS.items():
            for bad in (True, None, lower - 1, upper + 1, float('nan'), float('inf'), 10 ** 400):
                with self.subTest(key=key, bad_type=type(bad).__name__):
                    with self.assertRaises(config.ConfigurationError):
                        config.validate_runtime({key: bad})
            for good in (lower, upper):
                self.assertEqual(config.validate_runtime({key: good})[key], good)
            if integer:
                with self.assertRaises(config.ConfigurationError):
                    config.validate_runtime({key: float(lower)})

    def test_runtime_save_is_partial_persistent_and_keeps_source_records(self):
        batch = {'accepted': {'1': {'hash': 'a' * 40}}, 'baseline': {'tr_hashes': []}, 'seen_hashes': ['b' * 40]}
        pull.save_json(self.directory / 'batch.json', batch)
        original = (self.directory / 'source.json').read_bytes()
        values = {key: rule[1] for key, rule in config.NUMBERS.items()}
        values.update(auto_start=False, seeders_limit_mode='manual', manual_seeders_max=12,
                      transfer_path_mappings=[{'qb': '/downloads', 'tr': '/data'}])
        self.controller.update_settings(values)
        saved = web.Controller(self.path).settings()
        self.assertTrue(all(saved[key] == value for key, value in values.items()))
        self.assertEqual((self.directory / 'source.json').read_bytes(), original)
        self.assertEqual(web.load_json(self.directory / 'batch.json'), batch)
        before = self.path.read_bytes()
        with self.assertRaises(web.ApiError):
            self.controller.update_settings({'qb_url': 'http://elsewhere.invalid'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_connections_only_return_secret_presence(self):
        value = self.controller.configuration()
        self.assertEqual(set(value['values']), set(config.CONNECTION_FIELDS))
        self.assertTrue(all(value['secrets'].values()))
        encoded = json.dumps(value)
        for secret in ('private-site-token', 'private-qb-pass', 'private-tr-pass'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(len(value['revision']), 64)
        self.assertEqual(value['values']['qb_url'], 'http://qb.invalid:8080')
        with patch.dict(os.environ, WEB_PORT='8877', TZ='UTC'):
            self.assertEqual(self.controller.configuration()['deployment'], {'web_port': 8877, 'timezone': 'UTC'})
        with patch.object(self.controller, 'snapshot', return_value=([], {}, {})):
            snapshot = json.dumps(self.controller.status())
        for private in ('qb.invalid', 'tr.invalid', 'qb-user', 'tr-user', '/downloads/PTS', 'private-site-token'):
            self.assertNotIn(private, snapshot)

    def test_empty_secrets_preserve_and_destination_changes_do_not_pause(self):
        self.controller.automation(True)
        self.controller.management.update_policy({**management.DEFAULT_POLICY, 'cleanup_enabled': True})
        token = self.controller.login({'username': 'tr-user', 'password': 'private-tr-pass'}, 'test')
        result = self.save({'token': '', 'qb_password': '', 'tr_password': '', 'download_path': '',
                            'category': 'next', 'tag': 'one,two', 'keep_torrent': False})
        self.assertFalse(result['reauth_required'])
        self.assertFalse(result['automation_paused'])
        self.assertTrue(self.controller.runtime['automatic_enabled'])
        self.assertTrue(self.controller.management.config['cleanup_enabled'])
        self.assertTrue(self.controller.authenticated(token))
        source = self.controller.source()
        self.assertEqual((source['token'], source['password']), ('private-site-token', 'private-qb-pass'))
        self.assertEqual(config.tr_credentials(self.controller.settings())['password'], 'private-tr-pass')
        self.assertFalse(source['keep_torrent'])
        self.assertEqual(source['download_path'], '')

    def test_qb_clear_is_explicit_and_conflicting_input_is_atomic(self):
        before = self.path.read_bytes()
        for values in ({'clear_qb_password': True, 'qb_password': 'new'}, {'clear_qb_password': 1},
                       {'tr_username': ''}, {'token': 'bad\nsecret'}, {'keep_torrent': 0}):
            with self.assertRaises(web.ApiError):
                self.save(values)
            self.assertEqual(self.path.read_bytes(), before)
        result = self.save({'clear_qb_password': True})
        self.assertFalse(result['configuration']['secrets']['qb_password'])
        self.assertTrue(result['automation_paused'])
        self.assertEqual(self.controller.source()['password'], '')

    def test_revision_conflict_rejects_without_changing_automation_or_files(self):
        revision = self.controller.configuration()['revision']
        self.save({'category': 'first'})
        self.controller.automation(True)
        before = self.path.read_bytes()
        for stale in (revision, None, 'x', '中' * 64):
            with self.assertRaises(web.ApiError) as error:
                self.controller.update_configuration({'values': {'qb_url': 'http://other.invalid'}, 'revision': stale})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertTrue(self.controller.runtime['automatic_enabled'])

    def test_connection_change_pauses_persistently_and_resets_old_observations(self):
        self.controller.automation(True)
        self.controller.management.update_policy({**management.DEFAULT_POLICY, 'cleanup_enabled': True})
        self.controller.management.state['observations'] = {'a' * 40: {'since': self.now - 90000}}
        self.controller.management.raw = ([{'private': 'old-account'}], [])
        result = self.save({'qb_url': 'https://new.invalid/qb/'})
        self.assertTrue(result['automation_paused'])
        self.assertFalse(result['reauth_required'])
        self.assertFalse(self.controller.runtime['automatic_enabled'])
        self.assertFalse(self.controller.management.config['cleanup_enabled'])
        self.assertFalse(self.controller.management.state['observations'])
        self.assertEqual(self.controller.management.raw, ([], []))
        restarted = web.Controller(self.path)
        self.assertFalse(restarted.runtime['automatic_enabled'])
        self.assertFalse(restarted.management.config['cleanup_enabled'])
        self.assertEqual(restarted.configuration()['values']['qb_url'], 'https://new.invalid/qb')
        self.assertEqual((self.path.stat().st_mode & 0o777) if os.name != 'nt' else 0o600, 0o600)

    def test_tr_credentials_revoke_previous_sessions_and_survive_restart(self):
        token = self.controller.login({'username': 'tr-user', 'password': 'private-tr-pass'}, 'test')
        result = self.save({'tr_username': 'new-tr-user', 'tr_password': 'new-private-pass'})
        self.assertTrue(result['reauth_required'])
        self.assertFalse(self.controller.authenticated(token))
        with self.assertRaises(web.ApiError):
            self.controller.login({'username': 'tr-user', 'password': 'private-tr-pass'}, 'test')
        token = self.controller.login({'username': 'new-tr-user', 'password': 'new-private-pass'}, 'test')
        restarted = web.Controller(self.path, clock=lambda: self.now)
        self.assertTrue(restarted.authenticated(token))
        result = self.save({'tr_password': ''})
        self.assertFalse(result['reauth_required'])
        self.assertTrue(self.controller.authenticated(token))

    def test_draft_check_uses_inputs_but_cannot_save_or_pause(self):
        self.controller.automation(True)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with patch.object(web.pts, 'fetch_payload', return_value=sample_payload()) as site, \
             patch.object(web, 'API', return_value=self.api) as factory:
            result = self.controller.check_configuration({'values': {'api_base': 'https://draft.invalid',
                'token': 'new-draft-secret', 'qb_url': 'http://draft-qb.invalid', 'tr_password': 'draft-tr-pass'}})
        self.assertTrue(all(result[name]['connected'] for name in ('site', 'qb', 'tr')))
        self.assertEqual(site.call_args.args[0]['token'], 'new-draft-secret')
        source, settings = factory.call_args.args
        self.assertEqual(source['qb_url'], 'http://draft-qb.invalid')
        self.assertEqual(config.tr_credentials(settings)['password'], 'draft-tr-pass')
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})
        self.assertTrue(self.controller.runtime['automatic_enabled'])
        with patch.object(web.pts, 'fetch_payload', side_effect=ValueError('new-draft-secret')), \
             patch.object(web, 'API', side_effect=ValueError('new-draft-secret')):
            result = self.controller.check_configuration({'values': {}})
        self.assertFalse(any(value['connected'] for value in result.values()))
        self.assertNotIn('new-draft-secret', json.dumps(result))

    def test_busy_settings_and_connection_saves_preserve_data(self):
        self.controller.management.state['job'] = {'status': 'waiting'}
        before = self.path.read_bytes()
        for operation in (lambda: self.save({'category': 'new'}), lambda: self.controller.update_settings({'target': 800})):
            with self.assertRaises(web.ApiError) as error:
                operation()
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.controller.management.state['job'] = None
        self.controller.process = type('Busy', (), {'poll': lambda _: None})()
        with self.assertRaises(web.ApiError):
            self.save({'category': 'new'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_logs_honor_large_limit_and_status_cache_honors_interval(self):
        self.controller.update_settings({'log_limit': 170, 'page_refresh_seconds': 5})
        (self.directory / 'run.log').write_text(''.join(json.dumps({'time': str(i).zfill(4), 'event': 'progress'}) + '\n' for i in range(220)))
        self.assertEqual(len(self.controller.logs()['items']), 170)
        with patch.object(web.pull, 'Clients') as clients:
            clients.return_value.snapshot.return_value = ([], {})
            self.controller.snapshot()
            self.now += 4
            self.controller.snapshot()
            self.assertEqual(clients.call_count, 1)
            self.now += 1
            self.controller.snapshot()
            self.assertEqual(clients.call_count, 2)

    def test_urls_and_mappings_are_normalized_and_reject_invalid_inputs(self):
        for value in ('file:///a', 'http://user:pass@example.invalid', 'http://example.invalid?q=x',
                      'http://example.invalid/#x', 'http://example.invalid:99999', 'http://bad\n.invalid'):
            with self.assertRaises(config.ConfigurationError):
                config.url(value)
        for values in ([{'qb': '/', 'tr': '/data'}], [{'qb': '/a/../b', 'tr': '/b'}],
                       [{'qb': 'C:\\a', 'tr': '/b'}], [{'qb': '/a', 'tr': '/b'}, {'qb': '/a/', 'tr': '/c'}],
                       [{'qb': '/a', 'tr': '/b', 'extra': True}], [{'qb': '/a', 'tr': '/b'}] * 21):
            with self.assertRaises(config.ConfigurationError):
                config.mappings(values)
        settings = {'transfer_path_mappings': config.mappings([{'qb': '/downloads', 'tr': '/data'},
                                                              {'qb': '/downloads/PTS/', 'tr': '/special/.'}])}
        self.assertEqual(config.mapped_path('/downloads/PTS/movie', settings), ('/downloads/PTS/movie', '/special/movie'))
        self.assertEqual(config.mapped_path('/downloads/other', settings), ('/downloads/other', '/data/other'))
        self.assertIsNone(config.mapped_path('/downloads-other', settings))
        self.assertIsNone(config.mapped_path('/downloads/x', {'transfer_path_mappings': []}))

    def test_proxy_is_explicit_or_uses_environment_only_when_enabled(self):
        with patch.dict(os.environ, {'http_proxy': 'http://env.invalid:8080', 'https_proxy': 'http://env.invalid:8080'}):
            with patch.object(config.urllib.request, 'ProxyHandler', wraps=config.urllib.request.ProxyHandler) as proxy, \
                 patch.object(config.urllib.request, 'build_opener'):
                config.opener({'site_use_proxy': False})
                self.assertEqual(proxy.call_args.args, ({},))
                config.opener({'site_use_proxy': True, 'site_proxy_url': ''})
                self.assertEqual(proxy.call_args.args, ())
                config.opener({'bt_use_proxy': True, 'bt_proxy_url': 'http://explicit.invalid:8080'}, 'bt')
                self.assertEqual(proxy.call_args.args[0]['https'], 'http://explicit.invalid:8080')

    def test_downloader_uses_saved_endpoint_timeouts_and_auto_start(self):
        settings = self.controller.settings()
        candidate = config.prepare_connections({'qb_url': 'http://new-qb.invalid/ui'}, self.source, settings, self.controller.credentials())
        settings.update(connections=candidate, auto_start=False, qb_timeout_seconds=7, tr_timeout_seconds=9)
        with patch.object(pull, 'request', return_value=b'Ok.') as request:
            clients = pull.Clients(self.source, settings)
            clients.add(torrent(100))
        self.assertEqual(request.call_args.args[1], 'http://new-qb.invalid/ui/api/v2/torrents/add')
        self.assertIn(b'name="stopped"\r\n\r\ntrue', request.call_args.args[2])
        self.assertEqual(request.call_args.kwargs['timeout'], 7)
        api = downloads.API(self.source, settings)
        api.logged_in = True
        with patch.object(pull, 'request', return_value=[]):
            self.assertEqual(api.qget('torrents/info'), [])
        response = io.BytesIO(json.dumps({'result': 'success', 'arguments': {}}).encode())
        with patch.object(api.tr, 'open', return_value=response) as opened:
            api.rpc('session-get')
        self.assertEqual(opened.call_args.kwargs['timeout'], 9)
        self.assertEqual(opened.call_args.args[0].full_url, settings['tr_url'])

    def test_download_retry_and_site_cache_settings_reach_requests(self):
        settings = {**self.controller.settings(), 'download_retries': 4, 'download_retry_seconds': 3,
                    'request_interval_seconds': .2, 'download_timeout_seconds': 7}
        with patch.object(pull, 'request', side_effect=[pull.PullError('failed')] * 3 + [torrent(100)]) as request, \
             patch.object(pull.time, 'sleep') as sleep:
            data, meta, error = pull.Downloader(self.source, settings).get({'url': 'https://test.invalid/file'})
        self.assertIsNone(error)
        self.assertEqual(meta['size'], 100)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args.kwargs['timeout'], 7)
        self.assertEqual(sum(call.args == (3,) for call in sleep.call_args_list), 3)
        self.controller.update_settings({'pts_cache_seconds': 10})
        with patch.object(self.controller.pts, 'fetcher', return_value=sample_payload()) as fetcher:
            self.controller.pts.snapshot()
            self.now += 9
            self.controller.pts.snapshot()
            self.assertEqual(fetcher.call_count, 1)
            self.now += 1
            self.controller.pts.snapshot()
            self.assertEqual(fetcher.call_count, 2)

    def test_http_configuration_auth_csrf_conflict_and_secret_redaction(self):
        server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        server.controller = self.controller
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(client.close)
        cookie = 'seedkeep_session=' + self.controller.login({'username': 'tr-user', 'password': 'private-tr-pass'}, 'test')
        def request(method, path, body=None, auth=True, csrf=True, origin=None):
            headers = {'Content-Type': 'application/json'}
            if auth:
                headers['Cookie'] = cookie
            if csrf:
                headers['X-Seedkeep-Request'] = '1'
            if origin:
                headers['Origin'] = origin
            client.request(method, path, None if body is None else json.dumps(body), headers)
            response = client.getresponse()
            return response.status, response.read()
        for path in ('/api/configuration', '/api/configuration/check'):
            self.assertEqual(request('POST', path, {'values': {}}, auth=False)[0], 401)
            self.assertEqual(request('POST', path, {'values': {}}, csrf=False)[0], 403)
            self.assertEqual(request('POST', path, {'values': {}}, origin='http://other.invalid')[0], 403)
        self.assertEqual(request('GET', '/api/configuration', auth=False)[0], 401)
        code, data = request('GET', '/api/configuration')
        self.assertEqual(code, 200)
        self.assertNotIn(b'private-tr-pass', data)
        revision = json.loads(data)['revision']
        self.assertEqual(request('POST', '/api/configuration', {'values': {'category': 'next'}, 'revision': revision})[0], 200)
        self.assertEqual(request('POST', '/api/configuration', {'values': {'category': 'stale'}, 'revision': revision})[0], 409)
        with patch.object(web.pts, 'fetch_payload', return_value=sample_payload()), patch.object(web, 'API', return_value=self.api):
            self.assertEqual(request('POST', '/api/configuration/check', {'values': {}})[0], 200)


class RuntimeBudgetTests(unittest.TestCase):
    def test_queued_rate_limited_downloads_end_at_shared_deadline(self):
        import time
        started = time.monotonic()
        downloader = pull.Downloader({'api_base': 'https://site.invalid'},
            {'request_interval_seconds': 60, 'download_timeout_seconds': 300}, deadline=started + .2)
        with patch.object(pull, 'request', return_value=torrent(100)) as request:
            with pull.ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(downloader.get, [{'url': 'https://site.invalid/torrent'}] * 12))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(request.call_count, 1)
        self.assertLessEqual(request.call_args.kwargs['timeout'], .2)
        self.assertEqual(sum(result[2] == 'run_time_limit' for result in results), 11)

    def test_retry_wait_cannot_outlive_run_deadline(self):
        import time
        started = time.monotonic()
        downloader = pull.Downloader({'api_base': 'https://site.invalid'},
            {'download_retries': 10, 'download_retry_seconds': 60}, deadline=started + .1)
        with patch.object(pull, 'request', side_effect=pull.PullError('temporary_failure')) as request:
            result = downloader.get({'url': 'https://site.invalid/torrent'})
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result[2], 'run_time_limit')


class RuntimeManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR'))
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = NOW
        self.runtime = config.validate_runtime({})
        self.site = {'available': True, 'stale': False, 'seeders_max': 10}
        self.api = FakeAPI(torrent(100))
        self.hash = self.api.hash
        self.manager = self.restart()

    def restart(self):
        return management.Manager(self.directory, lambda: {'api_base': 'https://www.ptskit.org'},
            lambda: self.runtime, lambda: self.site, contextlib.nullcontext,
            client_factory=lambda *_: self.api, clock=lambda: self.now, transfer_mode='full')

    def begin(self):
        self.manager.begin('transfer', {'hashes': [self.hash], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.manager.tick()

    def test_different_mapped_directories_complete_after_restart_and_verification(self):
        self.runtime['transfer_path_mappings'] = [{'qb': '/downloads', 'tr': '/data'}]
        self.begin()
        self.assertEqual(self.api.tr[self.hash]['downloadDir'], '/data/PTS')
        source = self.manager.state['job']['items'][0]['source']
        self.assertEqual((source['download_dir'], source['tr_download_dir']), ('/downloads/PTS', '/data/PTS'))
        self.manager = self.restart()
        self.api.tr[self.hash].update(status=0, percentDone=1, leftUntilDone=0, haveValid=100, haveUnchecked=0)
        self.now += 20
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'completed')
        self.assertNotIn(self.hash, self.api.qb)
        self.assertEqual(self.api.tr[self.hash]['status'], 6)

    def test_mapped_cancel_restores_source_and_existing_target_state(self):
        self.runtime['transfer_path_mappings'] = [{'qb': '/downloads', 'tr': '/data'}]
        self.api.tr[self.hash] = tr_task(self.hash, downloadDir='/data/PTS')
        self.begin()
        self.manager = self.restart()
        self.manager.cancel({'job_id': self.manager.state['job']['id']})
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'cancelled')
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.api.tr[self.hash]['status'], 6)
        self.assertEqual(self.api.tr[self.hash]['downloadDir'], '/data/PTS')
        self.assertFalse(any(name == 'qremove' for name, _ in self.api.calls))

    def test_empty_mapping_rejects_transfer_but_legacy_cap_does_not_limit_queue(self):
        self.runtime['transfer_path_mappings'] = []
        self.assertFalse(self.manager.snapshot(force=True)['items'][0]['can_transfer'])
        with self.assertRaises(downloads.ManagementError):
            self.begin()
        self.runtime['transfer_path_mappings'] = copy.deepcopy(config.DEFAULT_MAPPINGS)
        self.runtime['transfer_max_per_job'] = 1
        other = 'a' * 40
        self.api.qb[other] = qb_task(other)
        result = self.manager.begin('transfer', {'hashes': [self.hash, other], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.assertEqual([item['hash'] for item in result['job']['items']], [self.hash, other])

    def test_manual_threshold_still_requires_live_site_for_deletion_wait(self):
        self.runtime.update(seeders_limit_mode='manual', manual_seeders_max=4)
        value = self.manager.snapshot(force=True)
        self.assertEqual((value['seeders_max'], value['site_seeders_max'], value['seeders_limit_mode']), (4, 10, 'manual'))
        self.assertEqual(value['items'][0]['validity'], 'invalid')
        self.assertTrue(self.manager.state['observations'])
        self.site['stale'] = True
        value = self.manager.snapshot(force=True)
        self.assertFalse(self.manager.state['observations'])
        self.assertFalse(value['items'][0]['delete_ready'])
        self.assertIsNone(value['items'][0]['delete_after'])
        self.assertEqual(value['seeders_max'], 4)

    def test_monitor_cache_and_tracker_freshness_are_effective(self):
        self.runtime.update(management_cache_seconds=5, monitor_interval_seconds=15, tracker_fresh_seconds=60)
        self.manager.snapshot()
        calls = len(self.api.calls)
        self.now += 4
        self.manager.snapshot()
        self.assertEqual(len(self.api.calls), calls)
        self.now += 1
        self.manager.snapshot()
        self.assertGreater(len(self.api.calls), calls)
        self.manager.tick()
        calls = len(self.api.calls)
        self.now += 14
        self.manager.tick()
        self.assertEqual(len(self.api.calls), calls)
        self.now += 1
        self.manager.tick()
        self.assertGreater(len(self.api.calls), calls)
        self.api.qb.clear()
        self.api.tr[self.hash] = tr_task(self.hash)
        self.now = NOW + 61
        self.assertEqual(self.manager.snapshot(force=True)['items'][0]['validity'], 'unknown')

    def test_pause_and_verification_timeouts_keep_qb_source(self):
        self.runtime['pause_timeout_seconds'] = 10
        self.api.stop_blocked = True
        self.begin()
        self.now += 11
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'failed')
        self.assertFalse(self.api.tr)
        self.api.stop_blocked = False
        self.runtime['verify_timeout_seconds'] = 60
        self.begin()
        self.now += 61
        self.manager.tick()
        self.assertEqual(self.manager.state['job']['status'], 'failed')
        self.assertEqual(self.api.qb[self.hash]['state'], 'uploading')
        self.assertFalse(any(name == 'qremove' for name, _ in self.api.calls))


if __name__ == '__main__':
    unittest.main()
