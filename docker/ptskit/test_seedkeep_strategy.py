"""Isolated offline acceptance tests for refill budgets and durable observations."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import seedkeep_strategy as strategy

NOW = 1800000000
SOURCE = {'api_base': 'https://www.pts.example'}
SETTINGS = {'refill_count_basis': 'site_effective', 'target': 1200,
            'refill_trigger': 1100, 'refill_floor': 1000, 'max_per_run': 50,
            'refill_max_inflight': 500, 'refill_site_max_age_minutes': 120,
            'refill_reservation_hours': 72, 'pending_grace_seconds': 300}


def h(number, length=40):
    return format(number, '0' + str(length) + 'x')


def synced(stamp):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp))


def site(current=742, stamp=NOW - 60, **fields):
    return {'current': current, 'synced_at': synced(stamp), 'available': True,
            'stale': False, **fields}


def qb(number=1, **fields):
    return {'hash': h(number), 'progress': .5, 'state': 'downloading',
            'name': 'private-name', 'save_path': '/private/path',
            'tracker': 'https://tracker.pts.example/announce?secret=private-token', 'tags': 'pts保种组', **fields}


def tr(number=1, **fields):
    return {'hashString': h(number), 'percentDone': .5, 'status': 4, 'error': 0,
            'trackerStats': [{'announce': 'https://tracker.pts.example/announce?secret=private-token'}], 'labels': ['pts保种组'], **fields}


def record(number=1, **fields):
    return {'hash': h(number), 'hashes': [h(number)], 'added_at': NOW - 10, **fields}


class StrategyTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('PI_SCRATCH_DIR'):
            raise RuntimeError('PI_SCRATCH_DIR is required for isolated test data')
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ['PI_SCRATCH_DIR'])
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = NOW
        self.settings = dict(SETTINGS)
        self.subject = strategy.Strategy(self.directory, clock=lambda: self.now)
        self.batch = {'accepted': {}, 'pending': {}, 'unconfirmed': {}}

    def evaluate(self, current=742, tasks=None, transmissions=None, snapshot=None, **keywords):
        return self.subject.evaluate(self.settings, snapshot if snapshot is not None else site(current, self.now - 60),
                                     tasks or [], transmissions or {}, self.batch, source=SOURCE, **keywords)

    def restart(self):
        self.subject = strategy.Strategy(self.directory, clock=lambda: self.now)

    def state(self):
        return json.loads(self.subject.path.read_text(encoding='utf-8'))

    def test_default_and_legacy_public_are_read_only(self):
        with patch.object(strategy, 'save_json', side_effect=AssertionError('unexpected write')):
            answer = self.subject.public(self.settings, True, self.now + 300)
            self.assertEqual(answer['basis'], 'site_effective')
            self.assertEqual(answer['status'], 'idle')
            self.assertFalse(answer['active'])
            self.assertIsNone(answer['last_checked_at'])
            legacy = self.subject.public({}, True, None)
            self.assertEqual(legacy['basis'], 'managed_tasks')
            self.assertIsNone(legacy['site_current'])
            self.assertEqual(self.subject.public(self.settings, False, None)['status'], 'disabled')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_hysteresis_target_1200_survives_restart(self):
        self.assertFalse(self.evaluate(1100)['active'])
        below = self.evaluate(1099)
        self.assertTrue(below['active'])
        self.assertEqual(below['allowance'], 50)
        self.restart()
        recovered = self.evaluate(1150)
        self.assertTrue(recovered['active'])
        self.assertEqual(recovered['allowance'], 50)
        self.assertFalse(self.evaluate(1200)['active'])
        self.assertEqual(self.evaluate(1200)['status'], 'at_target')
        self.assertEqual(self.evaluate(1199)['allowance'], 0)

    def test_742_plus_405_reserves_yields_53_then_per_run_50(self):
        tasks = [qb(index) for index in range(1, 406)]
        answer = self.evaluate(tasks=tasks)
        self.assertEqual((answer['site_current'], answer['inflight'], answer['reserved']), (742, 405, 405))
        self.assertEqual(answer['allowance'], 50)
        self.settings['max_per_run'] = 100
        self.assertEqual(self.evaluate(tasks=tasks)['allowance'], 53)

    def test_local_completed_and_managed_counts_do_not_replace_site(self):
        tasks = [qb(index, progress=1, state='uploading') for index in range(1, 700)]
        self.batch['accepted'] = {str(index): record(index) for index in range(1, 700)}
        answer = self.evaluate(tasks=tasks)
        self.assertEqual(answer['site_current'], 742)
        self.assertEqual(answer['reserved'], 0)
        self.assertEqual(answer['allowance'], 50)
        self.assertTrue(answer['warning'])

    def test_warning_uses_configured_floor_and_only_fresh_site(self):
        self.assertTrue(self.evaluate(999)['warning'])
        self.assertFalse(self.evaluate(1000)['warning'])
        self.settings['refill_floor'] = 800
        self.assertFalse(self.evaluate(900)['warning'])
        self.assertFalse(self.evaluate(snapshot=site(700, stale=True))['warning'])

    def test_all_pts_including_unmanaged_and_other_clients(self):
        tasks = [qb(1), qb(2, tracker='https://other.example/announce')]
        transmissions = {h(3): tr(3), h(4): tr(4, trackerStats=[])}
        answer = self.evaluate(tasks=tasks, transmissions=transmissions)
        self.assertEqual(answer['inflight'], 2)
        self.assertEqual(answer['reserved'], 2)

    def test_same_hash_cross_client_deduplicates(self):
        answer = self.evaluate(tasks=[qb(1)], transmissions={h(1): tr(1)})
        self.assertEqual((answer['reserved'], answer['inflight']), (1, 1))

    def test_v1_v2_aliases_dedupe_and_survive_migration_restart(self):
        tasks = [qb(1, infohash_v1=h(1), infohash_v2=h(2, 64))]
        transmissions = {h(2, 64): tr(2, hashString=h(2, 64))}
        self.assertEqual(self.evaluate(tasks=tasks, transmissions=transmissions)['reserved'], 1)
        original = self.state()['observations'][0]
        self.now += 20
        self.restart()
        self.assertEqual(self.evaluate(transmissions=transmissions)['reserved'], 1)
        recovered = self.state()['observations'][0]
        self.assertEqual(recovered['first_seen'], original['first_seen'])
        self.assertEqual(set(recovered['aliases']), {h(1), h(2, 64)})

    def test_multiple_instances_and_transitive_batch_aliases(self):
        self.batch['accepted'] = {'one': record(1, hashes=[h(1), h(2, 64)]),
                                  'two': record(3, hashes=[h(3), h(2, 64)])}
        tasks = [qb(1), qb(1, state='stalledDL'), qb(3)]
        transmissions = [tr(2, hashString=h(2, 64)), tr(2, hashString=h(2, 64))]
        answer = self.evaluate(tasks=tasks, transmissions=transmissions)
        self.assertEqual((answer['reserved'], answer['inflight']), (1, 1))

    def test_cross_instance_states_keep_healthy_reservation_and_completion(self):
        tasks = [qb(1, added_on=NOW - 120), qb(1, state='pausedDL', tracker='')]
        answer = self.evaluate(tasks=tasks)
        self.assertEqual((answer['reserved'], answer['inflight']), (1, 1))
        self.assertEqual(self.state()['observations'][0]['first_seen'], NOW - 120)
        self.now += 10
        tasks = [qb(1, state='uploading', progress=1), qb(1, state='error')]
        answer = self.evaluate(tasks=tasks)
        self.assertEqual((answer['reserved'], answer['inflight'], answer['receipt_reserved']), (1, 0, 1))

    def test_batch_scope_works_without_source_and_with_uppercase_aliases(self):
        self.batch['accepted'] = {'one': record(10, hash=h(10).upper(), hashes=[h(10).upper(), h(11, 64).upper()])}
        answer = self.subject.evaluate(self.settings, site(), [qb(10, tracker='')], {}, self.batch)
        self.assertEqual(answer['reserved'], 1)

    def test_repeated_poll_and_restart_never_extend_first_observation(self):
        self.evaluate(tasks=[qb()])
        original = self.state()['observations'][0]['first_seen']
        self.now += 1800
        self.evaluate(tasks=[qb(added_on=self.now)])
        self.restart()
        self.evaluate(tasks=[qb(added_on=self.now)])
        self.assertEqual(self.state()['observations'][0]['first_seen'], original)
        self.assertEqual(len(self.state()['observations']), 1)

    def test_completion_observation_waits_for_strictly_newer_site_sync(self):
        self.settings.update(target=744, refill_trigger=743, refill_floor=700)
        self.evaluate(tasks=[qb(1), qb(2)])
        self.now += 20
        self.restart()
        tasks = [qb(1, progress=1, state='uploading'), qb(2, progress=1, state='uploading')]
        answer = self.evaluate(tasks=tasks, snapshot=site(742, self.now - 1))
        self.assertEqual((answer['reserved'], answer['receipt_reserved'], answer['inflight']), (2, 2, 0))
        self.assertEqual(answer['status'], 'waiting_sync')
        self.restart()
        self.assertEqual(self.evaluate(tasks=tasks, snapshot=site(742, self.now))['reserved'], 2)
        self.now += 2
        answer = self.evaluate(tasks=tasks, snapshot=site(742, self.now))
        self.assertEqual(answer['reserved'], 0)
        self.assertEqual(answer['site_current'], 742)  # Sync advancement is not a per-task receipt.
        self.assertEqual(answer['allowance'], 2)

    def test_first_completed_tasks_need_legal_recent_completion_metadata(self):
        tasks = [qb(1, progress=1, state='uploading', completion_on=NOW - 20),
                 qb(2, progress=1, state='uploading', completion_on=NOW - 100),
                 qb(3, progress=1, state='uploading'),
                 qb(4, progress=1, state='uploading', completion_on=NOW + 1000),
                 qb(5, progress=1, state='uploading', completion_on=True)]
        transmissions = {h(6): tr(6, percentDone=1, status=6, doneDate=NOW - 30)}
        answer = self.evaluate(tasks=tasks, transmissions=transmissions)
        self.assertEqual((answer['reserved'], answer['receipt_reserved']), (2, 2))
        self.now += 10
        self.restart()
        self.assertEqual(self.evaluate(tasks=tasks, transmissions=transmissions)['reserved'], 2)

    def test_bad_or_unavailable_site_is_unknown_and_preserves_state(self):
        self.evaluate(tasks=[qb()])
        saved = self.state()['observations']
        snapshots = [None, {}, site(available=False), site(available=1), site(stale=True),
                     site(stale=0), site(current=False), site(current='742'), site(current=-1),
                     site(current=742.0), site(synced_at=None), site(synced_at='no_sync'),
                     site(synced_at='2026-02-30 12:00:00'), site(stamp=NOW - 7201),
                     site(stamp=NOW + 61), site(synced_at=float('inf'))]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                answer = self.subject.evaluate(self.settings, snapshot, [], {}, self.batch, force=True, source=SOURCE)
                self.assertEqual((answer['allowance'], answer['status']), (0, 'unknown'))
                self.assertTrue(answer['active'])
                self.assertIsNone(answer['site_current'])
                self.assertEqual(self.state()['observations'], saved)
        self.restart()
        self.assertTrue(self.subject.public(self.settings, True, None)['active'])

    def test_unknown_initial_site_never_activates_latch(self):
        answer = self.evaluate(snapshot=site(available=False))
        self.assertFalse(answer['active'])
        self.assertEqual(answer['allowance'], 0)

    def test_freshness_boundary_local_timezone(self):
        self.assertEqual(strategy._sync_time(synced(NOW)), NOW)
        self.assertNotEqual(self.evaluate(snapshot=site(stamp=NOW - 7200))['status'], 'unknown')
        self.assertNotEqual(self.evaluate(snapshot=site(stamp=NOW + 60))['status'], 'unknown')
        self.settings['refill_site_max_age_minutes'] = 1
        self.assertEqual(self.evaluate(snapshot=site(stamp=NOW - 61))['status'], 'unknown')

    def test_paused_error_release_reservations_but_keep_capacity(self):
        self.settings['refill_max_inflight'] = 3
        self.evaluate(tasks=[qb(1), qb(2), qb(3)])
        tasks = [qb(1, state='pausedDL'), qb(2, state='error')]
        answer = self.evaluate(tasks=tasks, transmissions={h(3): tr(3, status=0)})
        self.assertEqual((answer['reserved'], answer['inflight'], answer['allowance']), (0, 3, 0))
        self.assertEqual(answer['status'], 'waiting_downloads')
        answer = self.evaluate(tasks=[qb(1, state='pausedDL'), qb(2, state='missingFiles')])
        self.assertEqual((answer['inflight'], answer['allowance']), (2, 1))
        self.assertEqual(len(self.state()['observations']), 2)
        self.assertEqual(self.evaluate()['reserved'], 0)
        self.assertEqual(self.state()['observations'], [])

    def test_completed_paused_and_error_tasks_do_not_hold_receipt_reservations(self):
        for state in ('pausedUP', 'stoppedUP', 'error', 'missingFiles', 'futureState'):
            with self.subTest(state=state):
                answer = self.evaluate(1199, tasks=[qb(progress=1, state=state, completion_on=NOW - 20)], force=True)
                self.assertEqual((answer['reserved'], answer['allowance']), (0, 1))
        for status, error in ((0, 0), (6, 2)):
            with self.subTest(status=status, error=error):
                answer = self.evaluate(1199, transmissions={h(2): tr(2, percentDone=1, status=status, error=error, doneDate=NOW - 20)}, force=True)
                self.assertEqual((answer['reserved'], answer['allowance']), (0, 1))

    def test_recent_tr_completion_holds_receipt_until_site_sync(self):
        answer = self.evaluate(1199, transmissions={h(2): tr(2, percentDone=1, status=6, doneDate=NOW - 20)}, force=True)
        self.assertEqual((answer['receipt_reserved'], answer['allowance']), (1, 0))
        self.now += 30
        answer = self.evaluate(1199, transmissions={h(2): tr(2, percentDone=1, status=6, doneDate=NOW - 20)},
                               snapshot=site(1199, stamp=self.now), force=True)
        self.assertEqual((answer['reserved'], answer['allowance']), (0, 1))


    def test_transmission_errors_occupy_capacity(self):
        self.settings['refill_max_inflight'] = 1
        answer = self.evaluate(transmissions={h(1): tr(error=2)})
        self.assertEqual((answer['reserved'], answer['inflight'], answer['allowance']), (0, 1, 0))

    def test_checking_and_moving_keep_reservation_identity(self):
        self.evaluate(tasks=[qb()])
        first = self.state()['observations'][0]['first_seen']
        for state_name in ('checkingDL', 'checkingResumeData', 'moving'):
            self.now += 10
            answer = self.evaluate(tasks=[qb(state=state_name)])
            self.assertEqual(answer['reserved'], 1)
            self.assertEqual(self.state()['observations'][0]['first_seen'], first)
        self.assertEqual(self.evaluate(transmissions={h(1): tr(status=2)})['reserved'], 1)

    def test_completed_receipt_phase_survives_checking_and_moving(self):
        fixed_site = site()
        self.evaluate(tasks=[qb(progress=1, state='uploading', completion_on=NOW - 20)], snapshot=fixed_site)
        for state_name in ('moving', 'checkingResumeData', 'checkingDL'):
            self.now += 1
            self.restart()
            answer = self.evaluate(tasks=[qb(state=state_name, progress=0)], snapshot=fixed_site)
            self.assertEqual((answer['reserved'], answer['receipt_reserved'], answer['inflight']), (1, 1, 0))
        answer = self.evaluate(tasks=[qb(state='moving', progress=0)], snapshot=site(stamp=self.now))
        self.assertEqual(answer['reserved'], 0)
        answer = self.evaluate(tasks=[qb(state='moving', progress=0)], snapshot=site(stamp=self.now))
        self.assertEqual((answer['reserved'], answer['inflight']), (0, 0))

    def test_reservation_timeout_never_frees_inflight_or_resets_after_restart(self):
        self.settings.update(refill_reservation_hours=1, refill_max_inflight=1)
        self.evaluate(tasks=[qb()])
        self.now += 3600
        self.restart()
        answer = self.evaluate(tasks=[qb(added_on=self.now)])
        self.assertEqual((answer['reserved'], answer['inflight'], answer['allowance']), (0, 1, 0))
        self.assertEqual(self.state()['observations'][0]['first_seen'], NOW)
        answer = self.evaluate(tasks=[qb(progress=1, state='uploading')])
        self.assertEqual(answer['reserved'], 0)
        self.assertEqual(answer['allowance'], 1)

    def test_valid_added_dates_bound_first_observation(self):
        self.settings['refill_reservation_hours'] = 1
        answer = self.evaluate(tasks=[qb(1, added_on=NOW - 3601), qb(2, added_on=NOW + 500),
                                     qb(3, added_on=float('nan')), qb(4, added_on=True)],
                               transmissions={h(5): tr(5, addedDate=NOW - 3601)})
        self.assertEqual((answer['reserved'], answer['inflight']), (3, 5))
        self.assertEqual(self.state()['observations'][0]['first_seen'], NOW - 3601)

    def test_recent_pending_dedupes_aliases_clients_and_ignores_unconfirmed(self):
        self.batch['pending'] = {'seen': record(1, hashes=[h(1), h(2, 64)]),
                                 'recent': record(3, hashes=[h(3), h(4, 64)]),
                                 'duplicate': record(4, hash=h(4, 64), hashes=[h(4, 64)]),
                                 'expired': record(5, added_at=NOW - 300),
                                 'future': record(6, added_at=NOW + 100),
                                 'badtime': record(7, added_at=True)}
        self.batch['unconfirmed'] = {'missing': record(8), 'seen': record(9)}
        answer = self.evaluate(tasks=[qb(2, hash=h(2, 64)), qb(9)])
        self.assertEqual((answer['reserved'], answer['pending_reserved'], answer['inflight']), (3, 1, 2))
        self.now += 301
        answer = self.evaluate(tasks=[qb(2, hash=h(2, 64)), qb(9)])
        self.assertEqual(answer['pending_reserved'], 1)  # The formerly future record is now legitimately recent.
        self.now += 300
        self.assertEqual(self.evaluate(tasks=[qb(2, hash=h(2, 64)), qb(9)])['pending_reserved'], 0)

    def test_pending_capacity_is_subtracted_once(self):
        self.settings['refill_max_inflight'] = 3
        self.batch['pending'] = {'new': record(3)}
        answer = self.evaluate(tasks=[qb(1), qb(2)])
        self.assertEqual((answer['reserved'], answer['inflight'], answer['pending_reserved'], answer['allowance']), (3, 2, 1, 0))
        answer = self.evaluate(tasks=[qb(1), qb(2), qb(3)])
        self.assertEqual((answer['reserved'], answer['inflight'], answer['pending_reserved'], answer['allowance']), (3, 3, 0, 0))

    def test_force_one_round_does_not_latch_or_exceed_target(self):
        self.assertEqual(self.evaluate(1150)['allowance'], 0)
        answer = self.evaluate(1150, force=True)
        self.assertEqual(answer['allowance'], 50)
        self.assertFalse(answer['active'])
        self.assertEqual(self.evaluate(1150)['allowance'], 0)
        for current in (1200, 1300):
            self.assertEqual(self.evaluate(current, force=True)['allowance'], 0)
        self.settings['refill_max_inflight'] = 1
        self.assertEqual(self.evaluate(1199, tasks=[qb()], force=True)['allowance'], 0)

    def test_mutations_keep_observations_but_reset_results_as_requested(self):
        self.evaluate(tasks=[qb()])
        saved = self.state()['observations']
        self.subject.defer()
        answer = self.subject.public(self.settings, True, None)
        self.assertEqual((answer['status'], answer['reserved'], answer['allowance']), ('busy', 1, 50))
        self.assertTrue(answer['active'])
        self.subject.defer('running')
        self.assertEqual(self.subject.public(self.settings, True, None)['status'], 'running')
        self.subject.pause()
        answer = self.subject.public(self.settings, True, None)
        self.assertEqual((answer['active'], answer['status'], answer['reserved']), (False, 'disabled', 1))
        self.subject.reset()
        answer = self.subject.public(self.settings, True, None)
        self.assertEqual((answer['active'], answer['reserved'], answer['status']), (False, 0, 'idle'))
        self.assertEqual(self.state()['observations'], saved)
        self.restart()
        self.assertEqual(self.evaluate(tasks=[qb()])['reserved'], 1)

    def test_public_basis_change_and_disabled_never_mutate_or_write(self):
        self.evaluate(tasks=[qb()])
        before = self.subject.path.read_bytes()
        with patch.object(strategy, 'save_json', side_effect=AssertionError('unexpected write')):
            answer = self.subject.public({}, True, None)
            self.assertIsNone(answer['site_current'])
            self.assertFalse(answer['active'])
            disabled = self.subject.public(self.settings, False, NOW + 300)
            self.assertEqual(disabled['status'], 'disabled')
            self.assertFalse(disabled['active'])
            self.assertEqual(disabled['allowance'], 0)
            self.assertEqual(disabled['reserved'], 1)
            disabled['reserved'] = 999
        self.assertEqual(self.subject.path.read_bytes(), before)
        self.assertEqual(self.subject.public(self.settings, True, None)['reserved'], 1)

    def test_public_expired_sample_drops_budget_and_warning_without_write(self):
        self.evaluate(tasks=[qb()])
        before = self.subject.path.read_bytes()
        self.now += 7201
        with patch.object(strategy, 'save_json', side_effect=AssertionError('unexpected write')):
            answer = self.subject.public(self.settings, True, None)
            self.assertEqual((answer['status'], answer['allowance'], answer['warning']), ('unknown', 0, False))
            self.assertIsNone(answer['site_current'])
            self.assertTrue(answer['active'])
            self.assertEqual(answer['reserved'], 1)
        self.assertEqual(self.subject.path.read_bytes(), before)

    def test_local_basis_counts_exact_tag_tasks_without_site_sample(self):
        self.settings.pop('refill_count_basis')
        self.settings.update(target=5, max_per_run=10)
        self.batch['accepted'] = {'one': record(1)}
        self.batch['unconfirmed'] = {'seen': record(2)}
        self.batch['pending'] = {'new': record(3)}
        answer = self.evaluate(tasks=[qb(1, progress=1, state='pausedUP'), qb(2), qb(4, tags='')])
        self.assertEqual((answer['basis'], answer['allowance']), ('managed_tasks', 2))
        self.assertIsNone(answer['site_current'])
        self.assertIsNone(answer['site_synced_at'])
        self.assertFalse(answer['warning'])

    def test_safe_public_and_private_state_exclude_task_metadata(self):
        answer = self.evaluate(tasks=[qb()])
        expected = {'basis', 'active', 'status', 'site_current', 'site_synced_at', 'reserved', 'inflight',
                    'allowance', 'last_checked_at', 'warning', 'next_check_at', 'receipt_reserved', 'pending_reserved'}
        self.assertEqual(set(answer), expected)
        public_text = json.dumps(answer)
        saved = self.subject.path.read_text(encoding='utf-8')
        for value in (h(1), 'private-name', '/private/path', 'private-token', 'pts.example'):
            self.assertNotIn(value, public_text)
        for value in ('private-name', '/private/path', 'private-token', 'pts.example'):
            self.assertNotIn(value, saved)
        self.assertFalse(self.subject.path.with_suffix('.json.tmp').exists())
        if os.name != 'nt':
            self.assertEqual(self.subject.path.stat().st_mode & 0o777, 0o600)

    def test_broken_json_never_overwritten_and_error_does_not_echo_content(self):
        original = b'{"secret":"private-token",broken'
        self.subject.path.write_bytes(original)
        with self.assertRaises(strategy.StrategyError) as raised:
            self.restart()
        self.assertNotIn('private-token', str(raised.exception))
        self.assertEqual(self.subject.path.read_bytes(), original)

    def test_restore_strictly_rejects_illegal_flags_counts_times_aliases_and_structure(self):
        self.evaluate(tasks=[qb()])
        good = self.state()
        mutations = [lambda state: state.update(active=1),
                     lambda state: state.update(version=True),
                     lambda state: state.update(observations={}),
                     lambda state: state.update(secret='private-token'),
                     lambda state: state['summary'].update(status='private-token'),
                     lambda state: state['summary'].update(warning=1),
                     lambda state: state['summary'].update(reserved=-1),
                     lambda state: state['summary'].update(inflight=True),
                     lambda state: state['summary'].update(allowance=2.0),
                     lambda state: state['summary'].update(last_checked_at=float('nan')),
                     lambda state: state['summary'].update(next_check_at=float('inf')),
                     lambda state: state['summary'].update(site_current=True),
                     lambda state: state['observations'][0].update(aliases=['not-a-hash']),
                     lambda state: state['observations'][0].update(aliases=[]),
                     lambda state: state['observations'][0].update(first_seen=True),
                     lambda state: state['observations'][0].update(first_seen=float('inf')),
                     lambda state: state['observations'][0].update(completed_at=NOW - 1),
                     lambda state: state['observations'].append(copy.deepcopy(state['observations'][0]))]
        for mutation in mutations:
            state = copy.deepcopy(good)
            mutation(state)
            raw = json.dumps(state).encode('utf-8')
            self.subject.path.write_bytes(raw)
            with self.subTest(mutation=mutation), self.assertRaises(strategy.StrategyError):
                self.restart()
            self.assertEqual(self.subject.path.read_bytes(), raw)

    def test_state_size_and_task_count_are_bounded(self):
        self.evaluate(tasks=[qb()])
        original = self.subject.path.read_bytes()
        with patch.object(strategy, 'MAX_STATE_BYTES', 1), self.assertRaises(strategy.StrategyError):
            self.restart()
        self.assertEqual(self.subject.path.read_bytes(), original)
        with patch.object(strategy, 'MAX_TASKS', 1), self.assertRaises(strategy.StrategyError):
            self.evaluate(tasks=[qb(1), qb(2)])
        self.assertEqual(self.subject.path.read_bytes(), original)

    def test_invalid_batch_hashes_and_snapshot_fail_without_mutation(self):
        self.evaluate(tasks=[qb()])
        original = self.subject.path.read_bytes()
        self.batch['accepted'] = {'bad': record(hash='private-token')}
        with self.assertRaises(strategy.StrategyError) as raised:
            self.evaluate()
        self.assertNotIn('private-token', str(raised.exception))
        self.assertEqual(self.subject.path.read_bytes(), original)

    def test_storage_failure_keeps_memory_and_generic_error(self):
        self.evaluate(tasks=[qb()])
        before = self.subject.public(self.settings, True, None)
        with patch.object(strategy, 'save_json', side_effect=OSError('private-token')):
            with self.assertRaises(strategy.StrategyError) as raised:
                self.evaluate(1200)
        self.assertNotIn('private-token', str(raised.exception))
        self.assertEqual(self.subject.public(self.settings, True, None), before)


if __name__ == '__main__':
    unittest.main()
