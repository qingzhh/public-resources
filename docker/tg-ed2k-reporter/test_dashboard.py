from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

from app import Service, load_settings, main
from dashboard import Auth, Control, MAX_WEB_TEXT_BYTES, WebError, WebServer, create_app, input_text, password_record
from ed2k import Message
from state import Store

PASSWORD = 'test-only-web-password'
REPLACEMENT = 'replacement-test-password'
LINK = 'ed2k://|file|test.mkv|123|0123456789abcdef0123456789abcdef|/'
BROKEN = '解析 ED2K 失败: ed2k://|file|烈焰狂沙.Lie.Yan.Kuang.Sha.2026.2160p.WEB-DL.H.265.HDR.DDP5.1.2Audios-HHWEB.mkv|10989138278|52b96bd023ab3ce9435429f4d01f2363| err=结构错误plugin/ed2k_hash_reporter.go:184'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)
        self.addCleanup(self.store.close)
        self.config = {**password_record('admin', PASSWORD), 'allowed_hosts': ['localhost']}
        self.auth = Auth(self.config, self.directory)
        self.control = Control()
        self.settings = load_settings(Path(__file__).with_name('settings.json'))
        self.settings['report_enabled'] = False
        self.service = Service(self.store, self.settings, {}, 'http://proxy.invalid:3128', control=self.control)
        self.service.collector.collect = Mock(return_value={'new': 0, 'pages': 1})
        self.application = create_app(self.directory, self.settings, self.auth, self.control)
        self.client = self.application.test_client()

    def login(self):
        response = self.client.post('/api/login', json={'username': 'admin', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json['csrf_token']
        return response

    def post(self, route, value):
        return self.client.post(route, json=value, headers={'X-CSRF-Token': self.csrf})

    def ingest(self, text=LINK):
        self.store.ingest([Message('web', 1, time.time(), text)], time.time())
        self.store.export(self.directory / 'normalized.txt')

    def action(self, name, **values):
        response = self.post('/api/actions', {'action': name, **values})
        self.assertEqual(response.status_code, 202)
        self.service.commands()
        return self.client.get('/api/jobs/' + response.json['job']['id']).json['job']


class AuthTests(Fixture):
    def test_anonymous_api_requires_login(self):
        self.assertFalse(self.client.get('/api/session').json['authenticated'])
        for route in ('/api/status', '/api/items', '/api/export', '/api/jobs/' + 'a' * 32):
            self.assertEqual(self.client.get(route).status_code, 401)
        self.assertEqual(self.client.post('/api/actions', json={'action': 'pause_reporting'}).status_code, 401)

    def test_cookie_and_security_headers(self):
        response = self.login()
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertTrue(self.client.get('/api/session').json['authenticated'])

    def test_invalid_password_throttles_and_expires(self):
        clock = [10.0]
        self.auth.clock = lambda: clock[0]
        for _ in range(10):
            with self.assertRaisesRegex(WebError, 'login_failed'):
                self.auth.login('admin', 'wrong', 'test-peer')
        with self.assertRaisesRegex(WebError, 'login_throttled'):
            self.auth.login('admin', PASSWORD, 'test-peer')
        clock[0] += 301
        token, _ = self.auth.login('admin', PASSWORD, 'test-peer')
        clock[0] += 12 * 3600
        self.assertIsNone(self.auth.session(token))

    def test_csrf_and_cross_origin_are_rejected(self):
        self.login()
        self.assertEqual(self.client.post('/api/actions', json={'action': 'pause_reporting'}).status_code, 403)
        self.assertEqual(self.client.post('/api/actions', json={'action': 'pause_reporting'}, headers={'X-CSRF-Token': self.csrf, 'Origin': 'https://example.invalid'}).status_code, 403)
        self.assertEqual(self.client.post('/api/login', json={}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.client.get('/api/session', headers={'Host': 'evil.invalid'}).status_code, 403)
        self.assertFalse(self.control.pause_requested.is_set())

    def test_change_password_invalidates_all_sessions_and_survives_restart(self):
        self.login()
        token, _ = self.auth.login('admin', PASSWORD, 'second-peer')
        self.assertEqual(self.post('/api/password', {'current_password': 'wrong', 'new_password': REPLACEMENT}).status_code, 403)
        self.assertEqual(self.post('/api/password', {'current_password': PASSWORD, 'new_password': 'short'}).status_code, 400)
        self.assertEqual(self.post('/api/password', {'current_password': PASSWORD, 'new_password': REPLACEMENT}).status_code, 200)
        self.assertIsNone(self.auth.session(token))
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        reopened = Auth(self.config, self.directory)
        self.assertFalse(reopened.valid_password(PASSWORD))
        self.assertTrue(reopened.valid_password(REPLACEMENT))
        self.assertNotIn(REPLACEMENT, reopened.path.read_text())

    def test_logout_revokes_session(self):
        self.login()
        self.assertEqual(self.post('/api/logout', {}).status_code, 200)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_saved_credentials_fail_closed(self):
        self.auth.path.write_text('{"username":"admin"}')
        with self.assertRaises(WebError):
            Auth(self.config, self.directory)


class ApiTests(Fixture):
    def test_static_assets_and_private_paths(self):
        for route in ('/', '/style.css', '/app.js'):
            with self.client.get(route) as response:
                self.assertEqual(response.status_code, 200)
        for route in ('/state.sqlite3', '/.web-auth.json', '/settings.json', '/secrets/report.json'):
            self.assertEqual(self.client.get(route).status_code, 404)

    def test_status_has_zero_counts_and_safe_settings(self):
        self.login()
        self.ingest()
        response = self.client.get('/api/status')
        self.assertEqual(response.json['status']['counts']['pending'], 1)
        self.assertEqual(response.json['status']['counts']['failed'], 0)
        self.assertEqual(response.json['status']['total_unique'], 1)
        self.assertNotIn('password_hash', response.get_data(as_text=True))
        self.assertNotIn('proxy', response.get_data(as_text=True))

    def test_search_pagination_and_receipt_filter(self):
        self.login()
        self.ingest(LINK.replace('test.mkv', 'a%_test.mkv'))
        self.store.db.execute('UPDATE items SET receipt=?', (json.dumps({'status': 'created', 'secret': 'not-for-web'}),))
        self.store.db.commit()
        response = self.client.get('/api/items?q=%25_&page_size=1')
        self.assertEqual(response.json['total'], 1)
        self.assertIsInstance(response.json['items'][0]['size'], str)
        self.assertNotIn('secret', response.json['items'][0]['receipt'])
        self.assertEqual(self.client.get('/api/items?page=2&page_size=1').json['items'], [])
        for query in ('page=0', 'page=x', 'page_size=101', 'state=bad'):
            self.assertEqual(self.client.get('/api/items?' + query).status_code, 400)

    def test_preview_repairs_original_error_and_does_not_write(self):
        self.login()
        response = self.post('/api/import/preview', {'text': '\ufeff' + BROKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json['new'], response.json['repaired'], response.json['invalid']), (1, 1, 0))
        self.assertEqual(self.store.status()['total_unique'], 0)

    def test_import_queue_runs_in_worker_and_deduplicates(self):
        self.login()
        response = self.post('/api/import', {'text': LINK})
        self.assertEqual(response.status_code, 202)
        ident = response.json['job']['id']
        self.assertEqual(self.store.status()['total_unique'], 0)
        self.service.commands()
        job = self.client.get('/api/jobs/' + ident).json['job']
        self.assertEqual((job['state'], job['result']['new']), ('succeeded', 1))
        self.assertNotIn('_values', job)
        again = self.post('/api/import', {'text': LINK})
        self.service.commands()
        self.assertEqual(self.client.get('/api/jobs/' + again.json['job']['id']).json['job']['result']['new'], 0)
        self.assertEqual(self.post('/api/import/preview', {'text': LINK + '\n' + LINK}).json['duplicates'], 2)
        with self.client.get('/api/export') as response:
            self.assertEqual(response.get_data(as_text=True), LINK + '\n')

    def test_bad_payload_is_rejected(self):
        self.login()
        for route, value in (('/api/actions', {'action': []}), ('/api/actions', {'action': 'import'}), ('/api/actions', {'action': 'retry_failed', 'item_id': "' OR 1=1"}), ('/api/import', {'text': 3}), ('/api/import', {'text': ''}), ('/api/import', {'text': LINK, 'extra': 1})):
            self.assertEqual(self.post(route, value).status_code, 400)
        self.assertEqual(self.client.post('/api/import', data='{bad', content_type='application/json', headers={'X-CSRF-Token': self.csrf}).status_code, 400)
        self.assertEqual(self.client.post('/api/import', data=LINK, headers={'X-CSRF-Token': self.csrf}).status_code, 415)
        self.assertEqual(self.post('/api/import', {'text': 'x' * (MAX_WEB_TEXT_BYTES + 1)}).status_code, 413)

    def test_manual_pause_survives_restart_and_resume_preserves_auth_pause(self):
        self.login()
        self.assertEqual(self.action('pause_reporting')['state'], 'succeeded')
        self.assertTrue(self.control.pause_requested.is_set())
        self.assertEqual(self.client.get('/api/status').json['status']['report_pause']['reason'], 'manual')
        self.assertFalse(self.client.get('/api/status').json['runtime']['pause_requested'])
        restarted = Control()
        Service(self.store, self.settings, {}, 'http://proxy.invalid:3128', control=restarted)
        self.assertTrue(restarted.pause_requested.is_set())
        self.store.set('report_pause', {'reason': 'authentication_failed', 'at': time.time()})
        self.action('resume_reporting')
        self.assertFalse(self.control.pause_requested.is_set())
        self.assertFalse(self.client.get('/api/status').json['runtime']['pause_requested'])
        self.assertIsNone(self.store.get('report_manual_pause'))
        self.assertEqual(self.store.get('report_pause')['reason'], 'authentication_failed')

    def test_retry_one_failed_item_preserves_completed_records(self):
        self.login()
        self.ingest(LINK + '\n' + LINK.replace('0123456789abcdef0123456789abcdef', 'a' * 32))
        self.store.db.execute("UPDATE items SET state='failed',attempts=6 WHERE md4=?", ('a' * 32,))
        self.store.db.execute("UPDATE items SET state='reported' WHERE md4=?", ('0123456789abcdef0123456789abcdef',))
        self.store.db.commit()
        job = self.action('retry_failed', item_id='a' * 32 + ':123')
        self.assertEqual(job['result']['requeued'], 1)
        self.assertEqual(self.store.status()['counts'], {'reported': 1, 'retry': 1})
        job = self.action('retry_failed', item_id='0123456789abcdef0123456789abcdef:123')
        self.assertEqual(job['error'], 'item_not_retryable')

    def test_collect_now_runs_existing_cycle_and_records_event(self):
        self.login()
        job = self.action('collect_now')
        self.assertEqual(job['state'], 'succeeded')
        self.service.collector.collect.assert_called_once_with('regeng115')
        self.assertEqual(self.client.get('/api/status').json['recent_events'][0]['summary']['action'], 'collect_now')

    def test_pause_request_stops_writes_before_worker_handles_command(self):
        self.ingest()
        self.service.reporter = Mock()
        self.control.submit('pause_reporting')
        with patch('app.safe_log'):
            self.service.cycle()
        self.service.reporter.process.assert_not_called()
        self.assertEqual(self.store.status()['counts'], {'pending': 1})
        self.assertFalse(self.service.stop.is_set())

    def test_unexpected_job_failure_is_safe_and_worker_can_continue(self):
        self.login()
        with patch.object(self.store, 'ingest', side_effect=OSError('sensitive-test-value')):
            response = self.post('/api/import', {'text': LINK})
            with patch('app.safe_log'):
                self.service.commands()
        job = self.client.get('/api/jobs/' + response.json['job']['id']).json['job']
        self.assertEqual(job['error'], 'management_error')
        self.assertNotIn('sensitive-test-value', json.dumps(job))
        self.assertEqual(self.action('pause_reporting')['state'], 'succeeded')

    def test_health_includes_real_http_server(self):
        stop = threading.Event()
        server = WebServer(self.application, '127.0.0.1', 0, stop, self.control)
        server.start()
        try:
            port = int(server.server.effective_port)
            with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/api/session', timeout=3) as response:
                self.assertFalse(json.load(response)['authenticated'])
            self.store.set('web_port', port)
            self.store.set('heartbeat', time.time())
            self.assertEqual(main(['--data', str(self.directory), 'health']), 0)
        finally:
            stop.set()
            server.close()
        self.assertFalse(server.thread.is_alive())
        self.assertEqual(main(['--data', str(self.directory), 'health']), 1)


class ControlTests(unittest.TestCase):
    def test_queue_is_bounded_and_stopping_finishes_pending_jobs(self):
        control = Control()
        jobs = [control.submit('retry_failed') for _ in range(16)]
        with self.assertRaisesRegex(WebError, 'management_queue_full'):
            control.submit('pause_reporting')
        self.assertFalse(control.pause_requested.is_set())
        control.close()
        self.assertEqual(control.job(jobs[0]['id'])['error'], 'service_stopped')
        with self.assertRaisesRegex(WebError, 'service_stopping'):
            control.submit('resume_reporting')

    def test_payload_weight_is_released_and_not_exposed(self):
        control = Control()
        submitted = control.submit('import', text=LINK)
        self.assertNotIn('_values', submitted)
        self.assertGreater(control.pending_bytes, 0)
        control.finish(control.pop(), result={'new': 1})
        self.assertEqual(control.pending_bytes, 0)
        self.assertNotIn(LINK, json.dumps(control.job(submitted['id'])))

    def test_input_limits_and_unicode(self):
        for value in (None, '', '\ud800', 'x' * (MAX_WEB_TEXT_BYTES + 1)):
            with self.assertRaises(WebError):
                input_text(value)
        self.assertEqual(input_text('\ufeff' + LINK), LINK)


if __name__ == '__main__':
    unittest.main()
