"""Offline contracts for independent category/data cleanup; all files are disposable."""
import copy
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import seedkeep_cleanup as cleanup
import seedkeep_downloaders as downloads
import seedkeep_pull as pull
from seedkeep_storage import Storage
from test_seedkeep_management import SOURCE, NOW, qb_task, tr_task
from test_seedkeep_pull import torrent
from torrent_meta import torrent_metadata


def rule(**changes):
    return {**copy.deepcopy(cleanup.DEFAULT_RULE), 'id': 'r1', 'name': '分类清理',
            'scopes': [{'instance_id': 'q1', 'values': ['PTS'], 'include_empty': False}],
            'target_mode': 'independent', 'target': 1200, **changes}


def record(number=1, **changes):
    value = format(number, '040x')
    raw = qb_task(value, category='PTS')
    return {'id': 'q1:' + value, 'instance_id': 'q1', 'type': 'qb', 'hash': value,
            'aliases': [value], 'name': '任务', 'size': 100, 'pts': True, 'raw': raw,
            'safe': True, 'signature': cleanup.digest(raw),
            'evidence': {'seeders': True, 'unregistered': False}, **changes}


class RuleTests(unittest.TestCase):
    def test_defaults_are_independent_off_and_observing(self):
        value = cleanup.validate({'rules': [rule()]})['rules'][0]
        self.assertFalse(value['enabled'])
        self.assertTrue(value['observe_only'])
        self.assertEqual((value['check_minutes'], value['max_per_run']), (5, 20))

    def test_empty_scope_never_means_everything(self):
        with self.assertRaises(downloads.ManagementError):
            cleanup.validate({'rules': [rule(scopes=[{'instance_id': 'q1', 'values': [], 'include_empty': False}])]})
        value = record()
        self.assertFalse(cleanup.scope_match(rule(scopes=[]), value))

    def test_strict_types_unknown_fields_duplicate_ids_and_thresholds(self):
        for change in ({'target': True}, {'target': 0}, {'start_margin': 0}, {'stop_margin': 200},
                       {'max_per_run': 51}, {'check_minutes': 0}, {'seeders_wait_hours': float('nan')},
                       {'unknown': 1}, {'seeders_enabled': False, 'unregistered_enabled': False}):
            with self.subTest(change=change), self.assertRaises(downloads.ManagementError):
                cleanup.validate({'rules': [rule(**change)]})
        with self.assertRaises(downloads.ManagementError):
            cleanup.validate({'rules': [rule(), rule()]})

    def test_categories_union_exact_and_empty_explicit(self):
        configured = rule(scopes=[{'instance_id': 'q1', 'values': ['PTS', '保种'], 'include_empty': False}])
        for category, expected in [('PTS', True), ('保种', True), ('pts', False), ('PTS-old', False), ('', False)]:
            self.assertEqual(cleanup.scope_match(configured, record(raw=qb_task('1' * 40, category=category))), expected)
        configured['scopes'][0]['include_empty'] = True
        self.assertTrue(cleanup.scope_match(configured, record(raw=qb_task('1' * 40, category=''))))

    def test_transmission_labels_are_exact_or_and_untagged(self):
        configured = rule(scopes=[{'instance_id': 't1', 'values': ['转移做种', 'PTS'], 'include_empty': False}])
        for labels, expected in [(['转移做种', '其它'], True), (['PTS-old'], False), ([], False)]:
            self.assertEqual(cleanup.scope_match(configured, record(instance_id='t1', type='tr', raw=tr_task('1' * 40, labels=labels))), expected)

    def test_aliases_union_is_transitive(self):
        a, b, c = 'a' * 40, 'b' * 40, 'c' * 64
        groups = cleanup.unique([record(1, aliases=[a]), record(2, aliases=[b, c]), record(3, aliases=[a, b])])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_1399_1400_1300_hysteresis_and_budget(self):
        values = [record(i) for i in range(1, 1401)]
        state = {}
        report, state, items = cleanup.evaluate(rule(), values[:1399], state, NOW, 1200)
        self.assertFalse(report['active'])
        self.assertEqual(report['budget'], 0)
        report, state, items = cleanup.evaluate(rule(), values, state, NOW + 300, 1200)
        self.assertTrue(report['active'])
        self.assertEqual(report['budget'], 20)
        report, state, items = cleanup.evaluate(rule(), values[:1301], state, NOW + 600, 1200)
        self.assertTrue(report['active'])
        self.assertEqual(report['budget'], 1)
        report, state, items = cleanup.evaluate(rule(), values[:1300], state, NOW + 900, 1200)
        self.assertFalse(report['active'])
        self.assertEqual(report['budget'], 0)

    def test_follow_actual_target_and_downloads_consume_inventory(self):
        values = [record(i, safe=False) for i in range(1, 1301)]
        report, state, items = cleanup.evaluate(rule(target_mode='follow'), values, {}, NOW, 1100)
        self.assertEqual((report['target'], report['start_line'], report['stop_line'], report['inventory']), (1100, 1300, 1200, 1300))
        self.assertEqual(report['mature'], 0)
        self.assertTrue(all(item['status'] == 'protected' for item in items))

    def test_24h_strict_evidence_and_candidate_shortage(self):
        configured = rule(target=1, start_margin=2, stop_margin=0, check_minutes=60)
        values = [record(1), record(2, evidence={'seeders': False, 'unregistered': False}), record(3, safe=False)]
        state = {}
        for hour in range(25):
            report, state, items = cleanup.evaluate(configured, values, state, NOW + hour * 3600, 1)
            self.assertEqual(report['mature'], int(hour >= 24))
        self.assertEqual(report['inventory'], 3)
        self.assertEqual(report['budget'], 2)
        self.assertEqual(sum(item['status'] == 'ready' for item in items), 1)

    def test_reason_timers_independent_unknown_recovery_and_gap_reset(self):
        configured = rule(target=1, start_margin=1, stop_margin=0)
        values = [record(), record(2)]
        _, state, _ = cleanup.evaluate(configured, values, {}, NOW, 1)
        values[0]['evidence'] = {'seeders': None, 'unregistered': True}
        _, state, _ = cleanup.evaluate(configured, values, state, NOW + 300, 1)
        observed = state['observations'][values[0]['id']]
        self.assertNotIn('seeders', observed['reasons'])
        self.assertEqual(observed['reasons']['unregistered']['first'], NOW + 300)
        values[0]['evidence'] = {'seeders': True, 'unregistered': False}
        _, state, _ = cleanup.evaluate(configured, values, state, NOW + 600, 1)
        self.assertNotIn('unregistered', state['observations'][values[0]['id']]['reasons'])
        _, state, _ = cleanup.evaluate(configured, values, state, NOW + 1601, 1)
        self.assertEqual(state['observations'][values[0]['id']]['reasons']['seeders']['first'], NOW + 1601)

    def test_scope_identity_and_threshold_changes_reset(self):
        configured = rule(target=1, start_margin=1, stop_margin=0)
        values = [record(), record(2)]
        _, state, _ = cleanup.evaluate(configured, values, {}, NOW, 1)
        values[0]['signature'] = 'f' * 64
        _, state, _ = cleanup.evaluate(configured, values, state, NOW + 300, 1)
        self.assertEqual(state['observations'][values[0]['id']]['reasons']['seeders']['first'], NOW + 300)
        _, state, _ = cleanup.evaluate(configured, values, state, NOW + 600, 1, threshold_version='changed')
        self.assertEqual(state['observations'][values[0]['id']]['reasons']['seeders']['first'], NOW + 600)

    def test_shared_copy_and_rule_conflict_are_protected(self):
        value = record()
        other = record(2, id='t1:' + value['hash'], instance_id='t1', aliases=value['aliases'])
        report, state, items = cleanup.evaluate(rule(), [value, other], {}, NOW, 1200)
        self.assertEqual(report['inventory'], 1)
        self.assertEqual(items[0]['status'], 'shared')
        report, state, items = cleanup.evaluate(rule(), [value], {}, NOW, 1200, conflicts={value['id']})
        self.assertEqual(items[0]['status'], 'conflict')
        self.assertFalse(state['observations'])

    def test_rename_preserves_wait_and_version(self):
        configured = rule()
        _, state, _ = cleanup.evaluate(configured, [record()], {}, NOW, 1200)
        renamed = {**configured, 'name': '新名字'}
        _, state, _ = cleanup.evaluate(renamed, [record()], state, NOW + 300, 1200)
        self.assertEqual(state['observations'][record()['id']]['reasons']['seeders']['first'], NOW)


