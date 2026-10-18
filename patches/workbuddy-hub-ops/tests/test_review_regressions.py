"""Reproduce reviewed failure boundaries on the migrated baseline."""
import json
import io
import os
import subprocess
import threading
import time
import urllib.error
import unittest
from unittest import mock
from test_integration import IntegrationTests, wb, BODY, MODEL, Stream, turn
from test_installer import InstallerTests, installer


class ReviewTests(IntegrationTests):
    def test_first_frame_eof_after_deadline_is_not_success(self):
        clock=[0.0]
        class Eof:
            def __iter__(self):
                clock[0]=2.0
                return iter(())
            def close(self):
                pass
        with mock.patch.object(wb.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(wb.threading,'Timer'):
            with self.assertRaises(TimeoutError):
                wb.probe_first_frame(Eof(),deadline_at=1.0)


class InstallerReview(InstallerTests):
    def test_parent_junction_or_symlink_is_rejected(self):
        real=self.root/'real';real.mkdir()
        self.target.rename(real/'target');self.target=real/'target'
        alias=self.root/'alias'
        if os.name=='nt':
            subprocess.run(['cmd','/c','mklink','/J',str(alias),str(real)],check=True,capture_output=True)
        else:
            alias.symlink_to(real,target_is_directory=True)
        try:
            with self.assertRaises(ValueError):self.bundle_object().apply(alias/'target',dry_run=True)
        finally:
            if os.name=='nt':os.rmdir(alias)
            else:alias.unlink()

    def test_backup_revalidated_under_lock(self):
        from contextlib import contextmanager
        bundle=self.bundle_object();bundle.apply(self.target)
        before=self.original_sources()
        state=json.loads((self.target/installer.STATE).read_text())
        locking=installer.target_lock
        @contextmanager
        def intervene(target):
            with locking(target):
                (target/state['backup']/'wb_proxy.py').write_bytes(b'# damaged backup')
                yield
        with mock.patch.object(installer,'target_lock',intervene):
            with self.assertRaises(ValueError):bundle.rollback(self.target)
        self.assertEqual(before,self.original_sources())

    def test_rollback_state_delete_failure_restores_patched_files(self):
        bundle=self.bundle_object()
        bundle.apply(self.target)
        before=self.original_sources()
        unlink=installer.Path.unlink
        def fail_state(path,*args,**kwargs):
            if path.name==installer.STATE:
                raise OSError('synthetic state delete failure')
            return unlink(path,*args,**kwargs)
        with mock.patch.object(installer.Path,'unlink',fail_state):
            with self.assertRaises(OSError):bundle.rollback(self.target)
        self.assertEqual(before,self.original_sources())
        self.assertTrue((self.target/installer.STATE).exists())

    def test_concurrent_edit_after_preflight_is_not_overwritten(self):
        bundle=self.bundle_object()
        commit=bundle.commit
        def intervening(target,before,after,modes):
            (target/'wb_proxy.py').write_bytes(b'# concurrent user edit\n')
            return commit(target,before,after,modes)
        with mock.patch.object(bundle,'commit',side_effect=intervening):
            with self.assertRaises(RuntimeError):bundle.apply(self.target)
        self.assertEqual((self.target/'wb_proxy.py').read_bytes(),b'# concurrent user edit\n')
        self.assertFalse((self.target/installer.STATE).exists())


class RefreshReview(IntegrationTests):
    def account(self):
        return wb.wb_accounts.Account({'uid':'synthetic','realm':'intl','accessToken':'old-token','refreshToken':'refresh-token'})

    def test_refresh_rotation_is_shared_and_has_no_hidden_retries(self):
        account=self.account()
        with mock.patch.object(wb.wb_accounts,'http_json',return_value={'data':{'accessToken':'new-token'}}) as request:
            self.assertTrue(account.refresh_after_401('old-token',time.monotonic()+2))
            self.assertTrue(account.refresh_after_401('old-token',time.monotonic()+2))
            self.assertEqual(request.call_count,1)
            self.assertEqual(request.call_args.kwargs['retries'],1)
            self.assertGreater(request.call_args.kwargs['deadline_at'],time.monotonic())

    def test_lock_wait_respects_remaining_budget(self):
        account=self.account(); account._refresh_lock.acquire()
        try:
            started=time.monotonic()
            with mock.patch.object(account,'_refresh_locked') as refresh:
                self.assertFalse(account.refresh_after_401('old-token',started+.04))
                refresh.assert_not_called()
            self.assertLess(time.monotonic()-started,.5)
        finally:account._refresh_lock.release()

    def test_json_retry_does_not_reset_absolute_budget(self):
        clock=[0.0];calls=[]
        def fail(*args,**kwargs):
            calls.append(args[4]);clock[0]=args[4]
            raise urllib.error.URLError('synthetic transient')
        with mock.patch.object(wb.wb_accounts.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(wb.wb_accounts,'_deadline_json',side_effect=fail), \
             mock.patch.object(wb.wb_accounts,'_retryable',return_value=True):
            with self.assertRaises(TimeoutError):
                wb.wb_accounts.http_json('https://example.invalid',timeout=30,deadline_at=5)
        self.assertEqual(calls,[5])

    def invoke_401(self, *, repeated=False, reject_after_refresh=False, support=True):
        account=self.account();pool=mock.Mock();pool.count_ready.return_value=1;pool.accounts=[account]
        seen=[]
        def pick(**kwargs):
            seen.append(set(kwargs['exclude']))
            if account.uid in kwargs['exclude'] or (reject_after_refresh and len(seen)>1):return None
            return account
        pool.pick_for_session.side_effect=pick
        opened=[]
        def request(req,**kwargs):
            opened.append(req.get_header('Authorization'))
            if len(opened)==1 or repeated:
                raise urllib.error.HTTPError(req.full_url,401,'expired',{},io.BytesIO(b'expired'))
            return Stream(turn({'content':'answer'}))
        with mock.patch.object(wb,'POOL',pool), \
             mock.patch.object(wb,'auto_switch_product_enabled',return_value=False), \
             mock.patch.object(wb.wb_accounts,'urlopen',side_effect=request), \
             mock.patch.object(wb.wb_accounts,'http_json',return_value={'data':{'accessToken':'new-token'}}) as refreshed:
            if not support:account.refresh_after_401=None
            if repeated or reject_after_refresh or not support:
                with self.assertRaises((urllib.error.HTTPError,RuntimeError)) as caught:
                    wb.open_upstream(BODY,deadline_at=time.monotonic()+2)
                if isinstance(caught.exception,urllib.error.HTTPError):
                    self.assertEqual(caught.exception.code,401)
                else:self.assertIn('no usable account',str(caught.exception))
            else:
                stream,chosen,effort=wb.open_upstream(BODY,deadline_at=time.monotonic()+2)
                self.assertIs(chosen,account);stream.close()
            return opened,refreshed.call_count,seen

    def test_401_without_refresh_support_matches_rotation_failure(self):
        opened,count,_=self.invoke_401(support=False)
        self.assertEqual(opened,['Bearer old-token']);self.assertEqual(count,0)

    def test_401_refresh_rebuilds_headers_once(self):
        opened,count,_=self.invoke_401()
        self.assertEqual(opened,['Bearer old-token','Bearer new-token']);self.assertEqual(count,1)

    def test_repeated_401_does_not_refresh_forever(self):
        opened,count,_=self.invoke_401(repeated=True)
        self.assertEqual(len(opened),2);self.assertEqual(count,1)

    def test_refreshed_account_still_goes_through_admission(self):
        opened,count,seen=self.invoke_401(reject_after_refresh=True)
        self.assertEqual(len(opened),1);self.assertEqual(count,1);self.assertGreater(len(seen),1)


if __name__=='__main__':
    suite=unittest.TestSuite(c(n) for c in (ReviewTests,InstallerReview,RefreshReview) for n in c.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
