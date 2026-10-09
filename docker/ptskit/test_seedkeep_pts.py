"""Offline acceptance for site counts, candidate metadata and PTS query failures."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch
import seedkeep_pts as pts


def sample_payload():
    return {'ret': 0, 'msg': 'success', 'data': {
        'uid': 123, 'task': '保种任务[1]', 'has_task': True, 'has_record': True,
        'target': 1000, 'current': 439, 'missing': 561, 'seeders_max': 10,
        'synced_at': '2026-10-03 06:00:57', 'candidates_total': 1000,
        'candidates_truncated': False, 'candidates': [
            {'id': '123', 'name': '候选 A', 'small_descr': '说明', 'category': '短剧',
             'size': 100, 'seeders': 2, 'leechers': 0, 'download_url': 'https://example.invalid/private-token'},
            {'id': 124, 'name': '候选 B', 'category': '动态漫', 'size': 200, 'seeders': 6, 'leechers': 3}
        ]}}


class PtsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.settings = {'min_seeders': 2, 'max_seeders': 6, 'max_bytes': 524288000}
        self.fetcher = Mock(return_value=sample_payload())
        self.client = pts.Client(lambda: {'token': 'private-token'}, lambda: self.settings,
                                 fetcher=self.fetcher, clock=lambda: self.now)

    def test_counts_are_site_values_and_not_derived_from_candidates(self):
        value = self.client.snapshot()
        self.assertEqual((value['current'], value['target'], value['missing']), (439, 1000, 561))
        self.assertEqual((value['returned_candidates'], value['filter_matches']), (2, 2))
        self.assertTrue(value['available'])
        self.assertFalse(value['stale'])
        self.assertEqual(value['synced_at'], '2026-10-03 06:00:57')
        payload = sample_payload()
        payload['data']['missing'] = 700
        self.assertEqual(pts.sanitize(payload)['missing'], 700)

    def test_candidate_whitelist_excludes_user_and_download_credentials(self):
        value = self.client.snapshot()
        serialized = json.dumps(value)
        for field in ('private-token', 'download_url', 'uid', 'Authorization', 'token'):
            self.assertNotIn(field, serialized)
        self.assertEqual(set(value['items'][0]), {'id', 'name', 'small_descr', 'category', 'size', 'seeders', 'leechers', 'matches_filter', 'source'})
        self.assertEqual(value['items'][0]['category'], '短剧')

    def test_cache_expiry_and_explicit_refresh(self):
        self.client.snapshot()
        self.now += 59
        self.client.snapshot()
        self.assertEqual(self.fetcher.call_count, 1)
        self.now += 1
        self.client.snapshot()
        self.assertEqual(self.fetcher.call_count, 2)
        self.client.snapshot(force=True)
        self.assertEqual(self.fetcher.call_count, 3)

    def test_failure_preserves_previous_values_and_success_recovers(self):
        initial = self.client.snapshot()
        self.now += 60
        self.fetcher.side_effect = RuntimeError('private-token')
        failed = self.client.snapshot()
        self.assertFalse(failed['available'])
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['current'], initial['current'])
        self.assertEqual(failed['fetched_at'], initial['fetched_at'])
        self.assertEqual(failed['attempted_at'], self.now)
        self.assertNotIn('private-token', json.dumps(failed))
        self.client.snapshot()
        self.assertEqual(self.fetcher.call_count, 2)
        self.fetcher.side_effect = None
        recovered = self.client.snapshot(force=True)
        self.assertTrue(recovered['available'])
        self.assertIsNone(recovered['error'])

    def test_first_failure_reports_unknown_instead_of_zero(self):
        self.fetcher.side_effect = OSError('private-token')
        value = self.client.snapshot()
        self.assertFalse(value['available'])
        self.assertFalse(value['stale'])
        self.assertIsNone(value['fetched_at'])
        self.assertNotIn('current', value)
        self.assertNotIn('private-token', json.dumps(value))

    def test_settings_recompute_match_without_another_site_query(self):
        self.assertEqual(self.client.snapshot()['filter_matches'], 2)
        self.settings['min_seeders'] = 3
        self.assertEqual(self.client.snapshot()['filter_matches'], 1)
        self.settings['max_bytes'] = 200
        self.assertEqual(self.client.snapshot()['filter_matches'], 0)
        self.assertEqual(self.fetcher.call_count, 1)

    def test_filter_has_inclusive_seeders_and_strict_size_boundary(self):
        payload = sample_payload()
        payload['data']['candidates'] = [
            {'id': n + 1, 'size': size, 'seeders': seeders}
            for n, (size, seeders) in enumerate([(499, 2), (499, 6), (500, 2), (100, 1), (100, 7)])]
        self.fetcher.return_value = payload
        self.settings['max_bytes'] = 500
        matched = {row['id'] for row in self.client.snapshot()['items'] if row['matches_filter']}
        self.assertEqual(matched, {'1', '2'})

    def test_business_and_malformed_responses_never_echo_message(self):
        for payload in (None, {}, {'ret': True}, {'ret': 1, 'msg': 'private-token'}, {'ret': 0, 'data': None}):
            with self.subTest(payload=payload), self.assertRaises(pts.PtsError) as error:
                pts.sanitize(payload)
            self.assertNotIn('private-token', str(error.exception))

    def test_missing_or_invalid_counts_task_and_list_are_rejected(self):
        for key, bad in [('current', None), ('target', -1), ('missing', True), ('has_task', 1),
                         ('has_record', None), ('candidates', {}), ('candidates', [None] * 10001)]:
            with self.subTest(key=key):
                payload = sample_payload()
                payload['data'][key] = bad
                with self.assertRaises(pts.PtsError):
                    pts.sanitize(payload)

    def test_invalid_candidates_are_skipped_and_ids_normalized(self):
        payload = sample_payload()
        valid = copy.deepcopy(payload['data']['candidates'][0])
        payload['data']['candidates'].extend([
            None, {**valid, 'id': '00123'}, {**valid, 'id': '０１'}, {**valid, 'id': '0'},
            {**valid, 'id': True}, {**valid, 'id': '-1'}, {**valid, 'id': '3' * 21},
            {**valid, 'id': 130, 'size': True}, {**valid, 'id': 131, 'seeders': True},
            {**valid, 'id': 132, 'size': 0}, {**valid, 'id': 133, 'seeders': -1}])
        value = pts.sanitize(payload)
        self.assertEqual([row['id'] for row in value['items']], ['123', '124'])
        self.assertEqual(value['valid_candidates'], 2)

    def test_unknown_optional_values_do_not_fabricate_site_metadata(self):
        payload = sample_payload()
        payload['data'].update(synced_at='wrong', candidates_total=True, candidates_truncated='false', seeders_max=True)
        value = pts.sanitize(payload)
        for key in ('synced_at', 'candidates_total', 'candidates_truncated', 'seeders_max'):
            self.assertIsNone(value[key])

    def test_transport_uses_existing_token_timeout_and_bounded_read(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(sample_payload()).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(pts.urllib.request, 'build_opener', return_value=opener):
            value = pts.fetch_payload({'api_base': 'https://example.invalid/', 'token': 'private-token'})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.invalid/api/v1/seedkeep/refill?limit=1000')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-token')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)
        response.read.assert_called_once_with(pts.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(value['ret'], 0)

    def test_transport_errors_are_safe(self):
        opener = Mock()
        for code in (401, 403, 500):
            opener.open.side_effect = urllib.error.HTTPError('https://example.invalid/private-token', code, 'private-token', {}, io.BytesIO(b'private-token'))
            with patch.object(pts.urllib.request, 'build_opener', return_value=opener), self.assertRaises(pts.PtsError) as error:
                pts.fetch_payload({'api_base': 'https://example.invalid', 'token': 'private-token'})
            self.assertNotIn('private-token', str(error.exception))
        opener.open.side_effect = TimeoutError('private-token')
        with patch.object(pts.urllib.request, 'build_opener', return_value=opener), self.assertRaises(pts.PtsError):
            pts.fetch_payload({'api_base': 'https://example.invalid', 'token': 'private-token'})

    def test_missing_credentials_invalid_json_and_response_limit(self):
        with self.assertRaises(pts.PtsError):
            pts.fetch_payload({})
        for raw in (b'not-json', b'\xff', b'x' * (pts.MAX_RESPONSE_BYTES + 1)):
            with patch.object(pts.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = raw
                with self.assertRaises(pts.PtsError):
                    pts.fetch_payload({'api_base': 'https://example.invalid', 'token': 'private-token'})


    def cached_client(self, path, *, token='private-token', fetcher=None):
        return pts.Client(lambda: {'api_base': 'https://example.invalid', 'token': token},
                          lambda: self.settings, fetcher=fetcher or self.fetcher,
                          clock=lambda: self.now, cache_path=path)

    def test_statistics_reads_only_cache_and_marks_expiry_without_network(self):
        self.settings.update(target=1200, pts_cache_seconds=1800)
        empty = self.client.statistics()
        self.assertFalse(empty['available'])
        self.assertIsNone(empty.get('current'))
        self.fetcher.assert_not_called()
        self.client.snapshot(include_rss=False)
        value = self.client.statistics()
        self.assertEqual((value['current'], value['local_target']), (439, 1200))
        self.assertTrue(value['available'])
        self.assertNotIn('items', value)
        self.now += 1800
        expired = self.client.statistics()
        self.assertFalse(expired['available'])
        self.assertTrue(expired['stale'])
        self.assertTrue(expired['cache_expired'])
        self.assertEqual(expired['current'], 439)
        self.assertEqual(self.fetcher.call_count, 1)

    def test_statistics_does_not_wait_for_an_inflight_site_request(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(_):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test request not released')
            return sample_payload()
        self.client.fetcher = fetch
        thread = threading.Thread(target=lambda: self.client.snapshot(include_rss=False), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            value = self.client.statistics()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(value['available'])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.statistics()['current'], 439)

    def test_persisted_statistics_are_private_and_bound_to_the_source(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR')) as directory:
            path = Path(directory) / 'statistics.json'
            self.cached_client(path).snapshot(include_rss=False)
            document = path.read_text(encoding='utf-8')
            for sensitive in ('private-token', 'example.invalid', 'uid', 'download_url', '候选 A', 'items'):
                self.assertNotIn(sensitive, document)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            fetcher = Mock(return_value=sample_payload())
            restarted = self.cached_client(path, fetcher=fetcher)
            restored = restarted.statistics()
            self.assertEqual(restored['current'], 439)
            self.assertTrue(restored['available'])
            self.assertTrue(restored['restored'])
            fetcher.assert_not_called()
            self.assertFalse(restarted.snapshot(refresh=False)['available'])
            self.assertEqual(restarted.snapshot(include_rss=False)['returned_candidates'], 2)
            fetcher.assert_called_once()
            other = self.cached_client(path, token='another-test-token')
            self.assertIsNone(other.statistics().get('current'))

    def test_last_query_failure_survives_restart_as_old_statistics(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR')) as directory:
            path = Path(directory) / 'statistics.json'
            client = self.cached_client(path)
            client.snapshot(include_rss=False)
            self.fetcher.side_effect = RuntimeError('private-token')
            client.snapshot(force=True, include_rss=False)
            self.assertNotIn('private-token', path.read_text(encoding='utf-8'))
            restarted = self.cached_client(path)
            value = restarted.statistics()
            self.assertEqual(value['current'], 439)
            self.assertFalse(value['available'])
            self.assertTrue(value['stale'])
            self.assertIsNotNone(value['error'])

    def test_corrupt_future_and_invalid_statistics_are_ignored(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR')) as directory:
            path = Path(directory) / 'statistics.json'
            self.cached_client(path).snapshot(include_rss=False)
            original = json.loads(path.read_text(encoding='utf-8'))
            future = copy.deepcopy(original)
            future['fetched_at'] = self.now + 1
            invalid = copy.deepcopy(original)
            invalid['statistics']['current'] = True
            for raw in ('[]', 'not json', json.dumps(future), json.dumps(invalid), 'x' * 65537):
                with self.subTest(raw_type=raw[:8]):
                    path.write_text(raw, encoding='utf-8')
                    self.assertIsNone(self.cached_client(path).statistics().get('current'))

    def test_invalidation_removes_persisted_statistics(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR')) as directory:
            path = Path(directory) / 'statistics.json'
            client = self.cached_client(path)
            client.snapshot(include_rss=False)
            self.assertTrue(path.exists())
            client.invalidate()
            self.assertFalse(path.exists())
            self.assertIsNone(client.statistics().get('current'))
            self.assertIsNone(self.cached_client(path).statistics().get('current'))

    def test_statistics_cache_write_failure_does_not_hide_successful_query(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR')) as directory:
            path = Path(directory) / 'missing' / 'statistics.json'
            client = self.cached_client(path)
            value = client.snapshot(include_rss=False)
            self.assertTrue(value['available'])
            self.assertEqual(client.statistics()['current'], 439)
            self.assertIsNotNone(client.statistics()['cache_error'])


if __name__ == '__main__':
    unittest.main()
