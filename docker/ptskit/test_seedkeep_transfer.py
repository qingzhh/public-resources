"""Offline MP rule acceptance: fresh planning, exact selection, persistence and verified handover."""
import contextlib
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch
import seedkeep_pull as pull
import seedkeep_transfer as transfer
import seedkeep_downloaders as downloads
import test_seedkeep_fleet as fixtures
from test_seedkeep_management import qb_task, tr_task


class CronTests(unittest.TestCase):
    def stamp(self, text):
        return dt.datetime.fromisoformat(text).timestamp()

    def test_default_and_named_weekday_use_mp_monday_zero(self):
        now = self.stamp('2026-10-03T19:01:30')
        self.assertEqual(transfer.next_schedule('0 */6 * * *', now), self.stamp('2026-10-04T00:00:00'))
        for cron in ('15 8 * * 0', '15 8 * * mon'):
            self.assertEqual(transfer.next_schedule(cron, now), self.stamp('2026-10-05T08:15:00'))
        self.assertEqual(transfer.next_schedule('0 8 * * sun', now), self.stamp('2026-10-04T08:00:00'))
        self.assertEqual(transfer.next_schedule('0 8 * oct mon-fri', now), self.stamp('2026-10-05T08:00:00'))

    def test_calendar_constraints_and_rare_leap_weekday(self):
        now = self.stamp('2026-10-03T00:00:00')
        next_at = transfer.next_schedule('0 0 29 feb mon', now)
        moment = dt.datetime.fromtimestamp(next_at)
        self.assertEqual((moment.month, moment.day, moment.weekday()), (2, 29, 0))
        self.assertGreater(next_at, now)
        with self.assertRaises(downloads.ManagementError):
            transfer.next_schedule('0 0 31 feb *', now)

    def test_ranges_steps_lists_and_strict_invalid_values(self):
        self.assertEqual(transfer.cron_fields('5,15-25/5 8-12/2 * jan,mar 0-6/2'),
                         [[5, 15, 20, 25], [8, 10, 12], list(range(1, 32)), [1, 3], [0, 2, 4, 6]])
        for cron in ('* * * *', '* * * * * *', '60 * * * *', '* 24 * * *', '* * 0 * *',
                     '* * * 13 *', '* * * * 7', '*/0 * * * *', '* * * * fri-mon',
                     '* * * * bad', '* * * * 0,,1', '1/mon * * * *', None, True):
            with self.subTest(cron=cron), self.assertRaises(downloads.ManagementError):
                transfer.cron_fields(cron)


