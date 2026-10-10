import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import app
from batch_notices import BatchNotices, HISTORY_LIMIT, MAX_CONTENT_BYTES, OUTBOX_KEY, STATUS_KEY, format_batch_notices, readable_size
from ms_plugin_client import MsPluginClient
from reporter import Decision, MsReporter, ReportError
from state import Store
from test_plugin_reporter import TestFixture
from test_reporter import SECRETS, SETTINGS

BATCH = {'id': 'a' * 32, 'submitted': True}
SUMMARY = {'updated_at': 1791618741.0}
ROWS = [{'name': 'Example.2026.2160p.mkv', 'size': 2 * 1024 ** 3, 'state': 'reported'}]


class FormattingTests(unittest.TestCase):
    def test_names_sizes_actual_states_time_and_safe_content(self):
        rows = ROWS + [dict(ROWS[0], name='Already.mkv', size=123, state='existing'),
                       dict(ROWS[0], name='Later.mkv', state='retry')]
        messages = format_batch_notices(BATCH, rows, SUMMARY)
        self.assertEqual(len(messages), 1)
        text = messages[0]['content']
        for value in ('Example.2026.2160p.mkv', '2.00 GiB', '2,147,483,648 字节', '123 B',
                      '[上报确认]', '[云端已有]', '[待重试]', 'UTC+8', '本批：3 条'):
            self.assertIn(value, text)
        self.assertNotIn('ed2k://', text)
        self.assertNotIn('a' * 32, text)

    def test_long_batches_split_with_all_files_in_order(self):
        rows = [dict(ROWS[0], name=f'file-{i:03d}-' + '中文标题' * 18 + '.mkv') for i in range(20)]
        messages = format_batch_notices(BATCH, rows, SUMMARY)
        self.assertGreater(len(messages), 1)
        combined = '\n'.join(message['content'] for message in messages)
        positions = [combined.index(row['name']) for row in rows]
        self.assertEqual(positions, sorted(positions))
        for i, message in enumerate(messages, 1):
            self.assertLessEqual(len(message['content'].encode()), MAX_CONTENT_BYTES)
            self.assertIn(f'({i}/{len(messages)})', message['title'])

    def test_one_oversized_name_is_shortened_without_invalid_unicode(self):
        messages = format_batch_notices(BATCH, [dict(ROWS[0], name='资源' * 2000)], SUMMARY)
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(len(messages[0]['content'].encode()), MAX_CONTENT_BYTES)
        self.assertIn('…', messages[0]['content'])
        self.assertIn('2,147,483,648 字节', messages[0]['content'])

    def test_names_cannot_inject_html_or_extra_control_lines(self):
        message = format_batch_notices(BATCH, [dict(ROWS[0], name='<b>A&B</b>\nInjected\x00.mkv')], SUMMARY)[0]
        self.assertNotIn('<b>', message['content'])
        self.assertNotIn('\x00', message['content'])
        self.assertIn('＜b＞A＆B＜/b＞ Injected .mkv', message['content'])

    def test_zero_rows_and_size_boundaries(self):
        self.assertEqual(format_batch_notices(BATCH, [], SUMMARY), [])
        self.assertEqual(readable_size(1023), '1023 B')
        self.assertEqual(readable_size(1024), '1.00 KiB')
        self.assertEqual(readable_size(1024 ** 4), '1.00 TiB')


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.calls = []
        self.now = 2000.0
        self.notices = BatchNotices(self.store, self.send, clock=lambda: self.now)

    def send(self, title, content):
        self.assertEqual(self.store.get(OUTBOX_KEY)[0]['state'], 'sending')
        self.calls.append((title, content))
        return True

    def enqueue(self, batch=BATCH, rows=ROWS):
        with self.store.db:
            self.notices.enqueue(batch, rows, SUMMARY)

    def test_enqueue_is_transactional_and_deduplicated(self):
        self.enqueue()
        self.enqueue()
        self.assertEqual(len(self.store.get(OUTBOX_KEY)), 1)
        self.assertEqual(self.store.get(STATUS_KEY)['pending'], 1)
        other = dict(BATCH, id='b' * 32)
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.notices.enqueue(other, ROWS, SUMMARY)
                raise RuntimeError('rollback')
        self.assertEqual(len(self.store.get(OUTBOX_KEY)), 1)

    def test_withdrawn_and_empty_work_do_not_notify(self):
        self.enqueue(dict(BATCH, submitted=False))
        self.enqueue(rows=[])
        self.assertEqual(self.store.get(OUTBOX_KEY), [])
        self.assertEqual(self.notices.tick()['sent'], 0)
        self.assertEqual(self.calls, [])

    def test_pending_is_restored_and_success_is_never_repeated(self):
        self.enqueue()
        restarted = BatchNotices(self.store, self.send)
        self.assertEqual(restarted.tick()['sent'], 1)
        self.assertEqual(restarted.tick()['sent'], 0)
        self.assertEqual(len(self.calls), 1)
        job = self.store.get(OUTBOX_KEY)[0]
        self.assertEqual(job['state'], 'sent')
        self.assertNotIn('title', job)
        self.assertNotIn('content', job)

    def test_process_death_around_send_does_not_resend(self):
        class ProcessDeath(BaseException):
            pass
        def dying_send(title, content):
            self.calls.append((title, content))
            raise ProcessDeath()
        self.enqueue()
        with self.assertRaises(ProcessDeath):
            BatchNotices(self.store, dying_send).tick()
        restarted = BatchNotices(self.store, self.send)
        self.assertEqual(restarted.tick()['uncertain'], 1)
        self.assertEqual(restarted.tick()['sent'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.get(OUTBOX_KEY)[0]['state'], 'uncertain')

    def test_notification_errors_are_terminal_and_do_not_pause_uploads(self):
        for exc, state in ((ReportError('ms_notice_auth_failed', auth=True), 'failed'),
                           (ReportError('ms_notice_network_error', uncertain=True), 'uncertain'),
                           (RuntimeError('private value must not appear'), 'uncertain')):
            with self.subTest(state=state, exception=type(exc).__name__):
                self.store.set(OUTBOX_KEY, [])
                self.enqueue()
                notices = BatchNotices(self.store, lambda *_: (_ for _ in ()).throw(exc))
                self.assertEqual(notices.tick()[state], 1)
                self.assertFalse(notices.tick()['changed'])
                self.assertIsNone(self.store.get('report_pause'))
                self.assertNotIn('private value', json.dumps(self.store.get(OUTBOX_KEY)))

    def test_shutdown_preserves_pending_for_next_start(self):
        self.enqueue()
        stop = threading.Event();stop.set()
        self.assertFalse(self.notices.tick(stop=stop)['changed'])
        self.assertEqual(self.store.get(OUTBOX_KEY)[0]['state'], 'pending')
        self.assertEqual(self.calls, [])

    def test_multiple_parts_send_one_per_tick_and_finish_in_order(self):
        self.enqueue(rows=[dict(ROWS[0], name=f'file-{i}-' + '中' * 200) for i in range(8)])
        jobs = self.store.get(OUTBOX_KEY)
        expected = len(jobs)
        def sender(title, content):
            self.calls.append((title, content));return True
        notices = BatchNotices(self.store, sender)
        for i in range(expected):
            self.assertEqual(notices.tick()['sent'], 1)
            self.assertEqual(len(self.calls), i + 1)
        self.assertFalse(notices.tick()['changed'])
        self.assertEqual(self.store.get(STATUS_KEY)['pending'], 0)
        self.assertEqual(self.store.get(STATUS_KEY)['sent'], expected)

    def test_invalid_outbox_is_isolated_without_a_network_write(self):
        self.store.set(OUTBOX_KEY, {'invalid': True})
        self.enqueue()
        self.assertEqual(self.notices.tick()['error'], 'ms_notice_state_invalid')
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.get('report_pause'))

    def test_terminal_history_is_bounded_but_pending_is_preserved(self):
        jobs = [{'id': f'{i:032x}:1', 'state': 'sent'} for i in range(HISTORY_LIMIT + 10)]
        self.store.set(OUTBOX_KEY, jobs)
        self.enqueue()
        self.notices.tick()
        self.assertEqual(len(self.store.get(OUTBOX_KEY)), HISTORY_LIMIT)


