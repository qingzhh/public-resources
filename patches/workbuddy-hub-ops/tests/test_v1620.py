"""New upstream contracts, real HTTP keepalive and abort isolation."""
import http.server
import threading
import time
import unittest
import urllib.request
from unittest import mock
from test_integration import IntegrationTests, wb, BODY, MODEL, turn
from test_nullable_buffer import frames, NULL_SHAPES
import wb_upstream_pool as pool


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass


class Peer(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def do_GET(self):
        data = b''.join(frames(NULL_SHAPES[0], 128)) + b'\n' * 200000 if self.path == '/loop' else b''.join(turn({'content': 'answer'}))
        if self.path == '/json':data=b'{"answer":true}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.path == '/slow':
            self.server.release_slow.wait(3)
        self.wfile.write(data)
        self.wfile.flush()


class PoolContracts(IntegrationTests):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict('os.environ', {'WB_UPSTREAM_KEEPALIVE': '1'}))
        self.peer = Server(('127.0.0.1', 0), Peer)
        self.peer.release_slow = threading.Event()
        threading.Thread(target=self.peer.serve_forever, daemon=True).start()
        self.addCleanup(self.peer.server_close)
        self.addCleanup(self.peer.shutdown)
        self.addCleanup(self.peer.release_slow.set)
        self.url = 'http://127.0.0.1:%d' % self.peer.server_port

    def open(self, path='/good'):
        return wb.wb_accounts.urlopen(urllib.request.Request(self.url+path), timeout=2, proxy='')

    def test_completed_guard_returns_connection_and_late_close_is_harmless(self):
        first = self.open()
        conn = first._pool_conn
        guard = wb._GuardedStream(first, {'model': MODEL})
        self.assertIn(b'answer', b''.join(guard))
        guard.close()
        second = self.open()
        self.assertIs(conn, second._pool_conn)
        wb._interrupt_upstream(first)
        self.assertIn(b'answer', second.read())
        second.close()

    def test_loop_discards_unread_connection(self):
        first = self.open('/loop'); conn = first._pool_conn
        guard = wb._GuardedStream(first, {'model': MODEL})
        self.assertIn(b'reasoning_loop_detected', b''.join(guard))
        guard.close()
        second = self.open()
        self.assertIsNot(conn, second._pool_conn)
        self.assertIn(b'answer', second.read()); second.close()

    def test_blocked_read_deadline_discards_connection(self):
        first = self.open('/slow'); conn = first._pool_conn
        started = time.monotonic()
        guard = wb._GuardedStream(first, {'model': MODEL, 'generation_deadline': started+.1})
        self.assertIn(b'generation_timeout', b''.join(guard)); guard.close()
        self.assertLess(time.monotonic()-started, 1.5)
        second = self.open()
        self.assertIsNot(conn, second._pool_conn)
        self.assertIn(b'answer', second.read()); second.close()

    def test_refresh_watchdog_cancelled_before_pool_reuse(self):
        first=self.open('/json');conn=first._pool_conn
        first.read();first.close()
        with mock.patch.object(pool,'urlopen',side_effect=AssertionError('refresh must not use pool')):
            self.assertEqual(wb.wb_accounts.http_json(self.url+'/json',retries=1,deadline_at=time.monotonic()+.1),{'answer':True})
        second=self.open()
        self.assertIs(conn,second._pool_conn)
        time.sleep(.15)
        self.assertIn(b'answer',second.read());second.close()

    def test_refresh_slow_body_is_interrupted_without_touching_pool(self):
        first=self.open('/json');conn=first._pool_conn;first.read();first.close()
        start=time.monotonic()
        with mock.patch.object(pool,'urlopen',side_effect=AssertionError('refresh must not use pool')):
            with self.assertRaises((TimeoutError,OSError,ValueError)):
                wb.wb_accounts.http_json(self.url+'/slow',retries=1,deadline_at=start+.1)
        self.assertLess(time.monotonic()-start,1.5)
        second=self.open();self.assertIs(conn,second._pool_conn)
        self.assertIn(b'answer',second.read());second.close()


if __name__ == '__main__':
    suite = unittest.TestSuite(PoolContracts(name) for name in PoolContracts.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
