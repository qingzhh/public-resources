import copy
import unittest
from seedkeep_tagging import TaggingError, has_tag, inventory_counts, log_counters, summarize_rows, tag_value

TAG = 'pts保种组'
def h(char, length=40):
    return char * length

class TaggingTests(unittest.TestCase):
    def test_single_tag_validation(self):
        self.assertEqual(tag_value('  '+TAG+'  '),TAG)
        for value in ('', ' ', 'one,two', 'x\nlabel', 'x'*129, None, True):
            with self.subTest(value=value), self.assertRaises(TaggingError):
                tag_value(value)

    def test_exact_label_not_category_history_or_substring(self):
        qb=[{'hash':h('a'),'tags':TAG,'category':'unrelated'},
            {'hash':h('b'),'tags':'old-'+TAG,'category':TAG}]
        state={'accepted':{'b':{'hash':h('b')}}}
        before=copy.deepcopy(state)
        value=inventory_counts(state,qb,{},TAG)
        self.assertEqual(value,{'managed_active':1,'managed_qb':1,'managed_tr':0,'managed_both':0})
        self.assertEqual(state,before)
        self.assertTrue(has_tag({'labels':['迁移',TAG]},'tr',TAG))
        self.assertFalse(has_tag({'labels':[TAG+'-old']},'tr',TAG))

    def test_multiple_instances_cross_client_and_hybrid_aliases_count_once(self):
        qb=[{'hash':h('a'),'infohash_v2':h('b',64),'tags':TAG}, {'hash':h('a'),'tags':TAG}]
        tr=[{'hashString':h('b',64),'labels':[TAG]}, {'hashString':h('b',64),'labels':[TAG]}]
        self.assertEqual(inventory_counts({},qb,tr,TAG),{'managed_active':1,'managed_qb':1,'managed_tr':1,'managed_both':1})

    def test_record_aliases_bridge_identity_without_importing_history(self):
        state={'unconfirmed':{'x':{'hash':h('a'),'hashes':[h('b',64)]}}}
        qb=[{'hash':h('a'),'tags':TAG}]
        tr=[{'hashString':h('b',64),'labels':[TAG]}]
        self.assertEqual(inventory_counts(state,qb,tr,TAG)['managed_active'],1)

    def test_downloading_paused_and_unrecorded_tasks_all_occupy_total(self):
        qb=[{'hash':h('a'),'tags':TAG,'state':'downloading'}, {'hash':h('b'),'tags':TAG,'state':'stoppedUP'}]
        self.assertEqual(inventory_counts({},qb,{},TAG)['managed_active'],2)
        self.assertEqual(inventory_counts({},qb,{},'changed')['managed_active'],0)

    def test_missing_label_or_identity_fails_instead_of_counting_zero(self):
        for raw in ({'hash':h('a')}, {'hash':h('a'),'tags':None}, {'hash':'bad','tags':TAG}):
            with self.subTest(raw=raw),self.assertRaises(TaggingError):
                inventory_counts({},[raw],{},TAG)
        with self.assertRaises(TaggingError):
            inventory_counts({},[],[{'hashString':h('a'),'labels':None}],TAG)

    def row(self,key,state='valid',kind='qb',instance='q',tag=TAG):
        raw={'hash':key,'tags':tag} if kind=='qb' else {'hashString':key,'labels':[tag]}
        return {'hash':key,'client':kind,'instance_id':instance,'validity':state,'_aliases':{key},
                '_qb':raw if kind=='qb' else None,'_tr':raw if kind=='tr' else None}

    def test_summary_distinct_states_and_zero_values(self):
        rows=[self.row(h(c),state) for c,state in zip('abcde',('valid','invalid','unknown','inactive','downloading'))]
        value=summarize_rows(rows,[{'id':'q','enabled':True,'connected':True}],TAG,123,10)
        self.assertEqual(value['total'],5)
        self.assertEqual([value[k] for k in ('valid','invalid','unknown','inactive','downloading')],[1]*5)
        self.assertEqual(log_counters(value)['seeding_invalid'],1)
        empty=summarize_rows([],[],TAG,123)
        self.assertEqual(empty['total'],0)
        self.assertEqual(log_counters(empty)['seeding_valid'],0)

    def test_any_required_instance_failure_returns_unknown_not_stale_partial(self):
        rows=[self.row(h('a'))]
        instances=[{'id':'q','enabled':True,'connected':True},{'id':'t','enabled':True,'connected':False}]
        value=summarize_rows(rows,instances,TAG,123)
        self.assertFalse(value['connected'])
        self.assertIsNone(value['total'])
        self.assertIsNone(value['valid'])
        self.assertEqual(log_counters(value),{})
        instances[1]['enabled']=False
        self.assertEqual(summarize_rows(rows,instances,TAG,123)['total'],1)

    def test_migration_overlap_valid_copy_and_conflicting_receipts(self):
        rows=[self.row(h('a'),'inactive'),self.row(h('a'),'valid','tr','t')]
        instances=[{'id':key,'enabled':True,'connected':True} for key in ('q','t')]
        value=summarize_rows(rows,instances,TAG,123)
        self.assertEqual((value['total'],value['valid'],value['both']),(1,1,1))
        rows[0]['validity']='invalid'
        value=summarize_rows(rows,instances,TAG,123)
        self.assertEqual((value['total'],value['valid'],value['invalid'],value['unknown']),(1,0,0,1))

    def test_explicit_unregistered_is_invalid_but_other_and_missing_report_unknown(self):
        row=self.row(h('a'),'unknown')
        row['_seeding_validity']='invalid'
        value=summarize_rows([row,self.row(h('b'),'other')],[{'id':'q','enabled':True,'connected':True}],TAG,123)
        self.assertEqual((value['invalid'],value['unknown']),(1,1))


    def test_pending_alias_groups_reserved_once_and_live_untagged_copy_not_reserved(self):
        from seedkeep_tagging import inventory_budget
        state = {'accepted': {'bridge': {'hash': h('a'), 'hashes': [h('b', 64)]}},
                 'pending': {'one': {'hash': h('b', 64), 'hashes': [h('c')]},
                             'same': {'hash': h('c')}, 'new': {'hash': h('d')}}}
        before = copy.deepcopy(state)
        value = inventory_budget(state, [{'hash': h('a'), 'tags': ''}], {}, TAG)
        self.assertEqual((value['managed_active'], value['pending_reserved']), (0, 1))
        value = inventory_budget(state, [], {}, TAG)
        self.assertEqual((value['managed_active'], value['pending_reserved']), (0, 2))
        self.assertEqual(state, before)

    def test_transitive_aliases_bridge_current_and_summary(self):
        state = {'accepted': {'a': {'hash': h('a'), 'hashes': [h('b', 64)]},
                              'b': {'hash': h('b', 64), 'hashes': [h('c')]}}}
        value = inventory_counts(state, [{'hash': h('a'), 'tags': TAG}], [{'hashString': h('c'), 'labels': [TAG]}], TAG)
        self.assertEqual((value['managed_active'], value['managed_both']), (1, 1))
        rows = [self.row(h('a')), self.row(h('c'), kind='tr', instance='t')]
        value = summarize_rows(rows, [{'id': key, 'enabled': True, 'connected': True} for key in ('q', 't')], TAG, 123, state=state)
        self.assertEqual((value['total'], value['both']), (1, 1))

    def test_present_but_invalid_optional_identity_is_unknown(self):
        with self.assertRaises(TaggingError):
            inventory_counts({}, [{'hash': h('a'), 'tags': TAG, 'infohash_v2': 'bad'}], {}, TAG)

