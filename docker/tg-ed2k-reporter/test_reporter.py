import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from ed2k import Message, Page
from reporter import Decision, MAX_RESPONSE_BYTES, MsReporter, ReportError, report_batch
from state import Store

MD4 = "0123456789abcdef0123456789abcdef"
NOW = 2_000_000.0
ITEM = {"name": "测试.2026.mkv", "size": 123, "md4": MD4}
SECRETS = {"ms_url": "http://127.0.0.1:8888", "ms_api_key": "test-local-key", "email": "test@example.invalid", "slogan": "test-cloud-secret", "driver_name": "115 Open"}
SETTINGS = {
    "channels": ["regeng115"], "initial_days": 7, "poll_seconds": 300,
    "max_pages_per_cycle": 30, "edit_pages": 2, "telegram_spacing_seconds": 1,
    "http_timeout_seconds": 25, "report_enabled": True, "max_reports_per_cycle": 20,
    "report_spacing_seconds": 5, "max_attempts": 6, "retry_base_seconds": 300,
    "retry_cap_seconds": 21600,
}


def success(data):
    return {"code": 20000, "message": "SUCCESS", "data": data}


def media(kind="movie", **metadata):
    return success({"tmdbMedia": {"id": 12345, "mediaType": kind}, "metadata": metadata})


