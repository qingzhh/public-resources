"""Offline RSS transport, bounded parsing and independent candidate-cache acceptance."""
import io
import json
import threading
import tracemalloc
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
import seedkeep_pts as pts
import seedkeep_rss as rss

SOURCE = {'api_base': 'https://site.invalid', 'token': 'private-token'}
FEED = 'https://site.invalid/seedkeeprss.php?passkey=private-passkey'


def api_payload():
    return {'ret': 0, 'data': {'has_task': True, 'has_record': True, 'task': 'test', 'target': 1000,
        'current': 388, 'missing': 612, 'seeders_max': 10, 'synced_at': '2026-10-03 13:00:55',
        'candidates_total': 1, 'candidates_truncated': False,
        'candidates': [{'id': 1, 'name': 'API 一人做种', 'size': 1024, 'seeders': 1}]}}


def row(tid='2', seeders=2, size=10 * 1024 * 1024):
    return {'id': tid, 'name': 'RSS 候选', 'small_descr': '', 'category': 'test',
            'size': size, 'seeders': seeders, 'leechers': 0}


def xml_item(tid='2', seeders=2, size=1024, host='site.invalid', padding=''):
    return (f'<item><title>完整候选名称 {tid}</title><category>短剧</category>'
            f'<description><![CDATA[<b>做种：</b>{seeders} 下载：0 {padding}]]></description>'
            f'<enclosure url="https://{host}/download.php?id={tid}&amp;passkey=private-passkey" '
            f'length="{size}"/></item>').encode()


def feed(*items):
    return b'<rss><channel>' + b''.join(items) + b'</channel></rss>'


class RssCandidateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.settings = {'min_seeders': 2, 'max_seeders': 6, 'max_bytes': 500 * 1024 * 1024, 'pts_cache_seconds': 60}
        self.api = Mock(return_value=api_payload())
        self.rss = Mock(return_value={'items': [row()], 'total': 1, 'truncated': False})
        self.client = pts.Client(lambda: SOURCE, lambda: self.settings, fetcher=self.api,
                                 rss_fetcher=self.rss, clock=lambda: self.now)

    def test_rss_matching_rows_survive_api_only_zero_match_filter(self):
        value = self.client.snapshot(force=True)
        self.assertEqual(value['filter_matches'], 1)
        self.assertEqual([item['id'] for item in value['items'] if item['matches_filter']], ['2'])
        self.assertEqual((value['returned_candidates'], value['rss_returned_candidates'], value['merged_candidates']), (1, 1, 2))
        self.assertTrue(value['rss_available'])
        self.assertEqual((value['current'], value['target'], value['seeders_max']), (388, 1000, 10))
        self.assertEqual({item['source'] for item in value['items']}, {'api', 'rss'})

    def test_same_id_is_normalized_and_merged_once_with_both_sources(self):
        self.rss.return_value['items'] += [row('0001', 3), row('002', 6)]
        value = self.client.snapshot()
        self.assertEqual(len(value['items']), 2)
        both = next(item for item in value['items'] if item['id'] == '1')
        self.assertEqual((both['source'], both['seeders']), ('both', 1))
        self.assertEqual(value['rss_returned_candidates'], 2)

    def test_rss_failure_keeps_old_metadata_without_affecting_site_counts(self):
        initial = self.client.snapshot()
        self.rss.side_effect = OSError('private-passkey')
        failed = self.client.snapshot(force=True)
        self.assertTrue(failed['available'])
        self.assertFalse(failed['rss_available'])
        self.assertTrue(failed['rss_stale'])
        self.assertEqual(failed['rss_fetched_at'], initial['rss_fetched_at'])
        self.assertEqual(failed['filter_matches'], 1)
        self.assertNotIn('private-passkey', json.dumps(failed))
        self.rss.side_effect = None
        self.assertTrue(self.client.snapshot(force=True)['rss_available'])

    def test_api_failure_allows_rss_and_preserves_separate_site_old_values(self):
        self.client.snapshot()
        self.api.side_effect = OSError('private-token')
        self.rss.return_value['items'] += [row('1', 3)]
        value = self.client.snapshot(force=True)
        self.assertFalse(value['available'])
        self.assertTrue(value['stale'])
        self.assertTrue(value['rss_available'])
        self.assertEqual(value['current'], 388)
        self.assertEqual(next(item for item in value['items'] if item['id'] == '1')['seeders'], 3)
        self.assertEqual(value['filter_matches'], 2)
        self.client.invalidate()
        first_failure = self.client.snapshot()
        self.assertNotIn('current', first_failure)
        self.assertFalse(first_failure['stale'])
        self.assertEqual(first_failure['filter_matches'], 2)

    def test_rss_uses_longer_cache_and_filter_changes_do_not_fetch(self):
        self.client.snapshot()
        self.now += 60
        self.client.snapshot()
        self.assertEqual((self.api.call_count, self.rss.call_count), (2, 1))
        self.settings['min_seeders'] = 3
        self.assertEqual(self.client.snapshot()['filter_matches'], 0)
        self.now += 540
        self.client.snapshot()
        self.assertEqual(self.rss.call_count, 2)
        self.client.snapshot(force=True)
        self.assertEqual(self.rss.call_count, 3)

    def test_filter_boundaries_and_invalid_or_secret_metadata(self):
        self.rss.return_value['items'] = [row(str(index + 10), seeders, size) for index, (seeders, size) in enumerate(
            [(2, 499), (6, 499), (1, 100), (7, 100), (2, 500)])]
        self.rss.return_value['items'] += [row('０２'), row('0'), row('8', True), row('9', 2, True), None]
        self.rss.return_value['items'][0].update(download_url=FEED, token='private-token', password='secret')
        self.settings['max_bytes'] = 500
        value = self.client.snapshot()
        self.assertEqual({item['id'] for item in value['items'] if item['matches_filter']}, {'10', '11'})
        serialized = json.dumps(value)
        for private in ('private-token', 'private-passkey', 'download_url', 'password', 'secret'):
            self.assertNotIn(private, serialized)

    def test_threshold_read_never_fetches_rss_even_during_slow_candidate_refresh(self):
        started, release = threading.Event(), threading.Event()
        def slow(source, settings):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return {'items': [row()], 'total': 1, 'truncated': False}
        self.client.rss_fetcher = slow
        worker = threading.Thread(target=self.client.snapshot)
        worker.start()
        self.assertTrue(started.wait(2))
        try:
            value = self.client.snapshot(include_rss=False)
            self.assertEqual(value['seeders_max'], 10)
            self.assertEqual(value['rss_returned_candidates'], 0)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_invalidation_drops_old_rss_response_and_cache(self):
        started, release = threading.Event(), threading.Event()
        def slow(source, settings):
            started.set()
            release.wait(3)
            return {'items': [row()], 'total': 1, 'truncated': False}
        self.client.rss_fetcher = slow
        worker = threading.Thread(target=self.client.snapshot)
        worker.start()
        self.assertTrue(started.wait(2))
        self.client.invalidate()
        release.set()
        worker.join(3)
        self.assertIsNone(self.client.rss_cached)
        self.assertIsNone(self.client.cached)
        self.assertIsNone(self.client.rss_fetched_at)

    def test_parallel_polling_fetches_each_source_once(self):
        threads = [threading.Thread(target=self.client.snapshot) for _ in range(8)]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual((self.api.call_count, self.rss.call_count), (1, 1))