class DisplaySummaryTests(unittest.TestCase):
    def row(self, key, state='uploading', validity='valid', instance='q', kind='qb', tag=TAG, **fields):
        raw = ({'hash': key, 'tags': tag, 'state': state, 'progress': 1 if state.endswith('UP') or state == 'uploading' else .5}
               if kind == 'qb' else {'hashString': key, 'labels': [tag], 'status': state, 'percentDone': 1})
        raw.update(fields)
        return {'hash': key, 'client': kind, 'instance_id': instance, 'validity': validity,
                '_aliases': {key}, '_qb': raw if kind == 'qb' else None, '_tr': raw if kind == 'tr' else None}

    def instances(self, *ids):
        return [{'id': iid, 'name': '实际名称 ' + iid, 'type': 'tr' if iid.startswith('t') else 'qb',
                 'enabled': True, 'connected': True} for iid in (ids or ('q',))]

    def summary(self, rows, instances=None, history=None):
        from seedkeep_tagging import summarize_display
        return summarize_display(rows, instances or self.instances(), TAG, 123, 10, state=history)

    def test_completed_counts_are_separate_from_inventory_and_actual_downloads(self):
        rows = [self.row(h('a')), self.row(h('b'), validity='invalid'),
                self.row(h('c'), validity='unknown'), self.row(h('d'), 'stoppedUP', 'inactive'),
                self.row(h('e'), 'queuedUP', 'inactive'), self.row(h('f'), 'downloading', 'invalid'),
                self.row(h('1'), 'queuedDL', 'unknown'), self.row(h('2'), 'pausedDL', 'unknown'),
                self.row(h('3'), 'checkingDL', 'downloading')]
        before = copy.deepcopy(rows)
        value = self.summary(rows)
        self.assertEqual([value[k] for k in ('completed_total', 'valid', 'invalid', 'unknown', 'inactive', 'downloading')],
                         [5, 1, 1, 1, 2, 2])
        self.assertEqual(summarize_rows(rows, self.instances(), TAG, 123)['total'], 9)
        self.assertEqual(rows, before)

    def test_upload_recheck_is_completed_download_recheck_and_pause_are_neither(self):
        states = ('checkingUP', 'stoppedDL', 'pausedDL', 'checkingDL', 'forcedDL', 'metaDL', 'forcedMetaDL', 'stalledDL')
        rows = [self.row(h(c), state, 'inactive') for c, state in zip('abcdef12', states)]
        value = self.summary(rows)
        self.assertEqual((value['completed_total'], value['inactive'], value['downloading']), (1, 1, 4))

    def test_transmission_complete_pause_queue_and_unfinished_queue_are_distinct(self):
        rows = [self.row(h('a'), 0, 'inactive', kind='tr'), self.row(h('b'), 5, 'inactive', kind='tr'),
                self.row(h('c'), 3, 'unknown', kind='tr', percentDone=.5),
                self.row(h('d'), 4, 'unknown', kind='tr', percentDone=.5),
                self.row(h('e'), 0, 'unknown', kind='tr', percentDone=.5),
                self.row(h('f'), 1, 'unknown', kind='tr', percentDone=.5),
                self.row(h('1'), 2, 'unknown', kind='tr', percentDone=.5),
                self.row(h('2'), 4, 'unknown', kind='tr', percentDone=.5, error=1)]
        value = self.summary(rows)
        self.assertEqual((value['completed_total'], value['inactive'], value['downloading']), (2, 2, 2))

    def test_exact_scope_excludes_category_and_history_and_substring(self):
        rows = [self.row(h('a'), tag=''), self.row(h('b'), tag=TAG + '-old'), self.row(h('c'))]
        rows[0]['_qb']['category'] = TAG
        history = {'accepted': {'old': {'hash': h('d')}}}
        self.assertEqual(self.summary(rows, history=history)['completed_total'], 1)

    def test_completed_copy_precedence_cross_instance_aliases_and_per_instance_counts(self):
        rows = [self.row(h('a'), 'downloading', 'invalid'), self.row(h('b', 64), 6, instance='t', kind='tr')]
        history = {'accepted': {'bridge': {'hash': h('a'), 'hashes': [h('b', 64)]}}}
        value = self.summary(rows, self.instances('q', 't'), history)
        self.assertEqual((value['completed_total'], value['valid'], value['invalid'], value['downloading']), (1, 1, 0, 0))
        local = {row['instance_id']: row for row in value['instances']}
        self.assertEqual((local['q']['completed_total'], local['q']['downloading']), (0, 1))
        self.assertEqual((local['t']['completed_total'], local['t']['downloading']), (1, 0))
        self.assertEqual(local['t']['name'], '实际名称 t')

    def test_completed_validity_conflicts_stay_unknown_and_instances_deduplicate(self):
        rows = [self.row(h('a')), self.row(h('a'), validity='invalid'), self.row(h('a'))]
        value = self.summary(rows)
        self.assertEqual((value['completed_total'], value['valid'], value['invalid'], value['unknown']), (1, 0, 0, 1))
        self.assertEqual(value['instances'][0]['completed_total'], 1)

    def test_untagged_completed_copy_does_not_override_tagged_download(self):
        rows = [self.row(h('a'), tag=''), self.row(h('a'), 'queuedDL', 'unknown', instance='q2')]
        value = self.summary(rows, self.instances('q', 'q2'))
        self.assertEqual((value['completed_total'], value['downloading']), (0, 1))

    def test_untagged_alias_bridge_deduplicates_selected_identities(self):
        rows = [self.row(h('a')), self.row(h('b', 64), tag=''), self.row(h('c'))]
        rows[1]['_aliases'] = {h('a'), h('b', 64), h('c')}
        self.assertEqual(self.summary(rows)['completed_total'], 1)

    def test_untrusted_completion_is_unknown_instead_of_zero(self):
        for state in ('futureState', 'moving', 'error', 'missingFiles', 'checkingResumeData'):
            value = self.summary([self.row(h('a'), state)])
            self.assertIsNone(value['completed_total'])
            self.assertIsNone(value['downloading'])
            self.assertEqual(value['completion_unknown'], 1)
        for progress in (None, True, float('nan'), -1, 2, '1'):
            value = self.summary([self.row(h('a'), 6, kind='tr', percentDone=progress)])
            self.assertIsNone(value['completed_total'])
        self.assertIsNone(self.summary([self.row(h('a'), progress=.5)])['completed_total'])

    def test_known_complete_copy_still_confirms_identity_when_other_copy_is_unknown(self):
        value = self.summary([self.row(h('a')), self.row(h('a'), 'moving')])
        self.assertEqual((value['completed_total'], value['valid'], value['completion_unknown']), (1, 1, 0))

    def test_connection_failure_keeps_known_instance_and_global_unknown(self):
        instances = self.instances('q', 't')
        instances[1]['connected'] = False
        value = self.summary([self.row(h('a'))], instances)
        self.assertFalse(value['connected'])
        self.assertIsNone(value['completed_total'])
        self.assertEqual(value['instances'][0]['completed_total'], 1)
        self.assertIsNone(value['instances'][1]['completed_total'])

    def test_disabled_and_confirmed_empty_are_not_confused(self):
        instances = self.instances()
        instances[0]['enabled'] = False
        value = self.summary([self.row(h('a'))], instances)
        self.assertEqual((value['completed_total'], value['downloading']), (0, 0))
        self.assertIsNone(value['instances'][0]['completed_total'])
        self.assertEqual(self.summary([])['completed_total'], 0)

    def test_missing_label_never_makes_known_empty(self):
        row = self.row(h('a'))
        row['_qb'].pop('tags')
        value = self.summary([row])
        self.assertFalse(value['connected'])
        self.assertIsNone(value['completed_total'])


class TagFleetIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_seedkeep_fleet import FleetTests
        self.make_fleet = lambda: FleetTests.make_fleet(self)
        FleetTests.setUp(self)

    def test_existing_unrecorded_label_tasks_and_states_without_history_import(self):
        from test_seedkeep_management import qb_task
        before = (self.directory / 'batch.json').read_bytes()
        self.q2.qb[h('b')] = qb_task(h('b'), progress=.5, completed=50, amount_left=50, state='downloading')
        self.q2.qb[h('c')] = qb_task(h('c'), state='stoppedUP')
        self.q2.qb[h('d')] = qb_task(h('d'), seeders=11)
        self.q2.qb[h('e')] = qb_task(h('e'), tags=TAG+'-old', category=TAG)
        value = self.fleet.snapshot(True)
        summary = value['seedkeep']
        self.assertEqual((summary['total'], summary['valid'], summary['invalid'], summary['inactive'], summary['downloading']), (4,1,1,1,1))
        display = value['seedkeep_display']
        self.assertEqual((display['completed_total'], display['valid'], display['invalid'], display['inactive'], display['downloading']),
                         (3, 1, 1, 1, 1))
        self.assertEqual({item['instance_id'] for item in display['instances']}, {item['id'] for item in value['instances']})
        self.assertFalse(next(row for row in value['items'] if row['hash']==h('e'))['managed'])
        self.assertEqual((self.directory / 'batch.json').read_bytes(), before)

    def test_failure_missing_tag_disabled_instance_and_confirmed_empty(self):
        self.fleet.snapshot(True)
        self.q2.errors['qb'] = 'offline'
        self.assertIsNone(self.fleet.snapshot(True)['seedkeep']['total'])
        self.q2.errors.clear()
        self.q2.qb[self.hash].pop('tags')
        self.assertIsNone(self.fleet.snapshot(True)['seedkeep']['total'])
        for instance in self.settings['downloaders']:
            instance['enabled'] = False
            instance['default'] = False
        self.assertEqual(self.fleet.snapshot(True)['seedkeep']['total'], 0)

    def test_display_preserves_every_native_copy_inside_one_instance(self):
        from test_seedkeep_management import qb_task
        self.q1.qb.clear(); self.q2.qb.clear(); self.t1.tr.clear(); self.t2.tr.clear()
        self.q2.qb[h('a')] = qb_task(h('a'), infohash_v2=h('b', 64))
        self.q2.qb[h('b', 64)] = qb_task(h('b', 64), state='downloading', progress=.5, completed=50, amount_left=50)
        value = self.fleet.snapshot(True)
        display = value['seedkeep_display']
        self.assertEqual((display['completed_total'], display['valid'], display['downloading']), (1, 1, 0))
        local = next(item for item in display['instances'] if item['instance_id'] == 'q2')
        self.assertEqual((local['completed_total'], local['downloading']), (1, 0))
        # The old merged task/inventory interface remains unchanged.
        self.assertEqual(len(value['items']), 1)
        self.assertEqual(value['seedkeep']['total'], 1)

    def test_copy_tags_survive_cross_instance_dedup_in_refill_snapshot(self):
        import seedkeep_pull as pull
        self.q2.qb[self.hash]['tags'] = ''
        self.t1.tr[self.hash]['labels'] = []
        qb,tr = pull.FleetClients(self.source,self.settings,self.factory).snapshot()
        self.assertEqual((len(qb),len(tr)),(1,1))
        counts = pull.managed_counts(self.batch,qb,tr,TAG)
        self.assertEqual((counts['managed_active'],counts['managed_qb'],counts['managed_tr']),(1,1,0))

    def test_tracker_general_failure_and_download_state_are_not_invalid(self):
        self.q1.qb.clear(); self.q2.qb.clear()
        track = self.t1.tr[self.hash]['trackerStats'][0]
        track.update(lastAnnounceSucceeded=False,lastAnnounceResult='Connection timed out', seederCount=-1)
        summary = self.fleet.snapshot(True)['seedkeep']
        self.assertEqual((summary['invalid'],summary['unknown']),(0,1))
        track['lastAnnounceResult'] = 'Unregistered torrent'
        summary = self.fleet.snapshot(True)['seedkeep']
        self.assertEqual((summary['invalid'],summary['unknown']),(1,0))

    def test_monitor_summary_has_numbers_but_get_does_not_repeat_logs(self):
        import seedkeep_logstore as logs
        self.fleet.snapshot(True); self.fleet.snapshot(True)
        self.assertFalse((self.directory/'management.log').exists())
        self.fleet._tick(); self.fleet._tick()
        rows = logs.Store(self.directory).read()['items']
        summaries = [row for row in rows if row.get('event')=='inventory_summary']
        self.assertEqual(len(summaries),1)
        self.assertEqual((summaries[0]['seeding_total'],summaries[0]['seeding_valid'],summaries[0]['seeding_invalid']),(1,1,0))

    def test_native_transfer_retains_editable_tag_other_labels_and_result_logs(self):
        import seedkeep_logstore as logs
        self.legacy.transfer_mode='native'
        self.settings['managed_tag']='自定义组'
        self.q2.qb[self.hash]['tags']='pts保种组,HR'
        self.fleet.transfer({'tasks':[{'instance_id':'q2','hash':self.hash}], 'target_instance_id':'t2','confirm':'MOVE_QB_TO_TR_KEEP_DATA'})
        self.fleet._tick()
        self.t2.tr[self.hash].update(status=6,percentDone=1,leftUntilDone=0,haveValid=100,haveUnchecked=0)
        for _ in range(4): self.fleet._tick()
        self.assertEqual(self.fleet.public_job()['status'],'completed')
        self.assertTrue({'自定义组','HR'}.issubset(self.t2.tr[self.hash]['labels']))
        self.assertNotIn(self.hash,self.q2.qb)
        self.assertFalse(any(method=='torrent-verify' for method,_ in self.t2.calls))
        rows=logs.Store(self.directory).read()['items']
        end=next(row for row in rows if row['event']=='management_job_finished')
        self.assertEqual((end['transfer_total'],end['transfer_completed'],end['transfer_failed'],end['transfer_waiting']),(1,1,0,0))

class TagWebIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_seedkeep_multi_web import MultiWebTests
        MultiWebTests.setUp(self)
        self.controller.snapshot=lambda force=False: (list(self.api.qb.values()),dict(self.api.tr),{'qb':True,'tr':True,'error':None})
        for task in self.api.tr.values():
            for track in task['trackerStats']: track['lastAnnounceTime']=self.now

    def request(self,*args,**kwargs):
        from test_seedkeep_multi_web import MultiWebTests
        return MultiWebTests.request(self,*args,**kwargs)

    def test_http_status_tag_scope_and_history_are_independent(self):
        import seedkeep_pull as pull
        from test_seedkeep_management import qb_task
        pull.save_json(self.directory/'batch.json',{'accepted':{'old':{'hash':h('c')}}})
        before=(self.directory/'batch.json').read_bytes()
        self.api.qb[h('b')]=qb_task(h('b'),progress=.5,amount_left=50,state='downloading')
        self.api.qb[h('c')]=qb_task(h('c'),tags='',category=TAG)
        # Inventory GETs now use only statistics populated by the background monitor.
        from test_seedkeep_pts import sample_payload
        self.controller.pts.fetcher = lambda source: sample_payload()
        self.controller.refresh_site_statistics()
        code,value,_=self.request('GET','status')
        self.assertEqual(code,200)
        self.assertEqual((value['seedkeep']['total'],value['seedkeep']['valid'],value['seedkeep']['downloading']),(2,1,1))
        self.assertEqual((value['managed_active'],value['accepted_total'],value['qb_total'],value['tr_total']),(2,1,3,1))
        display = value['seedkeep_display']
        self.assertEqual((display['completed_total'], display['valid'], display['downloading']), (1, 1, 1))
        self.assertEqual({item['instance_id'] for item in display['instances']}, {'qb', 'tr'})
        self.assertEqual(value['remaining'],698)
        self.assertEqual((self.directory/'batch.json').read_bytes(),before)

    def test_edit_tag_preserves_saved_frequency_switch_and_next_schedule(self):
        import copy
        import seedkeep_pull as pull
        settings=self.controller.settings()
        settings.update(refill_count_basis='site_effective',target=1200,refill_check_minutes=120,
            interval_hours=2,page_refresh_seconds=300,management_cache_seconds=300,monitor_interval_seconds=600)
        pull.save_json(self.config,settings)
        self.controller.runtime.update(automatic_enabled=True,next_run_at=self.now+7200)
        self.controller.save_runtime()
        before=copy.deepcopy(self.controller.runtime)
        tasks=copy.deepcopy((self.api.qb,self.api.tr))
        code,_,_=self.request('POST','settings',{'managed_tag':' 新组 '})
        self.assertEqual(code,200)
        value=self.controller.settings()
        self.assertEqual((value['managed_tag'],value['refill_count_basis'],value['refill_check_minutes'],value['interval_hours']),('新组','site_effective',120,2))
        self.assertEqual(self.controller.runtime,before)
        self.assertEqual((self.api.qb,self.api.tr),tasks)
        self.assertFalse(self.spawned)
        code,status,_=self.request('GET','status')
        self.assertEqual((code,status['seedkeep']['total']),(200,0))
        self.assertEqual((status['seedkeep_display']['completed_total'], status['seedkeep_display']['downloading']), (0, 0))

    def test_invalid_tag_atomic_settings_and_no_new_inventory_or_log(self):
        before=self.config.read_bytes()
        for tag in ('','x,y','x\nlabel','x'*129,True,None):
            code,_,_=self.request('POST','settings',{'managed_tag':tag})
            self.assertEqual(code,400)
            self.assertEqual(self.config.read_bytes(),before)
        self.assertFalse(self.api.calls)
        self.assertFalse((self.directory/'management.log').exists())

    def test_refill_log_zero_and_unknown_values_are_preserved_safely(self):
        self.controller.fleet.snapshot(True)
        self.controller.log_refill_check({'status':'at_target','site_current':0,'allowance':0})
        row=self.controller.logs()['items'][0]
        self.assertEqual((row['site_current'],row['refill_allowance'],row['seeding_invalid']),(0,0,0))
        self.controller.fleet.cache=None
        self.controller.log_refill_check(reason='unknown')
        row=self.controller.logs()['items'][0]
        self.assertNotIn('site_current',row)
        self.assertNotIn('seeding_total',row)

if __name__=='__main__':
    unittest.main()
