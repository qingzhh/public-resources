"""Offline registry validation, legacy references, and isolated API reads."""
import copy
import json
import unittest
from unittest.mock import patch
from seedkeep_downloaders import API, ManagementError
from seedkeep_instances import Registry

SOURCE = {'api_base': 'https://site.invalid', 'token': 'site-secret', 'host': 'qb.invalid', 'port': 8080,
          'username': 'old-q-user', 'password': 'old-q-secret', 'download_path': '/downloads/PTS',
          'category': 'old-category', 'tag': 'old-tag'}
SETTINGS = {'tr_url': 'http://tr.invalid/transmission/rpc',
            'connections': {'tr_username': 'login-user', 'tr_password': 'login-secret'}}


def instance(iid, kind='qb', **fields):
    return {'id': iid, 'type': kind, 'name': iid, 'url': 'http://' + iid + '.invalid' + ('/transmission/rpc' if kind == 'tr' else ''),
            'username': iid + '-user', 'password': iid + '-secret', 'enabled': True, 'default': False,
            'download_path': '/downloads/PTS', 'category': 'PTS', 'tag': 'keep', 'keep_torrent': True,
            'use_proxy': False, 'proxy_url': '', **fields}


class Probe(API):
    calls = []

    def qget(self, operation, fields=None, *, binary=False):
        self.calls.append(('qb', operation))
        if operation == 'app/preferences':
            return {'up_limit': 1024, 'dl_limit': 0, 'alt_up_limit': 2048, 'alt_dl_limit': 0}
        if operation == 'transfer/speedLimitsMode':
            return b'0'
        return []

    def rpc(self, method, arguments=None):
        self.calls.append(('tr', method))
        if method == 'session-get':
            return {'units': {'speed-bytes': 1000}, 'speed-limit-up': 10, 'speed-limit-down': 20,
                    'speed-limit-up-enabled': True, 'speed-limit-down-enabled': False, 'alt-speed-enabled': False,
                    'alt-speed-up': 3, 'alt-speed-down': 4}
        return {'torrents': []}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.source, self.settings = copy.deepcopy(SOURCE), copy.deepcopy(SETTINGS)
        self.registry = Registry(lambda: self.source, lambda: self.settings, Probe)
        Probe.calls = []

    def test_virtual_import_never_writes_or_copies_password(self):
        before = copy.deepcopy((self.source, self.settings))
        rows = self.registry.items()
        self.assertEqual([row['id'] for row in rows], ['qb', 'tr'])
        self.assertTrue(all('password' not in row for row in rows))
        self.assertEqual(rows[0]['credential_ref'], 'legacy-qb')
        self.assertEqual((self.source, self.settings), before)
        self.assertEqual(self.registry.primary()['id'], 'qb')

    def test_private_public_secrets_and_revision(self):
        public = self.registry.public()
        text = json.dumps(public)
        for secret in ('site-secret', 'old-q-secret', 'login-secret', 'credential_ref'):
            self.assertNotIn(secret, text)
        self.assertTrue(public['login_independent'])
        self.assertEqual(len(public['revision']), 64)
        self.assertTrue(all(row['password_configured'] for row in public['items']))
        self.settings['connections']['tr_password'] = 'other-secret'
        self.assertNotEqual(self.registry.public()['revision'], public['revision'])

    def test_save_pure_password_retention_and_clear(self):
        rows = self.registry.updated({'id': 'qb', 'name': 'renamed', 'password': ''})
        self.assertNotIn('downloaders', self.settings)
        self.assertEqual(rows[0]['credential_ref'], 'legacy-qb')
        self.settings['downloaders'] = rows
        rows = self.registry.updated({'id': 'qb', 'password': 'new-secret'})
        self.assertEqual(rows[0]['password'], 'new-secret')
        self.assertNotIn('credential_ref', rows[0])
        self.settings['downloaders'] = rows
        self.assertEqual(self.registry.updated({'id': 'qb', 'password': ''})[0]['password'], 'new-secret')
        rows = self.registry.updated({'id': 'qb', 'clear_password': True})
        self.assertEqual(rows[0]['password'], '')
        self.assertEqual(self.settings['connections']['tr_password'], 'login-secret')

    def test_add_default_switch_delete_only_values(self):
        draft = instance('unused', default=True)
        draft.pop('id')
        rows = self.registry.updated(draft)
        self.assertFalse(rows[0]['default'])
        self.assertTrue(rows[-1]['default'])
        self.settings['downloaders'] = rows
        self.assertEqual(self.registry.primary()['id'], rows[-1]['id'])
        remaining = self.registry.removed(rows[-1]['id'])
        self.assertEqual(len(self.settings['downloaders']), 3)
        self.settings['downloaders'] = remaining
        self.assertIsNone(self.registry.primary())
        self.assertEqual(Probe.calls, [])

    def test_empty_and_tr_only_registry_never_reads_legacy_qb(self):
        self.source = {}
        self.settings['downloaders'] = []
        self.assertEqual(self.registry.public()['items'], [])
        self.assertIsNone(self.registry.primary())
        self.settings['downloaders'] = [instance('t1', 'tr')]
        self.assertTrue(self.registry.api('t1').inventory()[2] == {})
        self.assertEqual(Probe.calls, [('tr', 'torrent-get')])

    def test_reject_bad_drafts_and_stored_config(self):
        for fields in ({'url': 'http://user:secret@host.invalid'}, {'url': 'file:///tmp/x'},
                       {'download_path': '/downloads/../private'}, {'enabled': 1}, {'password': None},
                       {'clear_password': 'yes'}, {'clear_password': True, 'password': 'new'},
                       {'name': ''}, {'type': 'tr', 'default': True}, {'extra': 'ignored'}):
            with self.subTest(fields=fields), self.assertRaises(ManagementError):
                self.registry.updated({'id': 'qb', **fields})
        for rows in (None, [instance('x'), instance('x')], [instance('../bad')],
                     [instance('q1', default=True), instance('q2', default=True)], [{'type': 'qb', 'url': 'http://x.invalid'}]):
            with self.subTest(rows=rows), self.assertRaises(ManagementError):
                self.settings['downloaders'] = rows
                self.registry.items()

    def test_type_immutable_id_unknown_and_disabled_default(self):
        for fields in ({'id': 'qb', 'type': 'tr'}, {'id': 'missing'}, {'id': 'qb', 'enabled': False}):
            with self.assertRaises(ManagementError):
                self.registry.updated(fields)
        rows = self.registry.updated({'id': 'qb', 'enabled': False, 'default': False})
        self.settings['downloaders'] = rows
        with self.assertRaises(ManagementError):
            self.registry.api('qb')
        with self.assertRaises(ManagementError):
            self.registry.removed('missing')

    def test_pair_resolves_selected_fields_and_independent_proxies(self):
        self.settings['connections'].update(qb_url='http://legacy.invalid', qb_password='override-secret')
        self.settings['downloaders'] = [instance('q2', use_proxy=True, proxy_url='http://qp.invalid'),
                                        instance('t2', 'tr', use_proxy=True, proxy_url='http://tp.invalid', category='archive', tag='one,two')]
        source, settings = self.registry.pair('q2', 't2')
        api = API(source, settings)
        self.assertEqual(api.qb_url, 'http://q2.invalid/api/v2/')
        self.assertEqual(api.source['password'], 'q2-secret')
        self.assertEqual(api.settings['tr_url'], 'http://t2.invalid/transmission/rpc')
        self.assertEqual(settings['connections']['tr_password'], 't2-secret')
        self.assertEqual(settings['tr_labels'], ['archive', 'one', 'two'])
        self.assertEqual(settings['tr_proxy_source']['bt_proxy_url'], 'http://tp.invalid')
        self.assertEqual(self.settings['connections']['tr_password'], 'login-secret')

    def test_check_is_draft_only_and_generic_failure(self):
        before = copy.deepcopy(self.settings)
        self.assertTrue(self.registry.check({'id': 'tr', 'password': ''})['connected'])
        self.assertEqual(Probe.calls, [('tr', 'torrent-get')])
        self.assertEqual(self.settings, before)
        with patch.object(Probe, 'rpc', side_effect=RuntimeError('https://secret.invalid/login-secret')):
            failed = self.registry.check({'id': 'tr'})
        self.assertFalse(failed['connected'])
        self.assertNotIn('secret.invalid', failed['error'])
        self.assertNotIn('login-secret', failed['error'])

    def test_single_limits_do_not_touch_placeholder(self):
        values = self.registry.api('qb').limits()
        self.assertEqual(set(values), {'qb'})
        self.assertTrue(all(client == 'qb' for client, _ in Probe.calls))
        Probe.calls = []
        values = self.registry.api('tr').limits()
        self.assertEqual(set(values), {'tr'})
        self.assertAlmostEqual(values['tr']['upload_kib'], 10000 / 1024)
        self.assertEqual(Probe.calls, [('tr', 'session-get')])

    def test_single_api_rejects_other_client_before_read(self):
        with self.assertRaises(ManagementError):
            self.registry.api('qb').rpc('torrent-get')
        with self.assertRaises(ManagementError):
            self.registry.api('tr').qget('torrents/info')
        with self.assertRaises(ManagementError):
            self.registry.api('qb').wrapped.set_limits({'client': 'tr', 'upload_kib': 1, 'download_kib': 1}, only='qb')
        self.assertEqual(Probe.calls, [])


if __name__ == '__main__':
    unittest.main()