class NativeNoticeTests(unittest.TestCase):
    def client(self, raw=b'{"code":20000,"data":null}', status=200, error=None):
        class Response(io.BytesIO):
            code = status
        class Opener:
            def __init__(self):
                self.calls = []
            def open(self, request, timeout):
                self.calls.append(request)
                if error:
                    raise error
                return Response(raw)
        reporter = MsReporter(SECRETS)
        reporter.local = Opener()
        return MsPluginClient(reporter, 37, store=None)

    def test_json_success_api_key_header_and_direct_local_opener(self):
        client = self.client()
        self.assertTrue(client.send_notice('Title', 'Content'))
        request = client.reporter.local.calls[0]
        self.assertEqual(request.full_url, SECRETS['ms_url'] + '/api/v1/message/openSend')
        self.assertEqual(request.get_header('Apikey'), SECRETS['ms_api_key'])
        self.assertEqual(json.loads(request.data), {'title': 'Title', 'content': 'Content'})

    def test_documented_plain_success_is_supported(self):
        self.assertTrue(self.client(b'SUCCESS\n').send_notice('Title', 'Content'))

    def test_unknown_or_boolean_responses_are_uncertain(self):
        for raw in (b'false', b'{}', b'{"code":true}', b'{"code":"20000"}', b'SUCCESS extra', b'not-json'):
            with self.subTest(raw=raw), self.assertRaises(ReportError) as caught:
                self.client(raw).send_notice('Title', 'Content')
            self.assertTrue(caught.exception.uncertain)

    def test_auth_rejection_is_definite_but_timeout_may_have_delivered(self):
        with self.assertRaises(ReportError) as caught:
            self.client(status=401).send_notice('Title', 'Content')
        self.assertTrue(caught.exception.auth)
        self.assertFalse(caught.exception.uncertain)
        with self.assertRaises(ReportError) as caught:
            self.client(error=urllib.error.URLError('private address')).send_notice('Title', 'Content')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(str(caught.exception), 'ms_notice_network_error')

    def test_invalid_payload_never_sends(self):
        client = self.client()
        for title, content in (('', 'Content'), ('Title', ''), (True, 'Content')):
            with self.assertRaisesRegex(ReportError, 'ms_notice_payload_invalid'):
                client.send_notice(title, content)
        self.assertEqual(client.reporter.local.calls, [])


