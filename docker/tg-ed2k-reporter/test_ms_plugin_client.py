import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from ms_plugin_client import COMPLETION_FIELDS, MsPluginClient, OBSERVER_KEY, parse_completion
from reporter import MsReporter, ReportError
from state import Store
from test_reporter import SECRETS, SETTINGS

INSTANCE = 37
QUEUE = '/downloads/.tg-ed2k-queue/normalized.txt'


def completion(ident=11, **changes):
    values = {'总行数': 2, '有效条目': 2, '已上报': 1, '云端已存在': 1, '未识别': 0, '解析失败': 0, '失败': 0}
    return {'id': ident, 'code': f'plugin_instance_{INSTANCE}', 'caller': 'plugin/ed2k_hash_reporter.go:154',
            'msg': 'ED2K HASH 上报完成\n' + '\n'.join(f'{key}：{value}' for key, value in values.items()), **changes}


class CompletionTests(unittest.TestCase):
    def test_exact_terminal_template(self):
        self.assertEqual(set(parse_completion(completion(), INSTANCE)), COMPLETION_FIELDS)
        row = completion()
        row['msg'] = row['msg'].replace('\n', '\r\n')
        self.assertIsNotNone(parse_completion(row, INSTANCE))

    def test_generic_success_other_instances_and_invalid_fields_are_rejected(self):
        for changes in ({'msg': 'SUCCESS'}, {'code': 'plugin_instance_14'}, {'id': True}, {'caller': 'other.go:154'},
                        {'msg': completion()['msg'] + '\nextra'},
                        {'msg': completion()['msg'].replace('有效条目：2', '有效条目：3')},
                        {'msg': completion()['msg'].replace('失败：0', '失败：-1')},
                        {'msg': completion()['msg'].replace('已上报：1', '有效条目：1')}):
            with self.subTest(changes=changes):
                self.assertIsNone(parse_completion(completion(**changes), INSTANCE))


class NativeClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.now = 2000.0
        self.detail = {'id': INSTANCE, 'code': 'ed2k_hash_reporter', 'enabled': True,
                       'config': {'driverName': '115 Open', 'listFilePath': QUEUE, 'cron': '', 'ed2kText': '', 'notice': True}}
        self.logs = [completion(10)]
        self.calls = []
        self.error = None
        self.client = MsPluginClient(MsReporter(SECRETS), INSTANCE, store=self.store, api=self.api, clock=lambda: self.now)

    def api(self, path, payload):
        self.calls.append((path, payload))
        if 'detail' in path:
            return copy.deepcopy(self.detail)
        if '/logs/' in path:
            return {'list': copy.deepcopy(self.logs)}
        if '/call/' in path:
            observer = self.store.get(OBSERVER_KEY)
            self.assertEqual(observer['checkpoint'], 10)
            self.assertEqual(observer['batch_id'], 'b' * 32)
            if self.error:
                raise self.error
            return {'message': 'accepted'}
        raise AssertionError('unexpected route')

    def start(self):
        self.store.set('plugin_batch', {'id': 'b' * 32})
        return self.client.run()

    def test_acceptance_is_not_terminal_and_old_log_does_not_finish_batch(self):
        self.assertEqual(self.start(), {'completed': False})
        self.assertTrue(self.client.running())
        self.logs.append(completion(11, code='plugin_instance_14'))
        self.logs.append(completion(12, msg='SUCCESS'))
        self.assertTrue(self.client.running())
        self.assertFalse(self.store.get(OBSERVER_KEY)['finished'])

    def test_exact_new_completion_persists_across_client_restart(self):
        self.start()
        self.logs.append(completion(13))
        self.assertFalse(self.client.running())
        witness = self.store.get(OBSERVER_KEY)
        self.assertEqual(witness['terminal_log_id'], 13)
        self.assertEqual(witness['summary']['已上报'], 1)
        self.logs = []
        restarted = MsPluginClient(self.client.reporter, INSTANCE, store=self.store, api=self.api)
        self.assertFalse(restarted.running())
        self.assertEqual(sum('/call/' in path for path, _ in self.calls), 1)

    def test_dispatch_timeout_preserves_boundary_and_does_not_resend(self):
        self.error = ReportError('ms_network_error', transient=True, uncertain=True)
        with self.assertRaises(ReportError):
            self.start()
        restarted = MsPluginClient(self.client.reporter, INSTANCE, store=self.store, api=self.api)
        self.assertTrue(restarted.running())
        self.logs.append(completion(20))
        self.assertFalse(restarted.running())
        self.assertEqual(sum('/call/' in path for path, _ in self.calls), 1)

    def test_initial_probe_requires_terminal_or_no_logs(self):
        self.assertFalse(self.client.running())
        self.logs.append(completion(11, msg='other message'))
        self.assertIsNone(self.client.running())
        self.logs = []
        self.assertFalse(self.client.running())

    def test_changed_configuration_blocks_trigger(self):
        for field, value in (('cron', '* * * * *'), ('ed2kText', 'unexpected'), ('notice', False),
                             ('listFilePath', '/unexpected'), ('driverName', 'other')):
            original = copy.deepcopy(self.detail)
            self.detail['config'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ReportError, 'ms_plugin_config_changed'):
                self.start()
            self.detail = original
        self.assertFalse(any('/call/' in path for path, _ in self.calls))

    def test_string_configuration_is_supported_and_disabled_instance_rejected(self):
        self.detail['config'] = json.dumps(self.detail['config'])
        self.start()
        self.detail['enabled'] = False
        self.assertIsNone(self.client.running())
        self.assertEqual(self.client.last_error, 'ms_plugin_instance_changed')

    def test_invalid_observer_does_not_authorize_file_changes(self):
        self.store.set(OBSERVER_KEY, {'instance_id': 14, 'checkpoint': 10})
        self.assertIsNone(self.client.running())
        self.assertEqual(self.client.last_error, 'ms_plugin_observer_invalid')

    def test_authentication_failure_pauses_and_error_is_sanitized(self):
        with patch.object(self.client, '_validate', side_effect=ReportError('ms_auth_failed', auth=True)):
            self.assertIsNone(self.client.running())
        self.assertEqual(self.store.get('report_pause')['reason'], 'auth_failed')
        self.assertNotIn(SECRETS['ms_api_key'], json.dumps(self.store.status()))

    def test_http_business_code_must_be_integer_and_auth_uses_direct_get(self):
        class Response(io.BytesIO):
            code = 200
        class Opener:
            def __init__(self, body):
                self.body, self.calls = body, []
            def open(self, request, timeout):
                self.calls.append(request)
                return Response(json.dumps(self.body).encode())
        for code in (True, '20000', 20000):
            opener = Opener({'code': code, 'data': self.detail})
            self.client.reporter.local = opener
            if code == 20000 and type(code) is int:
                self.assertEqual(self.client._get('/test'), self.detail)
            else:
                with self.assertRaisesRegex(ReportError, 'ms_plugin_response_invalid'):
                    self.client._get('/test')
            self.assertEqual(opener.calls[0].get_header('Authorization'), 'Bearer ' + SECRETS['ms_api_key'])