class RuleTests(unittest.TestCase):
    make_fleet = fixtures.FleetTests.make_fleet
    task = fixtures.FleetTests.task
    advance = fixtures.FleetTests.advance

    def setUp(self):
        fixtures.FleetTests.setUp(self)
        self.rules = getattr(self.fleet, 'transfer_rules', None) or transfer.Rules(self.fleet)
        self.values = copy.deepcopy(self.rules.values)
        self.values.update(source_instance_id='q2', target_instance_id='t2')

    def save(self, **changes):
        self.values.update(changes)
        body = {'revision': self.rules.revision(), 'values': copy.deepcopy(self.values)}
        if self.values['enabled']:
            body['confirm'] = 'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA'
        return self.rules.update(body)

    def run_rule(self, tasks=None):
        body = {'revision': self.rules.revision(), 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'}
        if tasks is not None:
            body['tasks'] = tasks
        return self.rules.run(body)

    def preview(self, **changes):
        return self.rules.preview({'values': {**self.values, **changes}})

    def manager(self):
        return self.fleet.managers[('q2', 't2')]

    def complete(self):
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['items'][0]['phase'], 'verifying')
        self.t2.tr[self.hash].update(status=0, percentDone=1, leftUntilDone=0, haveValid=100, haveUnchecked=0)
        self.advance(11)
        self.fleet._tick()
        self.rules.observe()
        return self.manager().state['job']

    def mutating_calls(self):
        writes = {'qstop', 'qstart', 'qremove', 'qexport', 'torrent-add', 'torrent-set',
                  'torrent-stop', 'torrent-start', 'torrent-remove', 'torrent-verify'}
        return [call for api in (self.q1, self.q2, self.t1, self.t2) for call in api.calls if call[0] in writes]

    def test_default_get_is_disabled_safe_and_does_not_create_files(self):
        before = sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob('*'))
        public = self.rules.public()
        self.assertFalse(public['values']['enabled'])
        self.assertFalse(public['saved'])
        self.assertEqual(public['values']['source_instance_id'], 'q1')
        self.assertEqual(public['values']['target_instance_id'], 't1')
        self.assertEqual(public['runtime']['completed_count'], 0)
        self.assertEqual(before, sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob('*')))
        self.assertNotIn('site-secret', json.dumps(public))
        self.assertEqual(self.mutating_calls(), [])

    def test_preview_is_fresh_and_creates_no_configuration_pairs_or_history(self):
        before = {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        report = self.preview()
        self.assertEqual((report['counts']['eligible'], report['counts']['selected']), (1, 1))
        self.q2.qb[self.hash]['tags'] = 'hr'
        report = self.preview()
        self.assertEqual(report['counts']['eligible'], 0)
        self.assertEqual(report['items'][0]['reason_code'], 'excluded_tag')
        self.assertEqual(self.fleet.managers, {})
        self.assertEqual(before, {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()})
        self.assertEqual(self.mutating_calls(), [])
        text = json.dumps(report)
        for secret in ('site-secret', 'login-secret', 'fake-private', 'q2.invalid', '/downloads/PTS'):
            self.assertNotIn(secret, text)

    def test_tag_and_category_semantics_match_mp(self):
        task = self.q2.qb[self.hash]
        task.update(category='Movies', tags='one, two')
        self.assertEqual(self.preview(include_categories=['Movies'], include_tags=['one', 'two'])['counts']['eligible'], 1)
        self.assertEqual(self.preview(include_categories=['movies'])['items'][0]['reason_code'], 'category')
        self.assertEqual(self.preview(include_tags=['one', 'three'])['items'][0]['reason_code'], 'include_tag')
        task['tags'] = 'HR, one'
        self.assertEqual(self.preview()['items'][0]['reason_code'], 'excluded_tag')
        task['tags'] = 'Hr'
        self.assertEqual(self.preview()['counts']['eligible'], 1)
        task['tags'] = ''
        self.assertEqual(self.preview(include_tags=['required'])['counts']['eligible'], 1)
        self.assertEqual(self.preview(include_untagged=False)['items'][0]['reason_code'], 'untagged')

    def test_excluded_directory_uses_component_boundary(self):
        task = self.q2.qb[self.hash]
        task['save_path'] = '/downloads/PTS2'
        self.assertEqual(self.preview(excluded_dirs=['/downloads/PTS'])['counts']['eligible'], 1)
        task['save_path'] = '/downloads/PTS/sub'
        self.assertEqual(self.preview(excluded_dirs=['/downloads/PTS/'])['items'][0]['reason_code'], 'excluded_dir')

    def test_completed_is_separate_from_transfer_eligibility(self):
        self.q2.qb[self.hash].update(state='checkingUP')
        report = self.preview()
        self.assertEqual(report['counts']['completed'], 1)
        self.assertEqual(report['items'][0]['reason_code'], 'not_ready')
        self.q2.qb[self.hash].update(state='missingFiles')
        self.assertEqual(self.preview()['counts']['completed'], 0)

    def test_duplicate_default_skips_and_never_touches_downloaders(self):
        self.t2.tr[self.hash] = tr_task(self.hash)
        self.save()
        result = self.run_rule()
        self.assertIsNone(result['job'])
        self.assertEqual(result['counts']['target_existing'], 1)
        self.assertEqual(self.preview()['items'][0]['reason_code'], 'target_exists')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.q2.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.t2.tr[self.hash]['status'], 6)

    def test_duplicate_conflict_refused_even_with_delete_duplicate_enabled(self):
        self.t2.tr[self.hash] = tr_task(self.hash, downloadDir='/other')
        self.assertEqual(self.preview(delete_duplicate_source=True)['items'][0]['reason_code'], 'target_conflict')
        self.assertEqual(self.mutating_calls(), [])

    def test_failed_fresh_read_is_not_an_empty_success(self):
        self.save()
        self.t2.errors['tr'] = 'private-address secret'
        with self.assertRaises(downloads.ManagementError) as error:
            self.run_rule()
        self.assertEqual(error.exception.status, 502)
        self.assertNotIn('private-address', str(error.exception))
        self.assertIsNone(self.rules.state['last_run_at'])
        self.assertEqual(self.fleet.managers, {})

    def test_strict_fields_types_paths_and_revision_fail_safely(self):
        for changes in ({'notify': 1}, {'max_per_run': True}, {'max_per_run': 51}, {'include_tags': ['a,b']},
                        {'path_mappings': [{'qb': '/', 'tr': '/downloads'}]}, {'excluded_dirs': ['/']},
                        {'excluded_dirs': ['/downloads/../private']}, {'include_tags': ['a\nsecret']}, {'unknown': True}):
            with self.subTest(changes=changes), self.assertRaises(downloads.ManagementError):
                self.preview(**changes)
        for revision in (None, '', 'a' * 63, '非ASCII' * 20, 'A' * 64, 0):
            with self.subTest(revision=revision), self.assertRaises(downloads.ManagementError) as error:
                self.rules.update({'revision': revision, 'values': self.values})
            self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.rules.path.exists())

    def test_save_never_runs_requires_enable_confirmation_and_rejects_stale_revision(self):
        revision = self.rules.revision()
        with self.assertRaises(downloads.ManagementError):
            self.rules.update({'revision': revision, 'values': {**self.values, 'enabled': True}})
        result = self.save()
        self.assertTrue(result['saved'])
        self.assertEqual(self.fleet.managers, {})
        self.assertEqual(self.mutating_calls(), [])
        with self.assertRaises(downloads.ManagementError) as error:
            self.rules.update({'revision': revision, 'values': self.values})
        self.assertEqual(error.exception.status, 409)
        self.save(enabled=True)
        self.assertGreater(self.rules.state['next_run_at'], self.now)
        self.assertEqual(self.mutating_calls(), [])

    def test_manual_run_requires_saved_rule_and_exact_selected_composite_references(self):
        with self.assertRaises(downloads.ManagementError):
            self.run_rule()
        self.save()
        for tasks in ([self.task('q1')], [self.task('q2', 'a' * 40)], [self.task('t1')]):
            with self.subTest(tasks=tasks), self.assertRaises(downloads.ManagementError):
                self.run_rule(tasks)
        self.assertEqual(self.fleet.managers, {})
        result = self.run_rule([self.task('q2')])
        self.assertEqual(result['job']['source_instance_id'], 'q2')
        self.assertTrue(result['job']['rule_run'])
        self.assertEqual([item['hash'] for item in result['job']['items']], [self.hash])

    def test_all_eligible_are_queued_despite_legacy_caps_and_preview_truncation(self):
        self.q2.qb = {f'{n:040x}': qb_task(f'{n:040x}', name=f'Task {n}') for n in range(1, 131)}
        self.settings['transfer_max_per_job'] = 3
        self.save(max_per_run=5)
        report = self.preview()
        self.assertEqual((report['counts']['eligible'], report['counts']['selected']), (130, 130))
        self.assertNotIn('over_batch', report['counts'])
        self.assertNotIn('max_per_run', self.rules.public()['values'])
        self.assertTrue(report['truncated'])
        self.assertEqual(len(report['items']), 100)
        result = self.run_rule()
        self.assertEqual([item['hash'] for item in result['job']['items']], sorted(self.q2.qb))
        self.assertEqual(result['counts']['selected'], 130)

    def test_exact_selected_queue_has_no_cap_and_preserves_request_order(self):
        self.q2.qb = {f'{n:040x}': qb_task(f'{n:040x}') for n in range(1, 151)}
        self.settings['transfer_max_per_job'] = 1
        self.save(max_per_run=1)
        selected = [self.task('q2', value) for value in reversed(self.q2.qb)]
        result = self.run_rule(selected)
        self.assertEqual([item['hash'] for item in result['job']['items']], [ref['hash'] for ref in selected])
        self.assertEqual(result['counts']['selected'], 150)

    def test_legacy_rule_load_ignores_cap_without_rewriting_configuration(self):
        self.q2.qb = {f'{n:040x}': qb_task(f'{n:040x}') for n in range(1, 131)}
        pull.save_json(self.rules.path, {**self.values, 'max_per_run': 1})
        before = self.rules.path.read_bytes()
        self.rules = transfer.Rules(self.fleet)
        self.assertNotIn('max_per_run', self.rules.public()['values'])
        self.assertEqual(self.preview()['counts']['selected'], 130)
        self.assertEqual(self.rules.path.read_bytes(), before)
        self.assertEqual(self.mutating_calls(), [])

    def test_empty_mapping_previews_reasons_but_cannot_execute(self):
        self.save(path_mappings=[])
        self.assertEqual(self.preview()['items'][0]['reason_code'], 'path')
        with self.assertRaises(downloads.ManagementError):
            self.run_rule()
        with self.assertRaises(downloads.ManagementError):
            self.save(enabled=True)

    def test_busy_save_and_run_refused_preview_remains_read_only(self):
        self.save()
        self.run_rule()
        with self.assertRaises(downloads.ManagementError):
            self.save()
        with self.assertRaises(downloads.ManagementError):
            self.run_rule()
        self.assertEqual(self.preview()['counts']['eligible'], 1)
        self.assertTrue(self.rules.public()['busy'])

    def test_new_target_copy_keeps_source_restores_original_state_and_records_history(self):
        self.save(delete_source=False, target_labels=['Transferred'])
        self.run_rule()
        job = self.complete()
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['items'][0]['source_kept'])
        self.assertFalse(job['items'][0].get('source_removed', False))
        self.assertEqual(self.q2.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.t2.tr[self.hash]['status'], 6)
        self.assertIn('Transferred', self.t2.tr[self.hash]['labels'])
        self.assertFalse(any(call[0] == 'qremove' for call in self.q2.calls))
        self.assertEqual(self.preview()['items'][0]['reason_code'], 'already_processed')
        self.assertEqual(self.rules.public()['runtime']['completed_count'], 1)
        self.assertEqual(self.rules.state['last_result']['source_kept'], 1)
        self.fleet = self.make_fleet()
        self.rules = self.fleet.transfer_rules
        self.assertEqual(self.preview()['items'][0]['reason_code'], 'already_processed')

    def test_copy_paused_source_remains_paused_and_target_start_flag_is_independent(self):
        self.q2.qb[self.hash]['state'] = 'stoppedUP'
        self.save(delete_source=False, start_after_verify=False)
        self.run_rule()
        self.complete()
        self.assertEqual(self.q2.qb[self.hash]['state'], 'stoppedUP')
        self.assertEqual(self.t2.tr[self.hash]['status'], 0)

    def test_duplicate_delete_is_independent_and_merges_existing_labels_after_full_verify(self):
        self.t2.tr[self.hash] = tr_task(self.hash, labels=['Original'])
        self.save(delete_source=False, delete_duplicate_source=True, target_labels=['Transferred'], start_after_verify=False)
        self.run_rule()
        self.fleet._tick()
        self.assertIn(self.hash, self.q2.qb)
        self.assertEqual(self.t2.tr[self.hash]['status'], 2)
        self.assertEqual(self.t2.tr[self.hash]['labels'], ['Original'])
        self.t2.tr[self.hash].update(status=0, haveValid=100, haveUnchecked=0)
        self.advance(11)
        self.fleet._tick()
        self.rules.observe()
        self.assertNotIn(self.hash, self.q2.qb)
        self.assertEqual(self.t2.tr[self.hash]['status'], 0)
        self.assertEqual(set(self.t2.tr[self.hash]['labels']), {'Original', 'Transferred', 'pts保种组'})
        batch = json.loads((self.directory / 'batch.json').read_text())
        permanent = set(batch['seen_hashes']) | {value for record in batch['accepted'].values() for value in pull.record_hashes(record)}
        self.assertIn(self.hash, permanent)
        self.assertFalse(any(call[0] == 'torrent-remove' for call in self.t2.calls))
        self.assertEqual(self.rules.state['last_result']['source_removed'], 1)

    def test_failed_full_verify_restores_source_and_records_no_success_history(self):
        self.save(delete_source=True)
        self.run_rule()
        self.fleet._tick()
        self.t2.tr[self.hash].update(status=0, haveValid=99, haveUnchecked=0)
        self.advance(11)
        self.fleet._tick()
        self.rules.observe()
        self.assertEqual(self.manager().state['job']['status'], 'failed')
        self.assertIn(self.hash, self.q2.qb)
        self.assertEqual(self.q2.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.t2.tr[self.hash]['status'], 0)
        self.assertEqual(self.rules.state['history'], {})

    def test_rule_snapshot_drives_initial_checks_and_survives_restart_setting_changes(self):
        self.q2.qb[self.hash]['save_path'] = '/source/path'
        self.save(path_mappings=[{'qb': '/source', 'tr': '/target'}], delete_source=False,
                  start_after_verify=False, target_labels=['Snapshot'])
        self.run_rule()
        self.assertEqual(self.manager().state['job']['transfer_options']['path_mappings'], self.values['path_mappings'])
        self.settings['transfer_path_mappings'] = [{'qb': '/different', 'tr': '/wrong'}]
        self.fleet = self.make_fleet()
        self.rules = self.fleet.transfer_rules
        self.fleet._tick()
        self.assertEqual(self.t2.tr[self.hash]['downloadDir'], '/target/path')
        self.t2.tr[self.hash].update(status=0, haveValid=100, haveUnchecked=0)
        self.advance(11)
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['status'], 'completed')
        self.assertIn(self.hash, self.q2.qb)
        self.assertEqual(self.t2.tr[self.hash]['status'], 0)
        self.assertIn('Snapshot', self.t2.tr[self.hash]['labels'])

    def test_cancel_restores_source_and_existing_target_then_leaves_no_history(self):
        self.t2.tr[self.hash] = tr_task(self.hash, labels=['Existing'])
        self.save(delete_duplicate_source=True)
        result = self.run_rule()
        self.fleet._tick()
        self.fleet.cancel({'job_id': result['job']['id']})
        self.fleet._tick()
        self.rules.observe()
        self.assertEqual(self.manager().state['job']['status'], 'cancelled')
        self.assertEqual(self.q2.qb[self.hash]['state'], 'uploading')
        self.assertEqual(self.t2.tr[self.hash]['status'], 6)
        self.assertEqual(self.t2.tr[self.hash]['labels'], ['Existing'])
        self.assertEqual(self.rules.state['history'], {})

    def test_duplicate_appearing_after_submission_skips_without_touching_either_task(self):
        self.save()
        self.run_rule()
        self.t2.tr[self.hash] = tr_task(self.hash)
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['status'], 'completed')
        self.assertEqual(self.manager().state['job']['items'][0]['phase'], 'skipped')
        self.assertEqual(self.mutating_calls(), [])

    def test_rule_filters_are_rechecked_before_starting_queued_task(self):
        self.save()
        self.run_rule()
        self.q2.qb[self.hash]['tags'] = 'HR'
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['items'][0]['phase'], 'skipped')
        self.assertEqual(self.mutating_calls(), [])

    def test_lost_add_and_remove_receipts_resume_without_repeated_delete(self):
        self.save()
        self.run_rule()
        self.t2.faults['torrent-add'] = ['lost']
        self.fleet._tick()
        self.assertIn(self.hash, self.q2.qb)
        self.fleet = self.make_fleet()
        self.rules = self.fleet.transfer_rules
        self.fleet._tick()
        self.t2.tr[self.hash].update(status=0, haveValid=100, haveUnchecked=0)
        self.advance(11)
        self.q2.faults['qremove'] = ['lost']
        self.fleet._tick()
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['status'], 'completed')
        self.assertEqual(sum(call[0] == 'qremove' for call in self.q2.calls), 1)

    def test_lost_label_receipt_retries_before_removing_source(self):
        self.save(target_labels=['Transferred'])
        self.run_rule()
        self.fleet._tick()
        self.t2.tr[self.hash].update(status=0, haveValid=100, haveUnchecked=0, labels=['Existing'])
        self.advance(11)
        self.t2.faults['torrent-set'] = ['lost']
        self.fleet._tick()
        self.assertIn(self.hash, self.q2.qb)
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['status'], 'completed')
        self.assertEqual(sum(call[0] == 'torrent-set' for call in self.t2.calls), 1)

    def test_completion_observed_before_old_job_is_replaced_by_manual_transfer(self):
        self.save(delete_source=False)
        self.run_rule()
        self.complete()
        # Remove the destination externally, leaving a retained source and a persisted completed job.
        self.t2.tr.clear()
        self.rules.state.update(history={}, observed_job_ids=[])
        self.fleet.transfer({'tasks': [self.task('q2')], 'target_instance_id': 't2', 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'})
        self.assertEqual(self.rules.public()['runtime']['completed_count'], 1)

    def test_scheduler_is_serial_retries_failed_reads_and_persists_next_occurrence(self):
        self.save(enabled=True)
        due = self.rules.state['next_run_at']
        self.now = due + 1
        self.t2.errors['tr'] = 'private'
        self.rules.tick()
        self.assertEqual(self.rules.state['next_run_at'], due)
        self.assertEqual(self.rules.state['last_result']['status'], 'deferred')
        self.assertIsNone(self.rules.state['last_run_at'])
        self.t2.errors = {}
        self.advance(61)
        self.rules.tick()
        self.assertTrue(self.fleet.busy())
        job_id = self.manager().state['job']['id']
        next_due = self.rules.state['next_run_at']
        self.assertGreater(next_due, self.now)
        self.advance(6 * 3600)
        self.rules.tick()
        self.assertEqual(self.manager().state['job']['id'], job_id)
        self.assertEqual(self.rules.state['next_run_at'], next_due)

    def test_pause_disables_saved_automation_and_survives_reconstruction(self):
        self.save(enabled=True)
        self.rules.pause()
        self.assertFalse(self.rules.values['enabled'])
        self.assertIsNone(self.rules.state['next_run_at'])
        self.assertFalse(transfer.Rules(self.fleet).values['enabled'])

    def test_guard_is_never_nested_when_recording_a_rule_job(self):
        depth = [0]
        @contextlib.contextmanager
        def guard():
            depth[0] += 1
            self.assertEqual(depth[0], 1)
            try:
                yield
            finally:
                depth[0] -= 1
        self.fleet.guard = guard
        self.save()
        self.run_rule()
        self.assertEqual(depth[0], 0)

    def test_saved_runtime_corruption_is_reported_without_resetting_history(self):
        self.save()
        broken = {**self.rules.state, 'history': {'bad': 123}}
        pull.save_json(self.rules.state_path, broken)
        before = self.rules.state_path.read_bytes()
        with self.assertRaises(downloads.ManagementError):
            transfer.Rules(self.fleet)
        self.assertEqual(self.rules.state_path.read_bytes(), before)

    def test_same_hash_new_pair_is_independent_of_success_history(self):
        self.save(delete_source=False)
        self.run_rule()
        self.complete()
        self.assertEqual(self.preview()['items'][0]['reason_code'], 'already_processed')
        self.t1.tr.clear()
        report = self.preview(target_instance_id='t1')
        self.assertEqual(report['counts']['already_processed'], 0)
        self.assertEqual(report['counts']['eligible'], 1)

    def test_retained_source_restart_after_lost_resume_uses_original_options(self):
        self.save(delete_source=False, start_after_verify=False)
        self.run_rule()
        self.fleet._tick()
        self.t2.tr[self.hash].update(status=0, haveValid=100, haveUnchecked=0)
        self.advance(11)
        with patch.object(self.q2, 'qstart', side_effect=downloads.ManagementError('模拟恢复回执丢失', 502)):
            self.fleet._tick()
        self.q2.qb[self.hash]['state'] = 'uploading'
        self.fleet = self.make_fleet()
        self.rules = self.fleet.transfer_rules
        self.fleet._tick()
        self.assertEqual(self.manager().state['job']['status'], 'completed')
        self.assertTrue(self.manager().state['job']['items'][0]['source_kept'])
        self.assertEqual(self.t2.tr[self.hash]['status'], 0)

    def test_native_guard_rejection_keeps_due_pending_without_creating_job(self):
        self.save(enabled=True)
        self.now = self.rules.state['next_run_at'] + 1
        due = self.rules.state['next_run_at']
        @contextlib.contextmanager
        def locked():
            raise downloads.ManagementError('运行锁正在使用', 409)
            yield
        self.fleet.guard = locked
        self.rules.tick()
        self.assertEqual(self.rules.state['next_run_at'], due)
        self.assertFalse(self.fleet.busy())
        self.assertIsNone(self.manager().state.get('job'))

    def test_active_refill_blocks_scheduler_before_reading_and_does_not_advance_due(self):
        self.save(enabled=True)
        self.now = self.rules.state['next_run_at'] + 1
        due = self.rules.state['next_run_at']
        self.fleet.runner_busy = lambda: True
        with patch.object(self.rules, 'plan', side_effect=AssertionError('must not read during refill')):
            self.fleet._tick()
        self.assertEqual(self.rules.state['next_run_at'], due)
        self.assertIsNone(self.rules.state['last_run_at'])
        self.assertEqual(self.fleet.managers, {})



if __name__ == '__main__':
    unittest.main()