class BatchNoticeIntegrationTests(TestFixture):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.notices = BatchNotices(self.store, lambda title, content: self.sent.append((title, content)) is None, clock=lambda: self.now)
        self.batch = self.client(notices=self.notices)

    def complete(self):
        self.plugin.state = False
        self.cloud.default = Decision('exists')
        self.advance()
        return self.batch.tick()

    def test_notice_is_enqueued_only_after_final_confirmation_and_txt_retirement(self):
        self.start_batch(1, 2)
        self.assertIsNone(self.store.get(OUTBOX_KEY))
        self.complete()
        self.assertIsNone(self.store.get('plugin_batch'))
        self.assertEqual(self.text(), '')
        self.assertEqual(len(self.store.get(OUTBOX_KEY)), 1)
        self.assertEqual(self.sent, [])
        self.notices.tick()
        self.assertIn('1.mkv', self.sent[0][1])
        self.assertIn('2.mkv', self.sent[0][1])
        self.assertEqual(self.plugin.runs, 1)
        self.assertEqual(self.row(1)['state'], 'reported')

    def test_notice_failure_does_not_reupload_or_restore_txt(self):
        self.start_batch()
        self.complete()
        with patch.object(self.notices, 'send', side_effect=ReportError('ms_notice_network_error', uncertain=True)):
            self.assertEqual(self.notices.tick()['uncertain'], 1)
        self.advance();self.batch.tick()
        self.assertEqual(self.plugin.runs, 1)
        self.assertEqual(self.text(), '')
        self.assertEqual(self.row()['state'], 'reported')

    def test_all_existing_prequeries_create_no_notice_or_plugin_execution(self):
        self.add(1)
        self.cloud.default = Decision('exists')
        self.batch.tick()
        self.assertIsNone(self.store.get(OUTBOX_KEY))
        self.assertEqual(self.plugin.runs, 0)

    def test_service_connects_notices_to_the_sole_plugin_worker(self):
        settings = {**SETTINGS, 'report_backend': 'ms_plugin', 'ms_plugin': {
            'instance_id': 37, 'queue_file': str(self.queue), 'check_seconds': 15, 'timeout_seconds': 900}}
        service = app.Service(self.store, settings, SECRETS, 'http://127.0.0.1:9')
        self.assertIs(service.plugin.notices, service.notices)
        with patch.object(service.plugin, 'tick', return_value={'changed': False}), \
             patch.object(service.notices, 'tick', return_value={'changed': True, 'sent': 1}) as notice_tick:
            result = service.plugin_tick()
        self.assertTrue(result['changed'])
        self.assertEqual(result['notice']['sent'], 1)
        self.assertIs(notice_tick.call_args.kwargs['stop'], service.stop)


if __name__ == '__main__':
    unittest.main()
