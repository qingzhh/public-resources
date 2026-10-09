"""HTTP and controller integration for the multi-downloader iteration."""
import contextlib
import copy
import os
import http.client
import json
import threading
import unittest
from unittest.mock import patch
import seedkeep_web as web
import seedkeep_transfer as transfer
import seedkeep_pull as pull
import test_seedkeep_web as baseline
from test_seedkeep_management import FakeAPI, torrent, tr_task, qb_task


class MultiWebTests(unittest.TestCase):
    def setUp(self):
        baseline.WebTests.setUp(self)
        settings = self.controller.settings()
        settings['tr_url'] = 'http://tr.invalid:9091/transmission/rpc'
        pull.save_json(self.config, settings)
        source = pull.Path(settings['source_config'])
        values = web.load_json(source)
        values.update(api_base='https://www.ptskit.org', qb_url='http://qb.invalid:8080')
        pull.save_json(source, values)
        self.api = FakeAPI(torrent(100))
        self.api.tr[self.api.hash] = tr_task(self.api.hash)
        self.controller.fleet.registry.api_factory = lambda *args: self.api
        self.controller.fleet.api_factory = lambda *args: self.api
        self.controller.fleet.transfer_rules = transfer.Rules(self.controller.fleet)
        self.controller.fleet.pts = lambda: {'available': True, 'stale': False, 'seeders_max': 10}
        self.server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        self.server.controller = self.controller
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        self.addCleanup(self.connection.close)
        self.cookie = None
        code, _, self.cookie = self.request('POST', 'login', {'username': 'test-user', 'password': 'test-password'})
        self.assertEqual(code, 200)

    def request(self, method, path, body=None, auth=True, csrf=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if auth and self.cookie:
            headers['Cookie'] = self.cookie
        if csrf:
            headers['X-Seedkeep-Request'] = '1'
        if origin:
            headers['Origin'] = origin
        self.connection.request(method, '/api/' + path, json.dumps(body) if body is not None else None, headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read()), response.getheader('Set-Cookie')

    def test_all_new_routes_require_auth_origin_and_csrf(self):
        gets = ('instances', 'fleet/tasks', 'fleet/policy', 'fleet/limits?instance_id=qb', 'logs/settings',
                'logs?kind=error', 'fleet/transfer/settings', 'fleet/cleanup/settings', 'fleet/cleanup/storage', 'settings/pending')
        posts = ('configuration/token', 'instances/save', 'instances/delete', 'instances/check',
                 'fleet/tasks/refresh', 'fleet/actions', 'fleet/transfer', 'fleet/cancel',
                 'fleet/limits', 'fleet/policy', 'logs/settings', 'logs/cleanup',
                 'fleet/transfer/settings', 'fleet/transfer/preview', 'fleet/transfer/run',
                 'fleet/cleanup/settings', 'fleet/cleanup/storage', 'fleet/cleanup/preview',
                 'fleet/cleanup/mode', 'fleet/cleanup/run', 'fleet/cleanup/cancel', 'fleet/cleanup/recheck',
                 'status/refresh', 'pts/statistics/refresh', 'settings/defer')
        for path in gets:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path, auth=False)[0], 401)
        for path in posts:
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path, {}, auth=False)[0], 401)
                self.assertEqual(self.request('POST', path, {}, csrf=False)[0], 403)
                self.assertEqual(self.request('POST', path, {}, origin='http://other.invalid')[0], 403)

    def test_local_statistics_refresh_bypasses_both_caches_without_rescheduling_or_site_queries(self):
        from test_seedkeep_pts import sample_payload
        settings = self.controller.settings()
        settings['downloaders'] = self.controller.registry.items()
        pull.save_json(self.config, settings)
        self.controller.pts.fetcher = lambda source: sample_payload()
        self.controller.pts.snapshot(include_rss=False)
        self.controller.automation(True)
        before = {p: p.read_bytes() for p in (self.config, self.directory / 'web_runtime.json')}
        def factory(source, values):
            self.api.source, self.api.settings = source, values
            return self.api
        self.controller.fleet.registry.api_factory = factory
        self.controller.fleet.pts = lambda: self.controller.pts.snapshot(include_rss=False)
        for tracker in self.api.tr[self.api.hash]['trackerStats']:
            tracker['lastAnnounceTime'] = self.now
        with patch('seedkeep_downloaders.API', side_effect=factory):
            initial = self.request('GET', 'status')[1]
            self.assertEqual(initial['qb_total'], 1)
            self.api.qb['d' * 40] = qb_task('d' * 40)
            cached = self.request('GET', 'status')[1]
            self.assertEqual((cached['qb_total'], cached['seedkeep']['total']),
                             (initial['qb_total'], initial['seedkeep']['total']))
            self.now += 1
            self.controller.pts.attempted_at = self.now - 3601
            with patch.object(self.controller.pts, 'fetcher', side_effect=AssertionError('local refresh must not query PTS')) as fetcher:
                code, value, _ = self.request('POST', 'status/refresh', {})
                fetcher.assert_not_called()
            self.assertEqual(code, 200, value)
            self.assertEqual((value['qb_total'], value['seedkeep']['total']), (2, 2))
            self.assertEqual(value['seedkeep']['checked_at'], self.now)
            self.assertEqual(value['seedkeep']['valid'], 2)
            self.assertEqual(self.controller.cache_at, self.now)
            self.api.qb.clear()
            self.api.tr.clear()
            code, empty, _ = self.request('POST', 'status/refresh', {})
            self.assertEqual(code, 200, empty)
            self.assertEqual((empty['qb_total'], empty['tr_total'], empty['seedkeep']['total']), (0, 0, 0))
            self.api.qb['d' * 40] = qb_task('d' * 40)
            with patch.object(self.api, 'inventory_one', side_effect=RuntimeError('private network failure')):
                code, failed, _ = self.request('POST', 'status/refresh', {})
            self.assertEqual(code, 200, failed)
            self.assertFalse(failed['seedkeep']['connected'])
            self.assertIsNone(failed['qb_total'])
            self.assertIsNone(failed['seedkeep']['total'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse(self.spawned)
        self.assertEqual(self.downloader_writes(), [])
        for name in ('cleanup_rules.json', 'cleanup_rules_state.json', 'storage_mappings.json'):
            self.assertFalse((self.directory / name).exists())

    def test_site_statistics_refresh_forces_api_only_and_keeps_local_caches_and_schedule(self):
        from test_seedkeep_pts import sample_payload
        payload = sample_payload()
        calls = []
        self.controller.pts.fetcher = lambda source: calls.append('api') or payload
        self.controller.pts.rss_fetcher = lambda *args: calls.append('rss') or {'items': []}
        first = self.controller.pts.snapshot(include_rss=False)
        self.assertEqual(calls, ['api'])
        self.controller.automation(True)
        before = {p: p.read_bytes() for p in (self.config, self.directory / 'web_runtime.json')}
        self.controller.cache = ([], {}, {'qb': True, 'tr': True, 'error': None})
        self.controller.cache_at = self.now
        payload['data']['current'] = first['current'] + 7
        payload['data']['missing'] = max(0, first['target'] - payload['data']['current'])
        with patch.object(self.controller, 'snapshot', side_effect=AssertionError('site refresh must not query local inventory')):
            code, value, _ = self.request('POST', 'pts/statistics/refresh', {})
        self.assertEqual(code, 200, value)
        self.assertEqual(value['current'], first['current'] + 7)
        self.assertEqual(value['synced_at'], first['synced_at'])
        self.assertEqual(calls, ['api', 'api'])
        self.assertEqual(self.controller.cache_at, self.now)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse(self.spawned)
        self.assertEqual(self.downloader_writes(), [])

    def test_token_is_revealed_only_by_explicit_authenticated_post(self):
        code, values, _ = self.request('GET', 'configuration')
        self.assertEqual(code, 200)
        self.assertNotIn('test-secret', json.dumps(values))
        self.assertTrue(values['secrets']['token'])
        self.assertEqual(self.request('GET', 'configuration/token')[0], 404)
        self.assertEqual(self.request('POST', 'configuration/token', {'extra': True})[0], 400)
        code, result, _ = self.request('POST', 'configuration/token', {})
        self.assertEqual((code, result['token']), (200, 'test-secret'))
        self.assertNotIn('test-secret', json.dumps(self.controller.logs()))

    def test_crud_preserves_login_batch_and_password_but_pauses_identity_change(self):
        before_auth = self.controller.session_key()
        records = {'accepted': {'1': {'hash': self.api.hash}}, 'seen_hashes': ['b' * 40], 'baseline': {}}
        pull.save_json(self.directory / 'batch.json', records)
        batch = (self.directory / 'batch.json').read_bytes()
        self.controller.automation(True)
        revision = self.controller.registry.public()['revision']
        code, result, _ = self.request('POST', 'instances/save', {'revision': revision,
            'instance': {'id': 'qb', 'name': '改名', 'category': '新归类', 'password': ''}})
        self.assertEqual(code, 200)
        self.assertFalse(result['automation_paused'])
        self.assertTrue(self.controller.runtime['automatic_enabled'])
        self.assertTrue(next(row for row in result['items'] if row['id'] == 'qb')['password_configured'])
        code, _, _ = self.request('POST', 'instances/save', {'revision': revision, 'instance': {'id': 'qb', 'name': '旧页面'}})
        self.assertEqual(code, 409)
        new = {'type': 'qb', 'name': '第二个 qB', 'url': 'http://qb2.invalid:8080', 'username': 'another',
               'password': 'second-private', 'enabled': True, 'default': False}
        code, result, _ = self.request('POST', 'instances/save', {'revision': result['revision'], 'instance': new})
        self.assertEqual(code, 200)
        self.assertTrue(result['automation_paused'])
        self.assertFalse(self.controller.runtime['automatic_enabled'])
        self.assertEqual(len(result['items']), 3)
        self.assertNotIn('second-private', json.dumps(result))
        revision = result['revision']
        code, result, _ = self.request('POST', 'instances/delete', {'revision': revision, 'instance_id': 'tr',
            'confirm': 'REMOVE_INSTANCE_CONFIGURATION'})
        self.assertEqual(code, 200)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(self.controller.session_key(), before_auth)
        self.assertTrue(self.controller.authenticated(self.cookie.split('=', 1)[1].split(';', 1)[0]))
        self.assertEqual((self.directory / 'batch.json').read_bytes(), batch)
        self.assertFalse(any(call[0] in ('qremove', 'torrent-remove') for call in self.api.calls))

    def test_busy_fleet_blocks_config_logs_and_runner(self):
        self.controller.management.state['job'] = {'status': 'running'}
        revision = self.controller.registry.public()['revision']
        self.assertEqual(self.request('POST', 'instances/save', {'revision': revision, 'instance': {'id': 'qb', 'name': 'busy'}})[0], 409)
        self.assertEqual(self.request('POST', 'logs/cleanup', {'mode': 'all', 'confirm': 'CLEAR_MANAGED_LOGS'})[0], 409)
        self.assertEqual(self.request('POST', 'run', {})[0], 409)
        self.assertFalse(self.spawned)

    def test_logs_errors_expiry_and_automatic_cleanup_respect_active_runner(self):
        path = self.directory / 'run.log'
        rows = [{'time': '2000-01-01T00:00:00+00:00', 'event': 'progress'},
                {'time': '2026-10-02T22:00:00+08:00', 'event': 'run_failed', 'errors': 1},
                {'event': 'stopped', 'reason': 'target_reached'}]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        code, logs, _ = self.request('GET', 'logs?kind=error')
        self.assertEqual(code, 200)
        self.assertEqual([item['event'] for item in logs['items']], ['run_failed'])
        self.assertEqual(self.request('POST', 'logs/cleanup', {'mode': 'errors', 'confirm': 'wrong'})[0], 400)
        self.assertEqual(self.request('POST', 'logs/cleanup', {'mode': 'errors', 'confirm': 'CLEAR_MANAGED_LOGS'})[0], 200)
        self.assertNotIn('run_failed', path.read_text())
        self.assertEqual(self.request('GET', 'logs?kind=invalid')[0], 400)
        self.assertIn('target_reached', path.read_text())
        policy = {'auto_cleanup_enabled': True, 'retention_days': 30, 'cleanup_interval_hours': 24}
        self.assertEqual(self.request('POST', 'logs/settings', policy)[0], 200)
        self.controller.process = self.process
        before = path.read_bytes()
        self.controller.tick()
        self.assertEqual(path.read_bytes(), before)
        self.controller.process = None
        self.now += 24 * 3600
        self.controller.tick()
        self.assertNotIn('2000-01-01', path.read_text())
        self.assertIn('target_reached', path.read_text())

    def test_disabled_or_absent_default_qb_blocks_additions_but_not_tr_management(self):
        settings = self.controller.settings()
        settings['downloaders'] = [row for row in self.controller.registry.items() if row['type'] == 'tr']
        pull.save_json(self.config, settings)
        self.assertEqual(self.request('POST', 'automation', {'enabled': True})[0], 400)
        self.assertEqual(self.request('POST', 'run', {})[0], 400)
        with patch.object(self.api, 'inventory_one', return_value=list(self.api.tr.values())):
            code, value, _ = self.request('GET', 'fleet/tasks')
        self.assertEqual(code, 200)
        self.assertEqual([item['instance_id'] for item in value['items']], ['tr'])
        self.assertFalse(self.spawned)
        with patch('seedkeep_downloaders.API', return_value=self.api), patch.object(self.api, 'inventory_one', return_value=list(self.api.tr.values())):
            code, value, _ = self.request('GET', 'status')
        self.assertEqual(code, 200)
        self.assertEqual(value['connection'], {'qb': False, 'tr': True, 'error': None})
        self.assertEqual((value['qb_total'], value['tr_total']), (0, 1))

    def test_status_total_inventory_includes_unmanaged_tasks_and_deduplicates_enabled_instances(self):
        settings = self.controller.settings()
        qb, tr = self.controller.registry.items()
        second = dict(tr, id='tr-second', name='第二个 TR', url='http://tr2.invalid:9091/transmission/rpc')
        disabled = dict(tr, id='tr-off', name='停用 TR', url='http://off.invalid:9091/transmission/rpc', enabled=False)
        settings['downloaders'] = [qb, tr, second, disabled]
        pull.save_json(self.config, settings)
        shared, unmanaged_qb, unmanaged_tr, another_tr = (letter * 40 for letter in 'abcd')
        pull.save_json(self.directory / 'batch.json', {'accepted': {'1': {'hash': shared}}})
        before = (self.directory / 'batch.json').read_bytes()
        inventories = {qb['url']: [qb_task(shared), qb_task(unmanaged_qb, tags='')],
                       tr['url']: [tr_task(shared), tr_task(unmanaged_tr, labels=[])],
                       second['url']: [tr_task(unmanaged_tr, labels=[]), tr_task(another_tr, labels=[])]}
        reads = []

        class ReadOnlyAPI:
            def __init__(self, source, settings):
                self.source, self.settings = source, settings

            def inventory_one(self, kind):
                url = self.source['qb_url'] if kind == 'qb' else self.settings['tr_url']
                reads.append(url)
                return [dict(task) for task in inventories[url]]

        with patch('seedkeep_downloaders.API', side_effect=ReadOnlyAPI):
            self.controller.fleet.registry.api_factory = ReadOnlyAPI
            code, result, _ = self.request('GET', 'status')
        self.assertEqual(code, 200)
        self.assertIsNone(result['connection']['error'])
        self.assertEqual((result['qb_total'], result['tr_total']), (2, 3))
        self.assertEqual((result['managed_active'], result['managed_qb'], result['managed_tr']), (1, 1, 1))
        self.assertNotIn(disabled['url'], reads)
        self.assertEqual(len(reads), 9)
        self.assertEqual((self.directory / 'batch.json').read_bytes(), before)

    def test_pull_and_manager_append_after_cleanup_use_shared_logstore(self):
        self.controller.management.log('before_cleanup')
        with pull.private_logging(self.directory):
            pull.event('before_cleanup')
        self.assertEqual(self.request('POST', 'logs/cleanup', {'mode': 'all', 'confirm': 'CLEAR_MANAGED_LOGS'})[0], 200)
        self.controller.management.log('after_management')
        with pull.private_logging(self.directory):
            pull.event('after_pull', token='private-never-persist')
        events = {row['event'] for row in self.controller.logs()['items']}
        self.assertEqual(events, {'after_management', 'after_pull'})
        self.assertNotIn('private-never-persist', (self.directory / 'run.log').read_text())

    def test_log_settings_validation_is_atomic(self):
        original = self.controller.logstore.policy()
        for body in ({}, {**original, 'retention_days': True}, {**original, 'cleanup_interval_hours': 0}, {**original, 'extra': 1}):
            self.assertEqual(self.request('POST', 'logs/settings', body)[0], 400)
            self.assertEqual(self.controller.logstore.policy(), original)

    def rule_doc(self):
        code, doc, _ = self.request('GET', 'fleet/transfer/settings')
        self.assertEqual(code, 200)
        return doc

    def save_rules(self, **changes):
        doc = self.rule_doc()
        values = {**doc['values'], **changes}
        body = {'revision': doc['revision'], 'values': values}
        if values['enabled']:
            body['confirm'] = 'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA'
        code, saved, _ = self.request('POST', 'fleet/transfer/settings', body)
        self.assertEqual(code, 200, saved)
        return saved

    def downloader_writes(self):
        writes = {'qstop', 'qstart', 'qremove', 'qexport', 'torrent-add', 'torrent-set',
                  'torrent-stop', 'torrent-start', 'torrent-remove', 'torrent-verify'}
        return [call for call in self.api.calls if call[0] in writes]

    def large_selection(self):
        self.api.tr.clear()
        self.api.qb = {f'{n:040x}': qb_task(f'{n:040x}') for n in range(1, 2001)}
        return [{'instance_id': 'qb', 'hash': value} for value in reversed(self.api.qb)]

    def test_manual_transfer_large_http_selection_has_no_count_cap(self):
        tasks = self.large_selection()
        body = {'tasks': tasks, 'target_instance_id': 'tr', 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'}
        self.assertGreater(len(json.dumps(body).encode()), 131072)
        code, response, _ = self.request('POST', 'fleet/transfer', body)
        self.assertEqual(code, 200, response)
        self.assertEqual([item['hash'] for item in response['job']['items']], [item['hash'] for item in tasks])
        self.assertEqual(self.downloader_writes(), [])
        self.assertNotIn('transfer_max_per_job', self.request('GET', 'status')[1]['settings'])

    def test_rule_transfer_large_http_selection_uses_all_exact_references(self):
        tasks = self.large_selection()
        doc = self.save_rules(max_per_run=1)
        self.assertNotIn('max_per_run', doc['values'])
        code, response, _ = self.request('POST', 'fleet/transfer/run', {
            'tasks': tasks, 'revision': doc['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'})
        self.assertEqual(code, 200, response)
        self.assertEqual(response['counts']['selected'], len(tasks))
        self.assertEqual([item['hash'] for item in response['job']['items']], [item['hash'] for item in tasks])
        self.assertEqual(self.downloader_writes(), [])

    def test_rule_default_and_draft_preview_are_read_only_fresh_and_safe(self):
        files = lambda: {str(p.relative_to(self.directory)): p.read_bytes()
                         for p in self.directory.rglob('*') if p.is_file()}
        before = files()
        doc = self.rule_doc()
        self.assertFalse(doc['saved'])
        self.assertFalse(doc['values']['enabled'])
        self.assertEqual(len(doc['revision']), 64)
        self.assertEqual(before, files())
        values = {**doc['values'], 'delete_duplicate_source': True}
        code, preview, _ = self.request('POST', 'fleet/transfer/preview', {'values': values})
        self.assertEqual(code, 200, preview)
        self.assertEqual(preview['counts']['eligible'], 1)
        self.api.qb[self.api.hash]['tags'] = 'HR'
        code, preview, _ = self.request('POST', 'fleet/transfer/preview', {'values': values})
        self.assertEqual(code, 200)
        self.assertEqual(preview['items'][0]['reason_code'], 'excluded_tag')
        self.assertEqual(before, files())
        self.assertEqual(self.controller.fleet.managers, {})
        self.assertEqual(self.downloader_writes(), [])
        for private in ('test-secret', 'test-password', 'fake-private', 'qb.invalid', 'tr.invalid', '/downloads/PTS'):
            self.assertNotIn(private, json.dumps(preview))

    def test_rule_http_validation_and_revision_failures_are_atomic(self):
        doc = self.save_rules()
        rules = self.controller.fleet.transfer_rules
        before = (rules.path.read_bytes(), rules.state_path.read_bytes())
        bad_values = ({**doc['values'], 'max_per_run': True}, {**doc['values'], 'enabled': 1},
                      {**doc['values'], 'extra': 'ignored'}, {**doc['values'], 'cron': '* * * * 7'})
        for values in bad_values:
            with self.subTest(values=values):
                self.assertEqual(self.request('POST', 'fleet/transfer/settings',
                    {'revision': doc['revision'], 'values': values})[0], 400)
                self.assertEqual(self.request('POST', 'fleet/transfer/preview', {'values': values})[0], 400)
        for revision in (None, '旧版本', 'z' * 64, '0' * 64):
            self.assertEqual(self.request('POST', 'fleet/transfer/settings',
                {'revision': revision, 'values': doc['values']})[0], 409)
            self.assertEqual(self.request('POST', 'fleet/transfer/run',
                {'revision': revision, 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'})[0], 409)
        self.assertEqual(self.request('POST', 'fleet/transfer/settings',
            {'revision': doc['revision'], 'values': doc['values'], 'extra': True})[0], 400)
        self.assertEqual(self.request('POST', 'fleet/transfer/preview', {'values': doc['values'], 'extra': True})[0], 400)
        self.assertEqual(self.request('POST', 'fleet/transfer/run', {'revision': doc['revision'], 'confirm': 'wrong'})[0], 400)
        self.assertEqual((rules.path.read_bytes(), rules.state_path.read_bytes()), before)
        self.assertEqual(self.downloader_writes(), [])

    def test_rule_save_does_not_run_and_execution_uses_latest_saved_snapshot(self):
        self.api.tr.clear()
        initial = self.rule_doc()
        self.assertEqual(self.request('POST', 'fleet/transfer/run',
            {'revision': initial['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'})[0], 409)
        first = self.save_rules(delete_source=False, start_after_verify=False, target_labels=['第一版'])
        latest = self.save_rules(target_labels=['保存快照'])
        self.assertNotEqual(first['revision'], latest['revision'])
        self.assertEqual(self.downloader_writes(), [])
        self.assertIsNone(self.controller.fleet.public_job())
        self.assertEqual(self.request('POST', 'fleet/transfer/run',
            {'revision': first['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'})[0], 409)
        code, response, _ = self.request('POST', 'fleet/transfer/run',
            {'revision': latest['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA',
             'tasks': [{'instance_id': 'qb', 'hash': self.api.hash}]})
        self.assertEqual(code, 200, response)
        self.assertTrue(response['job']['rule_run'])
        self.assertEqual((response['job']['source_instance_id'], response['job']['target_instance_id']), ('qb', 'tr'))
        manager = self.controller.fleet.managers[('qb', 'tr')]
        self.assertEqual(web.load_json(manager.state_path)['job']['transfer_options'], latest['values'])
        self.controller.fleet._tick()
        self.assertEqual(manager.state['job']['verification_mode'], 'native')
        self.assertEqual(manager.state['job']['items'][0]['phase'], 'verified')
        self.assertFalse(any(call[0] == 'torrent-verify' for call in self.api.calls))
        self.api.tr[self.api.hash].update(status=0, haveValid=100, haveUnchecked=0)
        self.now += 20
        self.controller.fleet._tick()
        done = self.rule_doc()['runtime']['last_result']
        self.assertEqual((done['status'], done['source_kept'], done['source_removed']), ('completed', 1, 0))
        self.assertEqual(self.api.qb[self.api.hash]['state'], 'uploading')
        self.assertEqual(self.api.tr[self.api.hash]['status'], 0)
        self.assertEqual(self.api.tr[self.api.hash]['labels'], ['pts保种组', '保存快照'])
        self.assertFalse(any(call[0] == 'qremove' for call in self.api.calls))

    def test_rule_selected_request_never_substitutes_an_invalid_task(self):
        self.api.tr.clear()
        doc = self.save_rules()
        for tasks in ([{'instance_id': 'tr', 'hash': self.api.hash}],
                      [{'instance_id': 'qb', 'hash': 'a' * 40}],
                      [{'instance_id': 'qb', 'hash': self.api.hash}, {'instance_id': 'tr', 'hash': self.api.hash}]):
            self.assertEqual(self.request('POST', 'fleet/transfer/run',
                {'revision': doc['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA', 'tasks': tasks})[0], 409)
        self.assertIsNone(self.controller.fleet.public_job())
        self.assertEqual(self.downloader_writes(), [])

    def test_rule_mutations_and_due_run_are_blocked_by_active_refill(self):
        doc = self.save_rules(enabled=True)
        rules = self.controller.fleet.transfer_rules
        due = rules.state['next_run_at']
        self.now = due + 1
        self.request('POST', 'login', {'username': 'test-user', 'password': 'test-password'})
        self.controller.process = self.process
        before = rules.state_path.read_bytes()
        self.assertEqual(self.request('POST', 'fleet/transfer/settings',
            {'revision': doc['revision'], 'values': doc['values'],
             'confirm': 'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA'})[0], 409)
        self.assertEqual(self.request('POST', 'fleet/transfer/run',
            {'revision': doc['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'})[0], 409)
        with patch.object(rules, 'plan', side_effect=AssertionError('refill must block scheduling before reads')):
            self.controller.fleet._tick()
        self.assertEqual(rules.state_path.read_bytes(), before)
        self.assertEqual(rules.state['next_run_at'], due)
        self.assertEqual(self.downloader_writes(), [])

    def test_rule_enable_confirmation_and_identity_change_pause_persist(self):
        doc = self.rule_doc()
        enabled = {**doc['values'], 'enabled': True}
        self.assertEqual(self.request('POST', 'fleet/transfer/settings',
            {'revision': doc['revision'], 'values': enabled})[0], 400)
        doc = self.save_rules(enabled=True)
        self.assertTrue(doc['values']['enabled'])
        self.assertIsNotNone(doc['runtime']['next_run_at'])
        revision = self.controller.registry.public()['revision']
        code, renamed, _ = self.request('POST', 'instances/save',
            {'revision': revision, 'instance': {'id': 'qb', 'name': '仅改名'}})
        self.assertEqual(code, 200)
        self.assertTrue(self.rule_doc()['values']['enabled'])
        self.assertEqual(self.request('POST', 'fleet/transfer/run',
            {'revision': doc['revision'], 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'})[0], 409)
        code, changed, _ = self.request('POST', 'instances/save',
            {'revision': renamed['revision'], 'instance': {'id': 'qb', 'url': 'http://qb-new.invalid:8080'}})
        self.assertEqual(code, 200, changed)
        self.assertTrue(changed['automation_paused'])
        paused = self.rule_doc()
        self.assertFalse(paused['values']['enabled'])
        self.assertIsNone(paused['runtime']['next_run_at'])
        self.assertFalse(web.load_json(self.controller.fleet.transfer_rules.path)['enabled'])
        self.assertEqual(self.downloader_writes(), [])


    def pending_entry(self, key, values, **extras):
        c = self.controller
        revisions = {'configuration': c.configuration()['revision'], 'storage': c.fleet.cleanup.storage.public()['revision'],
                     'transfer': c.fleet.transfer_rules.revision(), 'category': c.fleet.cleanup._revision()}
        if key.startswith('labels:'):
            revisions[key] = c.registry.public()['revision']
        entry = {'key': key, 'values': values, **extras}
        if key in revisions:
            entry['document'] = {'revision': revisions[key]}
        return entry

    def queue_settings(self, entries):
        return self.request('POST', 'settings/defer', {'entries': entries})

    def execution_files(self):
        return {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob('*')
                if p.is_file() and p.name != 'settings_pending.json'}

    def test_deferred_http_jobs_save_only_queue_and_apply_once(self):
        for kind in ('refill', 'transfer', 'cleanup', 'needs_review', 'waiting'):
            with self.subTest(kind=kind):
                c = self.controller
                c.process = self.process if kind == 'refill' else None
                c.management.state['job'] = {'status': kind if kind == 'waiting' else 'running'} if kind in ('transfer', 'waiting') else None
                c.fleet.cleanup.state['job'] = {'status': kind if kind == 'needs_review' else 'running'} if kind in ('cleanup', 'needs_review') else None
                value = c.settings()['target'] + 1
                before = self.execution_files()
                code, result, _ = self.queue_settings([self.pending_entry('runtime', {'target': value})])
                self.assertEqual(code, 200, result)
                self.assertTrue(result['deferred'])
                self.assertEqual(result['pending']['status'], 'pending')
                self.assertEqual(self.execution_files(), before)
                queue = c.directory / 'settings_pending.json'
                queued = queue.read_bytes()
                self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': value})])[0], 200)
                self.assertEqual(queue.read_bytes(), queued)
                c.tick()
                self.assertEqual(c.settings()['target'], value - 1)
                c.process = None
                c.management.state['job'] = None
                c.fleet.cleanup.state['job'] = None
                c.tick()
                self.assertEqual(c.settings()['target'], value)
                pending = self.request('GET', 'settings/pending')[1]
                self.assertEqual((pending['status'], pending['entries'], pending['count']), ('applied', [], 0))
                applied = self.execution_files()
                c.tick()
                self.assertEqual(self.execution_files(), applied)
                if os.name != 'nt':
                    self.assertEqual(queue.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.downloader_writes(), [])

    def test_deferred_idle_fallback_does_not_write(self):
        before = self.execution_files()
        code, result, _ = self.queue_settings([self.pending_entry('runtime', {'target': 711})])
        self.assertEqual((code, result), (200, {'ok': True, 'deferred': False}))
        self.assertEqual(self.execution_files(), before)
        self.assertFalse((self.directory / 'settings_pending.json').exists())
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'none')

    def test_deferred_invalid_groups_and_revisions_are_zero_write(self):
        c = self.controller
        c.process = self.process
        bad = [self.pending_entry('runtime', {'target': True}),
               self.pending_entry('labels:missing', {'id': 'missing', 'category': 'x', 'tag': 'y'}),
               self.pending_entry('storage', {'mappings': [{'instance_id': 'qb'}]}),
               self.pending_entry('policy', {**c.fleet.policy(), 'unregistered_instances': ['missing']}),
               self.pending_entry('logs', {'auto_cleanup_enabled': False, 'retention_days': 0, 'cleanup_interval_hours': 24}),
               self.pending_entry('transfer', {**c.fleet.transfer_rules.values, 'enabled': True}),
               self.pending_entry('category', {'rules': [{**self.cleanup_values()['rules'][0], 'enabled': True}]}),
               {'key': 'configuration', 'values': {'revision': c.configuration()['revision'], 'values': {'token': 123}}}]
        for entry in bad:
            with self.subTest(key=entry['key']):
                before = self.execution_files()
                code, result, _ = self.queue_settings([self.pending_entry('runtime', {'target': 711}), entry])
                self.assertIn(code, (400, 404), result)
                self.assertEqual(self.execution_files(), before)
                self.assertFalse((self.directory / 'settings_pending.json').exists())
        good = self.pending_entry('labels:qb', {'id': 'qb', 'category': 'saved', 'tag': 'saved'})
        self.assertEqual(self.queue_settings([good])[0], 200)
        queued = (self.directory / 'settings_pending.json').read_bytes()
        stale = self.pending_entry('storage', {'mappings': []})
        stale['document']['revision'] = '0' * 64
        self.assertEqual(self.queue_settings([stale])[0], 409)
        self.assertEqual((self.directory / 'settings_pending.json').read_bytes(), queued)

    def test_deferred_merge_secrets_mixed_revisions_and_schedule(self):
        c = self.controller
        c.automation(True)
        schedule = copy.deepcopy(c.runtime)
        c.process = self.process
        configuration_entry = {'key': 'configuration', 'values': {'revision': c.configuration()['revision'],
                               'values': {'token': 'queued-private-token'}}}
        self.assertEqual(self.queue_settings([configuration_entry])[0], 200)
        labels = self.pending_entry('labels:qb', {'id': 'qb', 'category': 'later', 'tag': 'later'})
        entries = [self.pending_entry('runtime', {'min_seeders': 3}), labels,
                   self.pending_entry('storage', {'mappings': []}),
                   self.pending_entry('policy', c.fleet.policy()),
                   self.pending_entry('transfer', copy.deepcopy(c.fleet.transfer_rules.values)),
                   self.pending_entry('category', self.cleanup_values()),
                   self.pending_entry('logs', c.logstore.policy())]
        code, result, _ = self.queue_settings(entries)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['pending']['count'], 8)
        public = json.dumps(self.request('GET', 'settings/pending')[1])
        self.assertNotIn('queued-private-token', public)
        self.assertNotIn('test-password', public)
        self.assertNotIn('token', result['pending']['entries'][0]['values']['values'])
        self.assertIn('queued-private-token', (self.directory / 'settings_pending.json').read_text(encoding='utf-8'))
        c.process = None
        c.tick()
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')
        self.assertEqual(c.source()['token'], 'queued-private-token')
        self.assertEqual(c.settings()['min_seeders'], 3)
        self.assertEqual(c.registry.items()[0]['category'], 'later')
        self.assertFalse(c.runtime['automatic_enabled'])
        self.assertFalse(c.fleet.transfer_rules.values['enabled'])
        self.assertFalse(c.fleet.cleanup.values['rules'][0]['enabled'])
        self.assertEqual(self.downloader_writes(), [])
        self.assertNotIn('queued-private-token', json.dumps(c.logs()))

    def test_deferred_unrelated_changes_keep_automatic_schedule(self):
        c = self.controller
        c.automation(True)
        schedule = copy.deepcopy(c.runtime)
        c.process = self.process
        entries = [self.pending_entry('runtime', {'min_seeders': 3}),
                   self.pending_entry('labels:qb', {'id': 'qb', 'category': 'later', 'tag': 'later'})]
        self.assertEqual(self.queue_settings(entries)[0], 200)
        c.process = None
        c.tick()
        self.assertEqual(c.runtime, schedule)
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')

    def test_deferred_baseline_conflict_is_visible_and_preserves_queue(self):
        c = self.controller
        c.process = self.process
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 711})])[0], 200)
        source = pull.Path(c.settings()['source_config'])
        values = web.load_json(source)
        values['tag'] = 'external'
        pull.save_json(source, values)
        queued = (self.directory / 'settings_pending.json').read_bytes()
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 712})])[0], 409)
        self.assertEqual((self.directory / 'settings_pending.json').read_bytes(), queued)
        c.process = None
        c.tick()
        result = self.request('GET', 'settings/pending')[1]
        self.assertEqual((result['status'], result['count']), ('failed', 1))
        self.assertIn('基线', result['error'])
        self.assertEqual(c.settings()['target'], 700)
        before = (self.directory / 'settings_pending.json').read_bytes()
        c.tick()
        self.assertEqual((self.directory / 'settings_pending.json').read_bytes(), before)

    def test_deferred_resave_and_failed_retry_preserve_private_configuration(self):
        c = self.controller
        c.process = self.process
        values = {'revision': c.configuration()['revision'], 'values': {'token': 'queued-private-token'}}
        self.assertEqual(self.queue_settings([self.pending_entry('configuration', values)])[0], 200)
        values['values'] = {'api_base': c.configuration()['values']['api_base'], 'token': ''}
        self.assertEqual(self.queue_settings([self.pending_entry('configuration', values)])[0], 200)
        self.assertEqual(c.pending['entries'][0]['values']['values']['token'], 'queued-private-token')
        c.process = None
        with patch.object(c, 'update_configuration', side_effect=OSError('private')):
            c.tick()
        public = self.request('GET', 'settings/pending')[1]
        self.assertEqual(public['status'], 'failed')
        self.assertNotIn('queued-private-token', json.dumps(public))
        retry = public['entries'][0]
        retry['values']['revision'] = c.configuration()['revision']
        retry['values']['values']['token'] = ''
        self.assertEqual(self.queue_settings([retry])[0], 200)
        c.tick()
        self.assertEqual(c.source()['token'], 'queued-private-token')
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')
        self.assertNotIn('queued-private-token', json.dumps(self.request('GET', 'settings/pending')[1]))

    def test_deferred_partial_failure_stops_and_explicit_save_retries(self):
        c = self.controller
        c.process = self.process
        logs = {'auto_cleanup_enabled': False, 'retention_days': 11, 'cleanup_interval_hours': 24}
        entries = [self.pending_entry('runtime', {'target': 711}), self.pending_entry('logs', logs)]
        self.assertEqual(self.queue_settings(entries)[0], 200)
        c.process = None
        with patch.object(c, 'update_log_settings', side_effect=OSError('https://private.invalid token=private')):
            c.tick()
            c.tick()
        result = self.request('GET', 'settings/pending')[1]
        self.assertEqual(c.settings()['target'], 711)
        self.assertEqual((result['status'], result['count']), ('failed', 1))
        self.assertEqual(result['results'], {'applied': ['runtime'], 'failed': ['logs']})
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(self.queue_settings([self.pending_entry('logs', logs)])[0], 200)
        c.tick()
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')
        self.assertEqual(c.logstore.policy(), logs)

    def test_deferred_restart_waits_for_runner_lock_and_manager_recovery(self):
        c = self.controller
        c.process = self.process
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 711})])[0], 200)
        restarted = web.Controller(self.config, clock=lambda: self.now)
        self.server.controller = restarted
        with patch.object(restarted, 'file_lock', side_effect=web.ApiError('正在拉取', 409)):
            restarted.tick()
        self.assertEqual(restarted.settings()['target'], 700)
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'pending')
        restarted.management.state['job'] = {'status': 'waiting'}
        with patch.object(restarted, 'file_lock', side_effect=contextlib.nullcontext):
            restarted.tick()
            self.assertEqual(restarted.settings()['target'], 700)
            restarted.management.state['job'] = None
            restarted.tick()
        self.assertEqual(restarted.settings()['target'], 711)
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')

    def test_deferred_real_transfer_restart_recovers_before_apply(self):
        c = self.controller
        self.api.tr.clear()
        code, job, _ = self.request('POST', 'fleet/transfer', {'tasks': [{'instance_id': 'qb', 'hash': self.api.hash}],
                                  'target_instance_id': 'tr', 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.assertEqual(code, 200, job)
        before = self.execution_files()
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 711})])[0], 200)
        self.assertEqual(self.execution_files(), before)
        self.assertEqual(self.downloader_writes(), [])
        restarted = web.Controller(self.config, clock=lambda: self.now)
        self.server.controller = restarted
        self.assertEqual(restarted.fleet.public_job()['id'], job['job']['id'])
        manager = next(iter(restarted.fleet.managers.values()))
        manager.client_factory = lambda *args: self.api
        with patch.object(restarted, 'file_lock', side_effect=contextlib.nullcontext):
            restarted.tick()
            self.assertEqual(restarted.settings()['target'], 700)
            restarted.monitor_step()
            self.assertTrue(restarted.fleet.busy())
            self.assertEqual(restarted.settings()['target'], 700)
            restarted.fleet.cancel({'job_id': job['job']['id']})
            restarted.monitor_step()
            self.assertFalse(restarted.fleet.busy())
            restarted.tick()
        self.assertEqual(restarted.settings()['target'], 711)
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')

    def test_deferred_monitor_and_scheduler_apply_before_new_auto_jobs(self):
        c = self.controller
        c.process = self.process
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 711})])[0], 200)
        c.process = None
        with patch.object(c, 'file_lock', side_effect=web.ApiError('正在拉取', 409)), patch.object(c.fleet, '_tick') as monitor:
            c.monitor_step()
            monitor.assert_not_called()
        with patch.object(c.fleet, '_tick', side_effect=lambda: self.assertEqual(c.settings()['target'], 711)) as monitor:
            c.monitor_step()
            monitor.assert_called_once()
        c.process = self.process
        c.automation(True)
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'min_seeders': 4})])[0], 200)
        c.process = None
        self.now = c.runtime['next_run_at']
        with patch.object(c, 'start_run', side_effect=lambda: self.assertEqual(c.settings()['min_seeders'], 4)) as start:
            c.tick()
            start.assert_called_once()

    def test_deferred_baseline_checks_every_saved_group_but_not_observations(self):
        c = self.controller
        paths = [self.config, pull.Path(c.settings()['source_config']), self.directory / 'web_auth.json',
                 c.fleet.policy_path, c.fleet.transfer_rules.path, c.fleet.cleanup.path,
                 c.fleet.cleanup.storage.path, self.directory / 'log_policy.json']
        for path in paths:
            with self.subTest(file=path.name):
                c.process = self.process
                self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 711})])[0], 200)
                original = path.read_bytes() if path.exists() else None
                if path == self.config:
                    values = c.settings()
                    values['cron_minute'] = 18
                elif path.name == 'source.json':
                    values = web.load_json(path)
                    values['tag'] = 'external'
                elif path == c.fleet.policy_path:
                    values = {**c.fleet.policy(), 'cleanup_wait_hours': 13}
                elif path == c.fleet.transfer_rules.path:
                    values = {**c.fleet.transfer_rules.values, 'notify': not c.fleet.transfer_rules.values['notify']}
                elif path == c.fleet.cleanup.path:
                    values = self.cleanup_values()
                elif path == c.fleet.cleanup.storage.path:
                    # Malformed external mappings also must fail safely without overwriting them.
                    values = {'mappings': [{'external': True}]}
                elif path.name == 'log_policy.json':
                    values = {'auto_cleanup_enabled': False, 'retention_days': 12, 'cleanup_interval_hours': 24}
                else:
                    values = {'username': 'test-user', 'password': 'test-password'}
                pull.save_json(path, values)
                external = path.read_bytes()
                c.process = None
                c.tick()
                result = self.request('GET', 'settings/pending')[1]
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(c.settings()['target'], 700)
                self.assertEqual(path.read_bytes(), external)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)
        c.process = self.process
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 711})])[0], 200)
        c.fleet.state['observations']['poll'] = {'last_seen': self.now}
        c.fleet._save()
        c.runtime['next_run_at'] = self.now + 111
        c.save_runtime()
        c.process = None
        c.tick()
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')

    def test_deferred_same_key_replacement_and_saved_category_mode(self):
        c = self.controller
        doc = c.fleet.cleanup.public()
        values = self.cleanup_values()
        values['rules'][0].update(enabled=False, observe_only=False)
        code, saved, _ = self.request('POST', 'fleet/cleanup/settings', {'revision': doc['revision'], 'values': values})
        self.assertEqual(code, 200)
        code, saved, _ = self.request('POST', 'fleet/cleanup/mode', {'revision': saved['revision'], 'rule_id': 'r1', 'action': 'pause'})
        self.assertEqual(code, 200)
        c.process = self.process
        entries = [self.pending_entry('category', saved['values']), self.pending_entry('runtime', {'min_seeders': 3})]
        self.assertEqual(self.queue_settings(entries)[0], 200)
        first = self.request('GET', 'settings/pending')[1]
        self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'min_seeders': 4})])[0], 200)
        replaced = self.request('GET', 'settings/pending')[1]
        self.assertEqual(first['id'], replaced['id'])
        self.assertEqual(first['queued_at'], replaced['queued_at'])
        self.assertEqual([e['key'] for e in first['entries']], [e['key'] for e in replaced['entries']])
        c.process = None
        c.tick()
        self.assertEqual(c.settings()['min_seeders'], 4)
        self.assertFalse(c.fleet.cleanup.values['rules'][0]['enabled'])
        self.assertFalse(c.fleet.cleanup.values['rules'][0]['observe_only'])

    def test_deferred_queue_chmod_and_atomic_failure_preserve_old_queue(self):
        c = self.controller
        c.process = self.process
        entry = self.pending_entry('runtime', {'target': 711})
        with patch('seedkeep_web.os.chmod', wraps=os.chmod) as chmod:
            self.assertEqual(self.queue_settings([entry])[0], 200)
            self.assertIn(0o600, [call.args[1] for call in chmod.call_args_list])
        before = (self.directory / 'settings_pending.json').read_bytes()
        state = copy.deepcopy(c.pending)
        with patch('seedkeep_web.os.replace', side_effect=OSError('atomic replace failed')):
            self.assertEqual(self.queue_settings([self.pending_entry('runtime', {'target': 712})])[0], 500)
        self.assertEqual((self.directory / 'settings_pending.json').read_bytes(), before)
        self.assertEqual(c.pending, state)
        self.assertEqual(list(self.directory.glob('.settings_pending.*')), [])

    def test_deferred_sensitive_partial_checkpoint_keeps_pauses_on_restart_retry(self):
        c = self.controller
        # Model an already explicitly enabled saved category rule.
        category = self.cleanup_values()
        category['rules'][0].update(enabled=True, observe_only=False)
        c.fleet.cleanup.values = copy.deepcopy(category)
        pull.save_json(c.fleet.cleanup.path, category)
        c.automation(True)
        c.process = self.process
        configuration_entry = {'key': 'configuration', 'values': {'revision': c.configuration()['revision'],
                               'values': {'token': 'new-private-token'}}}
        rules = {**c.fleet.transfer_rules.values, 'enabled': True}
        entries = [configuration_entry, self.pending_entry('storage', {'mappings': []}),
                   self.pending_entry('policy', {**c.fleet.policy(), 'cleanup_enabled': True, 'unregistered_enabled': True}),
                   self.pending_entry('transfer', rules, confirm='ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA'),
                   self.pending_entry('category', category)]
        self.assertEqual(self.queue_settings(entries)[0], 200)
        c.process = None
        with patch.object(c.fleet.cleanup, 'update_storage', side_effect=OSError('storage failure')):
            c.tick()
        failed = self.request('GET', 'settings/pending')[1]
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['results']['applied'], ['configuration'])
        self.assertTrue(failed['automation_paused'])
        pending = {entry['key']: entry['values'] for entry in failed['entries']}
        self.assertFalse(pending['category']['rules'][0]['enabled'])
        self.assertFalse(pending['transfer']['enabled'])
        self.assertFalse(pending['policy']['cleanup_enabled'])
        restarted = web.Controller(self.config, clock=lambda: self.now)
        self.server.controller = restarted
        with patch.object(restarted, 'file_lock', side_effect=contextlib.nullcontext):
            restarted.tick()
            self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'failed')
            storage = {'key': 'storage', 'values': {'mappings': []},
                       'document': {'revision': restarted.fleet.cleanup.storage.public()['revision']}}
            self.assertEqual(self.queue_settings([storage])[0], 200)
            restarted.tick()
        self.assertEqual(self.request('GET', 'settings/pending')[1]['status'], 'applied')
        self.assertFalse(restarted.runtime['automatic_enabled'])
        self.assertFalse(restarted.fleet.config['cleanup_enabled'])
        self.assertFalse(restarted.fleet.transfer_rules.values['enabled'])
        self.assertFalse(restarted.fleet.cleanup.values['rules'][0]['enabled'])
        self.assertEqual(self.downloader_writes(), [])

    def test_deferred_file_lock_reentrant_only_within_thread_and_preserves_guard(self):
        c = self.controller
        calls, errors = [], []
        def flock(stream, flags):
            calls.append((threading.get_ident(), flags))
            if len(calls) > 1:
                raise BlockingIOError('held by the applying thread')
        shim = type('FcntlShim', (), {'LOCK_EX': 1, 'LOCK_NB': 4, 'flock': staticmethod(flock)})()
        original = web.Controller.file_lock.__get__(c)
        with patch.dict('sys.modules', {'fcntl': shim}), patch.object(c, 'file_lock', original):
            with c.file_lock():
                with c.file_lock():
                    with c.operation_guard():
                        pass
                def other_thread():
                    try:
                        with c.file_lock():
                            errors.append('lock incorrectly granted')
                    except web.ApiError as error:
                        errors.append(error.status)
                thread = threading.Thread(target=other_thread)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [409])
            self.assertFalse(c._file_lock_local.held)
        self.assertEqual(len(calls), 2)

    def cleanup_values(self):
        from test_seedkeep_cleanup import rule
        return {'rules': [rule(scopes=[{'instance_id': 'qb', 'values': ['PTS'], 'include_empty': False}])]}

    def test_cleanup_default_and_draft_preview_have_no_runtime_writes_or_deletes(self):
        code, doc, _ = self.request('GET', 'fleet/cleanup/settings')
        self.assertEqual(code, 200)
        self.assertFalse(doc['saved'])
        self.assertEqual(doc['values'], {'rules': []})
        code, preview, _ = self.request('POST', 'fleet/cleanup/preview', {'values': self.cleanup_values(), 'rule_id': 'r1'})
        self.assertEqual(code, 200, preview)
        self.assertEqual(preview['rules'][0]['id'], 'r1')
        self.assertFalse(self.controller.fleet.cleanup.path.exists())
        self.assertFalse(self.controller.fleet.cleanup.state_path.exists())
        self.assertEqual(self.downloader_writes(), [])
        self.assertNotIn('fake-private', json.dumps(preview))

    def test_cleanup_save_requires_revision_and_does_not_enable_execution(self):
        code, doc, _ = self.request('GET', 'fleet/cleanup/settings')
        values = self.cleanup_values()
        code, saved, _ = self.request('POST', 'fleet/cleanup/settings', {'revision': doc['revision'], 'values': values})
        self.assertEqual(code, 200, saved)
        self.assertTrue(saved['saved'])
        self.assertFalse(saved['values']['rules'][0]['enabled'])
        self.assertTrue(saved['values']['rules'][0]['observe_only'])
        self.assertEqual(self.request('POST', 'fleet/cleanup/settings', {'revision': doc['revision'], 'values': values})[0], 409)
        attempted = self.cleanup_values()
        attempted['rules'][0].update(enabled=True, observe_only=False)
        self.assertEqual(self.request('POST', 'fleet/cleanup/settings', {'revision': saved['revision'], 'values': attempted})[0], 400)
        self.assertEqual(self.downloader_writes(), [])

    def test_cleanup_enable_requires_confirmation_and_available_inspection(self):
        doc = self.request('GET', 'fleet/cleanup/settings')[1]
        saved = self.request('POST', 'fleet/cleanup/settings', {'revision': doc['revision'], 'values': self.cleanup_values()})[1]
        body = {'revision': saved['revision'], 'rule_id': 'r1', 'action': 'enable'}
        self.assertEqual(self.request('POST', 'fleet/cleanup/mode', body)[0], 400)
        body['confirm'] = 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'
        self.assertEqual(self.request('POST', 'fleet/cleanup/mode', body)[0], 409)
        current = self.request('GET', 'fleet/cleanup/settings')[1]
        self.assertFalse(current['values']['rules'][0]['enabled'])
        self.assertEqual(self.downloader_writes(), [])

    def test_cleanup_saved_observe_mode_and_pause_return_complete_documents(self):
        doc = self.request('GET', 'fleet/cleanup/settings')[1]
        saved = self.request('POST', 'fleet/cleanup/settings', {'revision': doc['revision'], 'values': self.cleanup_values()})[1]
        for action in ('pause', 'observe'):
            code, result, _ = self.request('POST', 'fleet/cleanup/mode', {'revision': saved['revision'], 'rule_id': 'r1', 'action': action})
            self.assertEqual(code, 200, result)
            self.assertEqual(len(result['revision']), 64)
            self.assertEqual(len(result['instances_revision']), 64)
            self.assertFalse(result['values']['rules'][0]['enabled'])
            self.assertEqual(result['values']['rules'][0]['observe_only'], action == 'observe')
            saved = result
        self.assertEqual(self.downloader_writes(), [])


if __name__ == '__main__':
    unittest.main()