def legacy_row(**changes):
    return {"id": 9, "sample_hash": f"ed2k:{MD4}:123", "file_size": 123, "driver_name": "115 Open", **changes}


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, payload, headers, **flags):
        self.calls.append({"url": url, "payload": dict(payload), "headers": dict(headers), **flags})
        if not self.responses:
            raise AssertionError("unexpected request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result()
        return result


def client(*responses):
    transport = Transport(*responses)
    return MsReporter(SECRETS, transport=transport), transport


class ProtocolTests(unittest.TestCase):
    def test_existing_skips_identification_and_write(self):
        reporter, transport = client(success({"status": "exists"}))
        result = reporter.process(ITEM)
        self.assertEqual(result.state, "existing")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertIn("query_by_unique", call["url"])
        self.assertEqual(call["payload"]["file_name"], ITEM["name"])
        self.assertEqual(call["payload"]["sample_hash"], f"ed2k:{MD4}:123")
        self.assertEqual(call["headers"]["X-Slogan"], SECRETS["slogan"])
        self.assertNotIn("Authorization", call["headers"])

    def test_created_and_updated_require_explicit_write_receipt(self):
        for status in ("created", "updated", "exists"):
            with self.subTest(status=status):
                reporter, transport = client(success({"status": "need_payload", "required_parts": ["media", "md4"]}), media(), success({"status": status}))
                result = reporter.process(ITEM)
                self.assertEqual(result.state, "existing" if status == "exists" else "reported")
                self.assertEqual(result.receipt, {"via": "report", "code": 20000, "status": status})
                local, report = transport.calls[1:]
                self.assertFalse(local["remote"])
                self.assertEqual(local["headers"]["Authorization"], "Bearer " + SECRETS["ms_api_key"])
                self.assertNotIn("X-Slogan", local["headers"])
                self.assertEqual(local["payload"], {"title": ITEM["name"]})
                self.assertTrue(report["write"])
                self.assertTrue(report["url"].endswith("/cs_hash/report"))
                self.assertEqual(report["payload"]["hash_info"], {"md4": MD4})
                self.assertEqual(report["payload"]["tmdb_id"], 12345)
                self.assertNotIn("season", report["payload"])

    def test_terminal_query_states_never_write(self):
        for status, state in (("blocked", "blocked"), ("ignored", "blocked"), ("conflict", "failed")):
            with self.subTest(status=status):
                reporter, transport = client(success({"status": status, "reason_code": "server-private-text"}))
                result = reporter.process(ITEM)
                self.assertEqual(result.state, state)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("server-private-text", json.dumps(result.receipt))

    def test_unknown_material_not_invented(self):
        reporter, transport = client(success({"status": "report_required", "required_parts": ["sha1"]}))
        with self.assertRaisesRegex(ReportError, "missing_hash_material"):
            reporter.process(ITEM)
        self.assertEqual(len(transport.calls), 1)

    def test_tv_season_and_episode(self):
        reporter, transport = client(success({"status": "report_required"}), media("tv", beginSeason=2, endSeason=2, beginEpisode=7, endEpisode=7), success({"status": "created"}))
        self.assertEqual(reporter.process(ITEM).state, "reported")
        self.assertEqual(transport.calls[-1]["payload"]["season"], 2)
        self.assertEqual(transport.calls[-1]["payload"]["episode"], 7)

    def test_tv_ranges_rejected_before_write(self):
        for metadata in ({"beginSeason": 1, "beginEpisode": 1, "endEpisode": 2}, {"beginSeason": 1, "beginEpisode": 1, "endSeason": 2}):
            reporter, transport = client(success({"status": "need_payload"}), media("tv", **metadata))
            with self.assertRaisesRegex(ReportError, "episode_range_unsupported"):
                reporter.process(ITEM)
            self.assertEqual(len(transport.calls), 2)

    def test_unrecognized_media_is_retryable(self):
        for data in (None, {}, {"tmdbMedia": {"id": 0, "mediaType": "movie"}, "metadata": {}}, {"tmdbMedia": {"id": True, "mediaType": "movie"}, "metadata": {}}):
            reporter, _ = client(success(data))
            with self.assertRaises(ReportError) as caught:
                reporter.analyze(ITEM)
            self.assertTrue(caught.exception.transient)

    def test_episode_missing_or_invalid(self):
        for metadata in ({}, {"beginSeason": 1, "beginEpisode": 0}, {"beginSeason": True, "beginEpisode": 1}):
            reporter, _ = client(media("tv", **metadata))
            with self.assertRaisesRegex(ReportError, "episode_unrecognized"):
                reporter.analyze(ITEM)

    def test_legacy_existing_checks_identity(self):
        reporter, transport = client(success(legacy_row()))
        self.assertEqual(reporter.process(ITEM).state, "existing")
        self.assertEqual(len(transport.calls), 1)
        for changes in ({"sample_hash": "different"}, {"file_size": 124}, {"driver_name": "other"}):
            reporter, _ = client(success(legacy_row(**changes)))
            with self.assertRaisesRegex(ReportError, "cloud_identity_conflict"):
                reporter.query(ITEM)

    def test_legacy_create_requires_readback(self):
        for missing in (None, {"id": 0}):
            reporter, transport = client(success(missing), media(), success(None), success(legacy_row()))
            self.assertEqual(reporter.process(ITEM).receipt["via"], "create_and_query")
            self.assertTrue(transport.calls[2]["url"].endswith("/cs_hash/create"))
            self.assertFalse(transport.calls[3]["write"])

    def test_legacy_unconfirmed_creation_is_uncertain(self):
        reporter, _ = client(success(None), media(), success(None), success(None))
        with self.assertRaises(ReportError) as caught:
            reporter.process(ITEM)
        self.assertTrue(caught.exception.uncertain)

    def test_invalid_query_shapes_retry_safely(self):
        for data in ([], "exists", {}, {"id": False}, {"status": "unknown"}, {"status": []}, {"status": "exists", "required_parts": None}):
            reporter, _ = client(success(data))
            with self.assertRaises(ReportError) as caught:
                reporter.query(ITEM)
            self.assertTrue(caught.exception.transient)

    def test_invalid_write_decision_is_uncertain(self):
        reporter, _ = client(success({"status": "need_payload"}), media(), success({"status": "unknown"}))
        with self.assertRaises(ReportError) as caught:
            reporter.process(ITEM)
        self.assertTrue(caught.exception.uncertain)

    def test_success_envelope_without_decision_not_accepted_for_report(self):
        reporter, _ = client(success({"status": "need_payload"}), media(), success(None))
        with self.assertRaises(ReportError) as caught:
            reporter.process(ITEM)
        self.assertTrue(caught.exception.uncertain)

    def test_only_exact_integer_success_code_accepted(self):
        for code in (0, 200, "20000", True, None):
            reporter, _ = client({"code": code, "data": {"status": "exists"}})
            with self.assertRaises(ReportError):
                reporter.query(ITEM)

    def test_business_errors_classified_and_redacted(self):
        for code, text, auth, transient in ((40001, "认证失败 " + SECRETS["slogan"], True, False), (50000, SECRETS["email"], False, True), (40003, SECRETS["ms_api_key"], False, False)):
            reporter, _ = client({"code": code, "message": text})
            with self.assertRaises(ReportError) as caught:
                reporter.query(ITEM)
            self.assertEqual(caught.exception.auth, auth)
            self.assertEqual(caught.exception.transient, transient)
            for value in SECRETS.values():
                self.assertNotIn(value, str(caught.exception))

    def test_shutdown_after_analysis_never_writes(self):
        stop = threading.Event()
        def identify():
            stop.set()
            return media()
        reporter, transport = client(success({"status": "need_payload"}), identify)
        with self.assertRaises(InterruptedError):
            reporter.process(ITEM, stop=stop)
        self.assertEqual(len(transport.calls), 2)

    def test_credentials_and_endpoint_validation(self):
        for changes in ({"email": "bad\nheader"}, {"ms_url": "http://user:password@localhost"}, {"cloud_url": "http://example.invalid"}, {"cloud_url": "https://example.invalid/?private=1"}, {"extra": "invalid"}):
            with self.assertRaises(ReportError):
                MsReporter({**SECRETS, **changes})


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.server.calls.append({"path": self.path, "headers": dict(self.headers), "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                code, raw, headers = self.server.responses.pop(0)
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.server.calls, self.server.responses = [], []
        self.reporter = MsReporter({**SECRETS, "ms_url": self.url, "cloud_url": self.url}, timeout=1)

    def queue(self, code=200, body=None, raw=None, **headers):
        self.server.responses.append((code, raw if raw is not None else json.dumps(body).encode(), headers))

    def test_real_http_post_json_and_local_proxy_bypass(self):
        self.queue(body=media())
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            reporter = MsReporter({**SECRETS, "ms_url": self.url}, proxy="http://127.0.0.1:1", timeout=1)
            self.assertEqual(reporter.analyze(ITEM)["tmdb_id"], 12345)
        self.assertEqual(self.server.calls[0]["payload"], {"title": ITEM["name"]})

    def test_real_cloud_post_with_auth_headers(self):
        self.queue(body=success({"status": "exists"}))
        self.assertEqual(self.reporter.query(ITEM).status, "exists")
        headers = {key.lower(): value for key, value in self.server.calls[0]["headers"].items()}
        self.assertEqual(headers["x-email"], SECRETS["email"])
        self.assertNotIn("authorization", headers)

    def test_auth_http_codes_pause(self):
        for code in (401, 403):
            self.queue(code=code, body={"private": SECRETS["slogan"]})
            with self.assertRaises(ReportError) as caught:
                self.reporter.query(ITEM)
            self.assertTrue(caught.exception.auth)
            self.assertNotIn(SECRETS["slogan"], str(caught.exception))

    def test_retryable_http_and_write_uncertainty(self):
        for code in (429, 500, 502, 503):
            self.queue(code=code, body={})
            with self.assertRaises(ReportError) as caught:
                self.reporter.request("/report", {}, write=True)
            self.assertTrue(caught.exception.transient)
            self.assertTrue(caught.exception.uncertain)

    def test_redirect_not_followed(self):
        self.queue(code=302, body={}, Location=self.url + "/credential-destination")
        with self.assertRaisesRegex(ReportError, "cloud_http_302"):
            self.reporter.query(ITEM)
        self.assertEqual(len(self.server.calls), 1)

    def test_non_json_and_oversized_write_replies_are_uncertain(self):
        for raw in (b"<html>private error</html>", b"x" * (MAX_RESPONSE_BYTES + 1)):
            self.queue(raw=raw)
            with self.assertRaises(ReportError) as caught:
                self.reporter.request("/report", {}, write=True)
            self.assertTrue(caught.exception.uncertain)
            self.assertNotIn("private error", str(caught.exception))

    def test_network_failure_is_sanitized(self):
        reporter = MsReporter({**SECRETS, "cloud_url": "http://127.0.0.1:1"}, timeout=1)
        with self.assertRaisesRegex(ReportError, "cloud_network_error") as caught:
            reporter.request("/report", {}, write=True)
        self.assertTrue(caught.exception.uncertain)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)
        self.now = NOW
        self.settings = {**SETTINGS, "report_spacing_seconds": 0}
        self.add(1)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, ident):
        return self.store.ingest([Message("regeng115", ident, NOW, f"ed2k://|file|{ident}.mkv|123|{ident:032x}|/")], NOW)

    def row(self, ident=1):
        return dict(self.store.db.execute("SELECT * FROM items WHERE md4=?", (f"{ident:032x}",)).fetchone())

    def batch(self, reporter, **kwargs):
        return report_batch(self.store, reporter, self.settings, clock=lambda: self.now, sleep=lambda _: None, **kwargs)

    def uncertain(self, attempts=1):
        item = self.store.due(self.now, 1)[0]
        self.store.begin(item, self.now)
        self.store.recover(self.now)
        with self.store.db:
            self.store.db.execute("UPDATE items SET attempts=?", (attempts,))

    def test_success_persisted_and_duplicate_not_written_again(self):
        reporter, transport = client(success({"status": "need_payload"}), media(), success({"status": "created"}))
        result = self.batch(reporter)
        self.assertEqual(result["reported"], 1)
        self.assertEqual(self.row()["state"], "reported")
        self.add(1)
        self.batch(reporter)
        self.assertEqual(len(transport.calls), 3)

    def test_transient_failure_exponential_backoff_then_limit(self):
        self.settings["max_attempts"] = 3
        for attempt in range(1, 4):
            reporter, _ = client(ReportError("cloud_network_error", transient=True))
            self.batch(reporter)
            row = self.row()
            self.assertEqual(row["attempts"], attempt)
            self.assertEqual(row["state"], "failed" if attempt == 3 else "retry")
            self.assertEqual(row["next_retry"], self.now + 300 * 2 ** (attempt - 1))
            self.assertFalse(self.store.due(row["next_retry"] - 1, 1))
            self.now = row["next_retry"]

    def test_write_timeout_recovers_by_query_without_second_write(self):
        reporter, transport = client(success({"status": "need_payload"}), media(), ReportError("cloud_network_error", transient=True, uncertain=True), success({"status": "exists"}))
        self.assertEqual(self.batch(reporter)["uncertain"], 1)
        self.assertEqual(self.row()["state"], "uncertain")
        self.assertEqual(self.batch(reporter)["rechecked"], 0)
        self.now += 300
        result = self.batch(reporter)
        self.assertEqual(result["rechecked"], 1)
        self.assertEqual(result["reported"], 0)
        self.assertEqual(self.row()["state"], "existing")
        self.assertEqual(sum(call["write"] for call in transport.calls), 1)
        self.assertEqual(self.row()["attempts"], 1)

    def test_crash_recovery_only_requeues_after_negative_query(self):
        self.uncertain()
        reporter, transport = client(success({"status": "need_payload"}))
        result = self.batch(reporter)
        self.assertEqual(result["retry"], 1)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.row()["next_retry"], self.now + 300)
        self.assertFalse(any(call["write"] for call in transport.calls))

    def test_uncertain_at_attempt_limit_not_requeued(self):
        self.uncertain(attempts=self.settings["max_attempts"])
        reporter, _ = client(success({"status": "need_payload"}))
        self.assertEqual(self.batch(reporter)["failed"], 1)
        self.assertEqual(self.row()["last_error"], "unconfirmed_request")

    def test_recheck_failure_stays_uncertain_and_waits(self):
        self.uncertain()
        reporter, transport = client(ReportError("cloud_network_error", transient=True))
        self.assertEqual(self.batch(reporter)["uncertain"], 1)
        self.assertEqual(self.row()["state"], "uncertain")
        self.assertEqual(self.row()["next_retry"], self.now + self.settings["retry_cap_seconds"])
        self.batch(reporter)
        self.assertEqual(len(transport.calls), 1)

    def test_auth_pauses_batch_and_explicit_retry_resumes(self):
        self.add(2)
        reporter, transport = client({"code": 40001, "message": "认证失败 " + SECRETS["slogan"]})
        self.assertTrue(self.batch(reporter)["paused"])
        self.assertEqual(self.row()["state"], "blocked")
        self.batch(reporter)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn(SECRETS["slogan"], json.dumps(self.store.status()))
        self.assertEqual(self.store.retry_failed(self.now), 1)
        self.assertIsNone(self.store.get("report_pause"))
        reporter, _ = client(success({"status": "exists"}), success({"status": "exists"}))
        self.assertEqual(self.batch(reporter)["existing"], 2)

    def test_auth_during_recovery_preserves_uncertain(self):
        self.uncertain()
        reporter, _ = client(ReportError("cloud_auth_failed", auth=True))
        result = self.batch(reporter)
        self.assertTrue(result["paused"])
        self.assertEqual(result["rechecked"], 1)
        self.assertEqual(self.row()["state"], "uncertain")

    def test_limit_includes_readbacks(self):
        self.uncertain()
        self.add(2)
        reporter, transport = client(success({"status": "exists"}))
        result = self.batch(reporter, limit=1)
        self.assertEqual(result["rechecked"], 1)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(len(transport.calls), 1)

    def test_permanent_failure_and_conflict_are_not_retried(self):
        for response in (success({"status": "conflict"}), {"code": 40003, "message": "bad request"}):
            with self.store.db:
                self.store.db.execute("UPDATE items SET state='pending'")
            reporter, _ = client(response)
            self.assertEqual(self.batch(reporter)["failed"], 1)
            self.assertFalse(self.store.due(self.now + 999999, 1))

    def test_shutdown_before_write_requeues(self):
        stop = threading.Event()
        def identify():
            stop.set()
            return media()
        reporter, transport = client(success({"status": "need_payload"}), identify)
        self.assertEqual(self.batch(reporter, stop=stop)["retry"], 1)
        self.assertEqual(self.row()["last_error"], "shutdown_before_write")
        self.assertFalse(any(call["write"] for call in transport.calls))


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(app.__file__)), "--data", str(self.directory / "data"), *args], capture_output=True, text=True, encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_actual_normalize_import_status_and_health_commands(self):
        incoming, output = self.directory / "input.txt", self.directory / "output.txt"
        incoming.write_text(f"失败: ed2k://|file|中文.mkv|123|{MD4}| err=结构错误", encoding="utf-8-sig")
        clean = self.cli("normalize", "--input", str(incoming), "--output", str(output))
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), f"ed2k://|file|中文.mkv|123|{MD4}|/\n")
        self.assertEqual(self.cli("import", "--input", str(incoming)).returncode, 0)
        second = self.cli("import", "--input", str(incoming))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(json.loads(second.stdout)["new"], 0)
        status = self.cli("status")
        self.assertEqual(status.returncode, 0)
        self.assertEqual(json.loads(status.stdout)["total_unique"], 1)
        self.assertEqual(self.cli("health").returncode, 1)
        store = Store(self.directory / "data")
        store.set("heartbeat", time.time())
        store.close()
        self.assertEqual(self.cli("health").returncode, 0)

    def test_invalid_import_encoding_is_sanitized(self):
        incoming = self.directory / "bad.txt"
        incoming.write_bytes(b"\xff\xfeprivate-value")
        result = self.cli("import", "--input", str(incoming))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-value", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_input_size_limit(self):
        incoming = self.directory / "large.txt"
        incoming.write_bytes(b"a" * 11)
        with patch.object(app, "MAX_INPUT_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "input_too_large"):
                app.read_input(incoming)

    def test_bad_settings_rejected(self):
        settings_path = self.directory / "settings.json"
        for changes in ({"channels": [[]]}, {"channels": ["regeng115", "regeng115"]}, {"poll_seconds": True}, {"report_enabled": "yes"}, {"retry_cap_seconds": 60}):
            settings_path.write_text(json.dumps({**SETTINGS, **changes}))
            with self.assertRaises(app.ConfigError):
                app.load_settings(settings_path)

    def test_only_changed_reporting_credentials_unpause(self):
        store = Store(self.directory / "data")
        try:
            app.Service(store, SETTINGS, SECRETS, "http://127.0.0.1:1")
            store.set("report_pause", {"reason": "cloud_auth_failed"})
            app.Service(store, {**SETTINGS, "report_enabled": False}, {}, "http://127.0.0.1:1")
            self.assertIsNotNone(store.get("report_pause"))
            app.Service(store, SETTINGS, SECRETS, "http://127.0.0.1:1")
            self.assertIsNotNone(store.get("report_pause"))
            app.Service(store, SETTINGS, {**SECRETS, "slogan": "replacement-test-secret"}, "http://127.0.0.1:1")
            self.assertIsNone(store.get("report_pause"))
        finally:
            store.close()

    def test_service_cycle_collects_and_reports_with_limit(self):
        store = Store(self.directory / "data")
        try:
            service = app.Service(store, {**SETTINGS, "edit_pages": 0}, SECRETS, "http://127.0.0.1:1")
            source = Message("regeng115", 1, time.time(), f"ed2k://|file|示例.mkv|123|{MD4}|/")
            service.telegram.fetch = lambda *_, **kw: Page((source,), None)
            service.reporter, transport = client(success({"status": "exists"}))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                summary = service.cycle(report_limit=1)
            self.assertEqual(summary["report"]["existing"], 1)
            self.assertEqual(store.status()["total_unique"], 1)
            self.assertTrue((store.directory / "normalized.txt").exists())
            self.assertEqual(len(transport.calls), 1)
            for value in SECRETS.values():
                self.assertNotIn(value, output.getvalue())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