class EvidenceTests(unittest.TestCase):
    def qtrack(self, **changes):
        return {'url': 'https://tracker.ptskit.org/announce?passkey=fake-private', 'status': 2,
                'num_seeds': 11, 'msg': '', '_verified_receipt_at': NOW, **changes}

    def test_ten_kept_eleven_invalid_and_tracker_conflict_unknown(self):
        self.assertFalse(cleanup.evidence([self.qtrack(num_seeds=10)], 'qb', SOURCE, NOW, 7200, 10)['seeders'])
        self.assertTrue(cleanup.evidence([self.qtrack()], 'qb', SOURCE, NOW, 7200, 10)['seeders'])
        self.assertIsNone(cleanup.evidence([self.qtrack(), self.qtrack(num_seeds=10)], 'qb', SOURCE, NOW, 7200, 10)['seeders'])

    def test_explicit_unregistered_only_trusted_non_generic_failures(self):
        self.assertTrue(cleanup.evidence([self.qtrack(status=4, msg='Unregistered torrent')], 'qb', SOURCE, NOW, 7200, 10)['unregistered'])
        for changes in ({'url': 'https://other.invalid/announce'}, {'msg': 'passkey invalid; unregistered torrent', 'status': 4},
                        {'msg': 'timeout', 'status': 4}, {'msg': 'Unregistered torrent', 'status': 2},
                        {'url': 'ftp://tracker.ptskit.org/announce', 'status': 4}):
            self.assertIsNone(cleanup.evidence([self.qtrack(**changes)], 'qb', SOURCE, NOW, 7200, 10)['unregistered'])

    def test_qb_receipt_missing_stale_future_or_nonfinite_resets_both_reasons(self):
        for changes in ({}, {'status': 4, 'msg': 'Unregistered torrent'}):
            for stamp in (None, NOW - 7201, NOW + 61, float('nan'), True):
                value = self.qtrack(**changes, _verified_receipt_at=stamp)
                self.assertEqual(cleanup.evidence([value], 'qb', SOURCE, NOW, 7200, 10),
                                 {'seeders': None, 'unregistered': None})

    def test_repeated_qb_snapshot_expires_instead_of_accumulating_24_hours(self):
        configured = rule(target=1, start_margin=1, stop_margin=0, check_minutes=5)
        values, state = [record(), record(2)], {}
        track = self.qtrack()
        for offset in range(0, 86401, 300):
            for item in values:
                item['evidence'] = cleanup.evidence([track], 'qb', SOURCE, NOW + offset, 7200, 10)
            report, state, _ = cleanup.evaluate(configured, values, state, NOW + offset, 1)
            self.assertEqual(report['mature'], 0)
        self.assertEqual(state['observations'], {})
        track['_verified_receipt_at'] = NOW + 86400
        values[0]['evidence'] = cleanup.evidence([track], 'qb', SOURCE, NOW + 86400, 7200, 10)
        _, state, _ = cleanup.evaluate(configured, values, state, NOW + 86400, 1)
        self.assertEqual(state['observations'][values[0]['id']]['reasons']['seeders']['first'], NOW + 86400)

    def test_transmission_stale_future_and_failed_reports(self):
        track = tr_task('a' * 40, seeders=11)['trackerStats'][0]
        self.assertTrue(cleanup.evidence([track], 'tr', SOURCE, NOW, 7200, 10)['seeders'])
        for stamp in (NOW - 7201, NOW + 61, 0):
            self.assertIsNone(cleanup.evidence([{**track, 'lastAnnounceTime': stamp}], 'tr', SOURCE, NOW, 7200, 10)['seeders'])
        unregistered = {**track, 'lastAnnounceSucceeded': False, 'lastAnnounceResult': 'torrent not registered'}
        self.assertTrue(cleanup.evidence([unregistered], 'tr', SOURCE, NOW, 7200, 10)['unregistered'])