class RssTransportTests(unittest.TestCase):
    def registry(self, data):
        registry = Mock()
        registry.items.return_value = [{'id': 'q1', 'type': 'qb', 'enabled': True, 'default': True}]
        registry.api.return_value.qget.return_value = data
        return registry

    def test_discovers_nested_string_and_object_feeds_with_no_article_data(self):
        registry = self.registry({'folder': {'feed': FEED, 'same': {'url': FEED}}})
        self.assertEqual(rss.discover(SOURCE, registry), FEED)
        registry.api.return_value.qget.assert_called_once_with('rss/items?withData=false')
        registry.items.return_value[0]['default'] = False
        self.assertEqual(rss.discover(SOURCE, registry), FEED)

    def test_unknown_ambiguous_disabled_and_other_host_feeds_are_rejected(self):
        for data in ({}, {'other': 'https://other.invalid/seedkeeprss.php'},
                     {'cleartext': 'http://site.invalid/seedkeeprss.php'}, {'other_port': 'https://site.invalid:1234/seedkeeprss.php'},
                     {'one': FEED, 'two': FEED + '&extra=1'}, {'login': 'https://u:p@site.invalid/seedkeeprss.php'}):
            with self.subTest(data=data), self.assertRaises(rss.RssError):
                rss.discover(SOURCE, self.registry(data))
        registry = self.registry({'feed': FEED})
        registry.items.return_value[0]['enabled'] = False
        with self.assertRaises(rss.RssError):
            rss.discover(SOURCE, registry)
        registry = self.registry({'feed': FEED})
        registry.api.return_value.qget.side_effect = OSError('private-passkey')
        with self.assertRaises(rss.RssError) as error:
            rss.discover(SOURCE, registry)
        self.assertNotIn('private-passkey', str(error.exception))

    def test_metadata_from_namespaces_and_invalid_items_are_safe(self):
        value = rss.parse(io.BytesIO(feed(xml_item(), xml_item('0002', 6), xml_item('3', host='other.invalid'),
                                           xml_item('4', size=0), xml_item('0'))), SOURCE)
        self.assertEqual(value['total'], 1)
        self.assertEqual(value['items'][0]['seeders'], 6)
        self.assertEqual(value['items'][0]['category'], '短剧')
        self.assertNotIn('private-passkey', json.dumps(value))
        namespaced = feed(xml_item()).replace(b'<rss>', b'<rss xmlns="urn:rss">')
        self.assertEqual(rss.parse(io.BytesIO(namespaced), SOURCE)['total'], 1)

    def test_malformed_dtd_entities_utf16_depth_and_size_are_rejected(self):
        invalid = [b'<rss><channel>', b'<!DOCTYPE rss [<!ENTITY x "secret">]><rss>&x;</rss>',
                   b'<html><body>authentication failed</body></html>',
                   '<!DOCTYPE rss><rss/>'.encode('utf-16'), b'<a>' * 33 + b'</a>' * 33,
                   b'<rss><channel><description>' + b'x' * (rss.MAX_NODE_BYTES + 20000) + b'</description></channel></rss>']
        for raw in invalid:
            with self.subTest(length=len(raw)), self.assertRaises(rss.RssError):
                rss.parse(io.BytesIO(raw), SOURCE)
        with self.assertRaises(rss.RssError):
            rss.parse(io.BytesIO(feed(xml_item())), SOURCE, max_bytes=100)

    def test_item_limit_is_explicitly_truncated_and_timeout_is_bounded(self):
        value = rss.parse(io.BytesIO(feed(xml_item('1'), xml_item('2'), xml_item('3'))), SOURCE, max_items=2)
        self.assertTrue(value['truncated'])
        self.assertEqual(value['total'], 2)
        ticks = iter([0, 1, 5])
        with self.assertRaises(rss.RssError):
            rss.parse(io.BytesIO(feed(xml_item())), SOURCE, budget=3, clock=lambda: next(ticks))

    def test_large_response_is_read_in_chunks_without_retaining_xml_tree(self):
        class Generated:
            def __init__(self):
                self.index, self.pending, self.maximum_read = 0, b'<rss><channel>', 0
            def read(self, size):
                self.maximum_read = max(self.maximum_read, size)
                if not self.pending and self.index < 2500:
                    self.pending = xml_item(str(self.index + 1), padding='x' * 20000)
                    self.index += 1
                elif not self.pending and self.index == 2500:
                    self.pending, self.index = b'</channel></rss>', 2501
                result, self.pending = self.pending[:size], self.pending[size:]
                return result
        stream = Generated()
        tracemalloc.start()
        try:
            value = rss.parse(stream, SOURCE, budget=30)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(value['total'], 2500)
        self.assertLessEqual(stream.maximum_read, 16384)
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertFalse(value['truncated'])

    def test_transport_redirects_and_failures_never_expose_private_urls(self):
        response = io.BytesIO(feed(xml_item()))
        response.geturl = lambda: FEED
        opener = Mock()
        opener.open.return_value = response
        with patch.object(rss.configuration, 'opener', return_value=opener):
            value = rss.fetch(SOURCE, {'site_timeout_seconds': 15}, self.registry({'feed': FEED}))
        self.assertEqual(value['total'], 1)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)
        redirect = rss.SameSiteRedirect(SOURCE)
        for target in ('https://other.invalid/seedkeeprss.php', 'http://site.invalid/seedkeeprss.php'):
            with self.assertRaises(rss.RssError):
                redirect.redirect_request(urllib.request.Request(FEED), None, 302, '', {}, target)
        for error in (TimeoutError('private-passkey'), urllib.error.HTTPError(FEED, 403, 'private-passkey', {}, io.BytesIO())):
            opener.open.side_effect = error
            with patch.object(rss.configuration, 'opener', return_value=opener), self.assertRaises(rss.RssError) as caught:
                rss.fetch(SOURCE, {}, self.registry({'feed': FEED}))
            self.assertNotIn('private-passkey', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