class ServicePluginTests(unittest.TestCase):
    def test_plugin_service_owns_store_and_keeps_history_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                settings = {**SETTINGS, 'report_backend': 'ms_plugin', 'ms_plugin': {
                    'instance_id': INSTANCE, 'queue_file': str(Path(directory) / 'normalized.txt'), 'check_seconds': 15, 'timeout_seconds': 900}}
                service = app.Service(store, settings, SECRETS, 'http://127.0.0.1:9')
                self.assertIs(service.plugin.plugin.store, store)
                self.assertEqual(store.status()['report_backend'], 'ms_plugin')
                store.set('plugin_status', {'phase': 'idle', 'error': None})
                store.set('plugin_last_batch', {'id': 'b' * 32, 'phase': 'completed', 'total': 2, 'confirmed': 2, 'keys': ['private']})
                status = store.status()['plugin']
                self.assertEqual(status['phase'], 'completed')
                self.assertEqual(status['confirmed'], 2)
                self.assertNotIn('keys', status)
            finally:
                store.close()

    def test_active_plugin_batch_cannot_switch_to_direct_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.set('plugin_batch', {'id': 'b' * 32})
                with self.assertRaisesRegex(app.ConfigError, 'plugin_batch_requires_reconciliation'):
                    app.Service(store, SETTINGS, SECRETS, 'http://127.0.0.1:9')
            finally:
                store.close()

    def test_settings_validate_plugin_identity_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            base = {**SETTINGS, 'report_backend': 'ms_plugin', 'ms_plugin': {'instance_id': INSTANCE}}
            path.write_text(json.dumps(base), encoding='utf-8')
            self.assertEqual(app.load_settings(path)['ms_plugin']['queue_file'], '/queue/normalized.txt')
            for changes in ({'instance_id': True}, {'instance_id': 0}, {'queue_file': 'relative.txt'}, {'check_seconds': 1}, {'timeout_seconds': 10}):
                path.write_text(json.dumps({**base, 'ms_plugin': {**base['ms_plugin'], **changes}}), encoding='utf-8')
                with self.subTest(changes=changes), self.assertRaises(app.ConfigError):
                    app.load_settings(path)


if __name__ == '__main__':
    unittest.main()