class LatestTransmissionEvidenceTests(unittest.TestCase):
    def test_new_failed_announce_does_not_reuse_old_successful_scrape(self):
        track = tr_task('a' * 40, seeders=11)['trackerStats'][0]
        track.update(lastScrapeTime=NOW - 600, lastScrapeSucceeded=True,
                     lastAnnounceSucceeded=False, lastAnnounceResult='HTTP 500')
        evidence = cleanup.evidence([track], 'tr', SOURCE, NOW, 7200, 10)
        self.assertIsNone(evidence['seeders'])
        self.assertIsNone(evidence['unregistered'])

    def test_fresh_scrape_recovery_beats_older_unregistered_announce(self):
        track = tr_task('a' * 40, seeders=10)['trackerStats'][0]
        track.update(lastAnnounceTime=NOW - 600, lastAnnounceSucceeded=False,
                     lastAnnounceResult='torrent not registered', lastScrapeTime=NOW,
                     lastScrapeSucceeded=True, lastScrapeResult='success')
        self.assertEqual(cleanup.evidence([track], 'tr', SOURCE, NOW, 7200, 10),
                         {'seeders': False, 'unregistered': False})

    def test_timeout_flag_and_simultaneous_conflicting_receipts_are_unknown(self):
        track = tr_task('a' * 40, seeders=11)['trackerStats'][0]
        timed = {**track, 'lastAnnounceTimedOut': True}
        conflict = {**track, 'lastScrapeTime': NOW, 'lastScrapeSucceeded': False}
        for value in (timed, conflict):
            self.assertEqual(cleanup.evidence([value], 'tr', SOURCE, NOW, 7200, 10),
                             {'seeders': None, 'unregistered': None})


if __name__ == '__main__':
    unittest.main()
