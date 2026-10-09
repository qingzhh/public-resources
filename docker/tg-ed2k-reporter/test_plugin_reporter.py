from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import catalog
from ed2k import Message
from plugin_reporter import PluginBatch
from reporter import Decision, MsReporter, ReportError
from state import Store

NOW = 2_000_000.0
SETTINGS = {
    "max_reports_per_cycle": 20, "max_attempts": 3,
    "retry_base_seconds": 30, "retry_cap_seconds": 120,
    "ms_plugin": {"check_seconds": 15, "timeout_seconds": 90},
}
SUMMARY_KEYS = {"id", "instance_id", "phase", "total", "confirmed", "failed", "started_at", "updated_at", "next_check_at", "error"}
NEEDS = Decision("need_payload", ("media", "md4"))


class Crash(BaseException):
    """Simulate abrupt process death without the normal exception cleanup."""


class Cloud:
    def __init__(self):
        self.calls, self.responses = [], {}
        self.default = NEEDS

    def query(self, item):
        ident = int(item["md4"], 16)
        self.calls.append(ident)
        result = self.responses.get(ident, self.default)
        if isinstance(result, BaseException):
            raise result
        return result(item) if callable(result) else result

    def process(self, *args, **kwargs):
        raise AssertionError("only cloud.query is authorized")

    report = create = process


class Plugin:
    def __init__(self):
        self.runs, self.polls = 0, 0
        self.state, self.result, self.callback = False, {"completed": False}, None

    def running(self):
        self.polls += 1
        if isinstance(self.state, BaseException):
            raise self.state
        return self.state() if callable(self.state) else self.state

    def run(self):
        self.runs += 1
        if self.callback is not None:
            self.callback()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class TestFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("PI_SCRATCH_DIR"))
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory / "data")
        self.addCleanup(self.store.close)
        self.queue = self.directory / "queue" / "normalized.txt"
        self.queue.parent.mkdir(mode=0o700)
        self.now = NOW
        self.settings = {**SETTINGS, "ms_plugin": dict(SETTINGS["ms_plugin"])}
        self.cloud, self.plugin = Cloud(), Plugin()
        self.batch = self.client()

    def client(self, **kwargs):
        return PluginBatch(self.store, self.cloud, self.plugin, self.settings, self.queue, 99, clock=lambda: self.now, **kwargs)

    def add(self, ident, state="pending", *, attempts=0, next_retry=0):
        self.store.ingest([Message("regeng115", ident, self.now, f"ed2k://|file|{ident}.mkv|123|{ident:032x}|/")], self.now)
        if state != "pending" or attempts or next_retry:
            with self.store.db:
                self.store.db.execute("UPDATE items SET state=?,attempts=?,next_retry=? WHERE md4=?", (state, attempts, next_retry, f"{ident:032x}"))
        return self.row(ident)

    def row(self, ident=1):
        return dict(self.store.db.execute("SELECT * FROM items WHERE md4=?", (f"{ident:032x}",)).fetchone())

    def text(self):
        return self.queue.read_text(encoding="utf-8")

    def lines(self):
        return self.text().splitlines()

    def advance(self, seconds=15):
        self.now += seconds

    def start_batch(self, *idents):
        for ident in idents or (1,):
            self.add(ident)
        result = self.batch.tick()
        self.assertEqual(self.plugin.runs, 1)
        return result

    def recover(self):
        self.store.recover(self.now)
        self.batch = self.client()


class QueueTests(TestFixture):
    def test_finished_history_is_not_published_or_reuploaded(self):
        self.add(1, "reported")
        self.add(2, "existing")
        self.assertEqual(self.batch.tick()["processed"], 0)
        self.assertEqual(self.text(), "")
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.plugin.runs, 0)
        self.advance()
        self.assertFalse(self.batch.tick()["changed"])

    def test_prequery_exists_does_not_run_and_history_export_stays_complete(self):
        self.add(1)
        self.cloud.responses[1] = Decision("exists")
        result = self.batch.tick()
        self.assertEqual(result["existing"], 1)
        self.assertEqual(self.row()["state"], "existing")
        self.assertEqual(self.text(), "")
        self.assertEqual(self.plugin.runs, 0)
        destination = self.directory / "data" / "normalized.txt"
        self.store.export(destination)
        self.assertIn(self.row()["normalized"], destination.read_text(encoding="utf-8"))
        self.batch.tick()
        self.assertEqual(self.text(), "")
        self.assertEqual(self.cloud.calls, [1])

    def test_only_selected_due_items_are_active_and_all_failures_stay_commented(self):
        selected = self.add(1)
        pending = self.add(2)
        failed = self.add(3, "failed")
        blocked = self.add(4, "blocked")
        cooling = self.add(5, "retry", next_retry=self.now + 100)
        uncertain = self.add(6, "uncertain", next_retry=self.now + 100)
        self.add(7, "existing")
        result = self.batch.tick(limit=1)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(self.lines(), [selected["normalized"], "# pending " + pending["normalized"],
                                      "# failed " + failed["normalized"], "# blocked " + blocked["normalized"],
                                      "# retry " + cooling["normalized"], "# uncertain " + uncertain["normalized"]])
        journal = self.store.get("plugin_batch")
        self.assertRegex(journal["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(journal["keys"], [[selected["md4"], 123]])
        self.assertEqual(journal["checksum"], hashlib.sha256(self.queue.read_bytes()).hexdigest())
        self.assertEqual(set(result["plugin"]), SUMMARY_KEYS)
        self.assertEqual(set(self.store.get("plugin_status")), SUMMARY_KEYS)
        if os.name != "nt":
            self.assertEqual(self.queue.stat().st_mode & 0o777, 0o600)

    def test_idle_rebuild_is_all_comments_and_never_an_implicit_manual_batch(self):
        pending, failed = self.add(1), self.add(2, "failed")
        self.batch.tick(start=False)
        self.assertEqual(self.lines(), ["# pending " + pending["normalized"], "# failed " + failed["normalized"]])
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.plugin.runs, 0)

    def test_dispatch_is_journaled_and_items_and_checksum_persist_before_run(self):
        self.add(1)
        def before_run():
            reader = Store(self.directory / "data", readonly=True)
            try:
                journal = reader.get("plugin_batch")
                self.assertEqual(journal["phase"], "dispatching")
                self.assertTrue(journal["published"] and journal["submitted"])
                self.assertEqual(journal["checksum"], hashlib.sha256(self.queue.read_bytes()).hexdigest())
                self.assertEqual(reader.db.execute("SELECT state FROM items").fetchone()[0], "inflight")
            finally:
                reader.close()
        self.plugin.callback = before_run
        self.batch.tick()

    def test_new_input_waits_while_file_is_frozen(self):
        self.start_batch(1)
        frozen = self.queue.read_bytes()
        self.add(2)
        self.plugin.state = True
        self.advance()
        self.batch.tick()
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertEqual(self.row(2)["state"], "pending")
        self.assertNotIn(2, self.cloud.calls)
        self.assertEqual(self.plugin.runs, 1)
        self.cloud.responses[1] = Decision("exists")
        self.plugin.state = False
        self.advance()
        self.batch.tick()
        self.assertEqual(self.lines(), ["# pending " + self.row(2)["normalized"]])
        self.assertEqual(self.plugin.runs, 1)
        self.batch.tick()
        self.assertEqual(self.plugin.runs, 2)
        self.assertEqual(self.lines(), [self.row(2)["normalized"]])

    def test_checksum_change_pauses_before_any_network_and_preserves_foreign_file(self):
        self.start_batch()
        self.queue.write_text("external content\n", encoding="utf-8")
        calls, polls = len(self.cloud.calls), self.plugin.polls
        self.advance()
        result = self.batch.tick()
        self.assertTrue(result["paused"])
        self.assertEqual(self.store.get("report_pause")["reason"], "plugin_queue_changed")
        self.assertEqual(result["plugin"]["error"], "plugin_queue_changed")
        self.assertEqual(self.text(), "external content\n")
        self.assertEqual((len(self.cloud.calls), self.plugin.polls, self.plugin.runs), (calls, polls, 1))
        self.recover()
        self.batch.tick()
        self.assertEqual(self.text(), "external content\n")

    def test_missing_queue_directory_is_not_silently_created(self):
        self.queue.parent.rmdir()
        self.add(1)
        result = self.batch.tick()
        self.assertTrue(result["paused"])
        self.assertEqual(self.store.get("report_pause")["reason"], "plugin_queue_io_error")
        self.assertFalse(self.queue.parent.exists())
        self.assertEqual(self.plugin.runs, 0)


class ConfirmationTests(TestFixture):
    def test_accepted_is_not_completed(self):
        result = self.start_batch()
        self.assertEqual(result["reported"], 0)
        self.assertEqual(self.row()["state"], "inflight")
        self.assertIsNone(self.row()["receipt"])
        frozen = self.queue.read_bytes()
        calls, polls = len(self.cloud.calls), self.plugin.polls
        self.batch.tick()
        self.assertEqual((len(self.cloud.calls), self.plugin.polls), (calls, polls))
        self.assertEqual(self.queue.read_bytes(), frozen)

    def test_authoritative_query_receipt_catalog_and_rebuild_do_not_reintroduce_success(self):
        self.start_batch()
        ident = self.store.get("plugin_batch")["id"]
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        result = self.batch.tick()
        self.assertEqual(result["reported"], 1)
        self.assertEqual(self.row()["state"], "reported")
        self.assertEqual(json.loads(self.row()["receipt"]), {"via": "ms_plugin_query", "code": 20000, "status": "confirmed", "batch_id": ident})
        visible = catalog(self.store, state="reported")
        self.assertEqual(visible["total"], 1)
        self.assertEqual(visible["items"][0]["receipt"]["status"], "confirmed")
        self.assertEqual(self.text(), "")
        self.assertIsNone(self.store.get("plugin_batch"))
        last = self.store.get("plugin_last_batch")
        self.assertEqual(set(last), SUMMARY_KEYS)
        self.assertEqual((last["id"], last["phase"], last["confirmed"]), (ident, "completed", 1))
        self.add(1)
        self.batch.tick(start=False)
        self.assertEqual(self.text(), "")
        self.assertEqual(self.plugin.runs, 1)
        self.assertEqual(self.cloud.calls, [1, 1])

    def test_confirmed_hashes_do_not_retire_file_when_running_or_unknown(self):
        for state in (True, None):
            with self.subTest(state=state):
                self.setUp()
                self.start_batch()
                frozen = self.queue.read_bytes()
                self.add(2)
                self.plugin.state = state
                self.cloud.responses[1] = Decision("exists")
                self.advance()
                self.batch.tick()
                self.assertEqual(self.row()["state"], "reported")
                self.assertEqual(self.queue.read_bytes(), frozen)
                self.assertIsNotNone(self.store.get("plugin_batch"))
                self.advance(1000)
                self.batch.tick()
                self.assertEqual(self.queue.read_bytes(), frozen)
                self.assertEqual(self.plugin.runs, 1)
                self.assertEqual(self.row(2)["state"], "pending")
                self.plugin.state = False
                self.advance()
                self.batch.tick(start=False)
                self.assertEqual(self.lines(), ["# pending " + self.row(2)["normalized"]])

    def test_run_completed_is_authoritative_but_still_requires_cloud_query(self):
        self.plugin.result = {"completed": True}
        self.start_batch()
        self.plugin.state = None
        polls = self.plugin.polls
        self.assertEqual(self.row()["state"], "inflight")
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        result = self.batch.tick()
        self.assertEqual(result["reported"], 1)
        self.assertEqual(self.plugin.polls, polls)
        self.assertEqual(self.text(), "")

    def test_manual_pause_can_confirm_active_batch_without_dispatching_another(self):
        self.start_batch()
        self.add(2)
        self.store.set("report_pause", {"reason": "manual", "at": self.now})
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        result = self.batch.tick(start=False)
        self.assertTrue(result["paused"])
        self.assertEqual(result["reported"], 1)
        self.assertEqual(self.lines(), ["# pending " + self.row(2)["normalized"]])
        self.batch.tick()
        self.assertEqual(self.plugin.runs, 1)
        self.assertNotIn(2, self.cloud.calls)

    def test_stop_does_no_network_and_does_not_change_active_file(self):
        self.start_batch()
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        frozen, calls, polls = self.queue.read_bytes(), len(self.cloud.calls), self.plugin.polls
        stop = threading.Event()
        stop.set()
        self.batch.tick(stop=stop)
        self.assertEqual((len(self.cloud.calls), self.plugin.polls, self.plugin.runs), (calls, polls, 1))
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertEqual(self.row()["state"], "inflight")

    def test_readback_budget_includes_orphan_queries_and_fairly_checks_active_batch(self):
        self.add(1, "uncertain", attempts=1)
        self.add(2)
        self.cloud.responses[1] = Decision("exists")
        result = self.batch.tick(limit=1)
        self.assertEqual((result["rechecked"], result["processed"]), (1, 0))
        self.assertEqual(self.cloud.calls, [1])
        self.add(3)
        self.batch.tick()
        self.plugin.state = True
        self.advance()
        before = len(self.cloud.calls)
        self.batch.tick(limit=1)
        self.assertEqual(len(self.cloud.calls) - before, 1)
        self.advance()
        self.batch.tick(limit=1)
        self.assertEqual(self.cloud.calls[-2:], [2, 3])


class FailureTests(TestFixture):
    def test_timeout_does_not_guess_idle_and_retry_is_bounded_with_backoff(self):
        self.start_batch()
        frozen = self.queue.read_bytes()
        self.plugin.state = True
        self.advance(91)
        self.batch.tick()
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertEqual(self.store.get("plugin_batch")["error"], "plugin_timeout")
        self.plugin.state = False
        self.advance()
        result = self.batch.tick()
        self.assertEqual(result["retry"], 1)
        self.assertEqual(self.row()["next_retry"], self.now + 30)
        self.assertEqual(self.lines(), ["# retry " + self.row()["normalized"]])
        self.assertEqual(self.plugin.runs, 1)
        self.batch.tick()
        self.assertEqual(self.plugin.runs, 1)
        for attempt in (2, 3):
            self.now = self.row()["next_retry"]
            self.plugin.result = ReportError("private API response", transient=True)
            self.batch.tick()
            self.assertEqual(self.plugin.runs, attempt)
            self.advance()
            result = self.batch.tick()
            expected = "retry" if attempt < 3 else "failed"
            self.assertEqual(self.row()["state"], expected)
            self.assertEqual(self.row()["attempts"], attempt)
            self.assertEqual(self.row()["next_retry"], self.now + min(120, 30 * 2 ** (attempt - 1)))
            self.assertEqual(self.lines(), [f"# {expected} " + self.row()["normalized"]])
        self.advance(10000)
        self.batch.tick()
        self.assertEqual(self.plugin.runs, 3)
        self.assertNotIn("private API response", json.dumps(self.store.get("plugin_last_batch")))

    def test_trigger_network_timeout_readbacks_without_blind_resubmission(self):
        self.plugin.result = ReportError("private timeout/path", transient=True, uncertain=True)
        result = self.start_batch()
        self.assertEqual(result["uncertain"], 1)
        self.assertEqual(self.row()["state"], "uncertain")
        frozen = self.queue.read_bytes()
        self.plugin.state = None
        self.advance()
        self.batch.tick()
        self.assertEqual(self.plugin.runs, 1)
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.cloud.responses[1] = Decision("exists")
        self.plugin.state = False
        self.advance()
        self.assertEqual(self.batch.tick()["reported"], 1)
        self.assertEqual(self.plugin.runs, 1)
        self.assertEqual(self.text(), "")
        self.assertNotIn("private timeout/path", json.dumps(self.store.get("plugin_last_batch")))

    def test_interrupted_dispatch_is_not_treated_as_a_certain_unsent_call(self):
        self.plugin.result = InterruptedError("private interruption")
        self.start_batch()
        self.assertEqual(self.row()["state"], "uncertain")
        self.recover()
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        self.batch.tick()
        self.assertEqual(self.row()["state"], "reported")
        self.assertEqual(self.plugin.runs, 1)

    def test_auth_failure_uses_fixed_pause_and_does_not_save_raw_error(self):
        self.add(1)
        self.cloud.responses[1] = ReportError("secret/filename/auth", auth=True)
        result = self.batch.tick()
        self.assertTrue(result["paused"])
        self.assertEqual(self.row()["state"], "blocked")
        self.assertEqual(self.row()["last_error"], "auth_failed")
        self.assertEqual(self.store.get("report_pause")["reason"], "auth_failed")
        calls, polls = len(self.cloud.calls), self.plugin.polls
        self.batch.tick()
        self.assertEqual((len(self.cloud.calls), self.plugin.polls), (calls, polls))
        self.assertEqual(self.plugin.runs, 0)
        self.assertNotIn("secret/filename/auth", json.dumps(result))

    def test_active_query_error_keeps_uncertain_with_backoff_and_does_not_mutate_txt(self):
        self.start_batch()
        self.cloud.responses[1] = ReportError("private cloud error", transient=True)
        frozen = self.queue.read_bytes()
        self.advance()
        self.batch.tick()
        self.assertEqual(self.row()["state"], "uncertain")
        self.assertEqual(self.row()["next_retry"], self.now + 30)
        self.assertEqual(self.queue.read_bytes(), frozen)
        calls, polls = len(self.cloud.calls), self.plugin.polls
        self.advance()
        self.batch.tick()
        self.assertEqual((len(self.cloud.calls), self.plugin.polls), (calls, polls))
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        self.batch.tick()
        self.assertEqual(self.row()["state"], "reported")
        self.assertEqual(self.text(), "")

    def test_running_unknown_without_batch_neither_publishes_nor_triggers(self):
        self.add(1)
        self.batch.tick(start=False)
        frozen = self.queue.read_bytes()
        self.add(2)
        for state in (True, None):
            self.plugin.state = state
            self.batch.tick()
            self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.plugin.runs, 0)


class RecoveryTests(TestFixture):
    def test_crash_dispatch_journal_survives_recover_and_queries_before_any_second_run(self):
        self.plugin.result = Crash()
        self.add(1)
        with self.assertRaises(Crash):
            self.batch.tick()
        self.assertEqual(self.store.get("plugin_batch")["phase"], "dispatching")
        frozen = self.queue.read_bytes()
        self.recover()
        self.assertEqual(self.row()["state"], "uncertain")
        self.plugin.result, self.plugin.state = {"completed": False}, None
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        self.batch.tick()
        self.assertEqual(self.row()["state"], "reported")
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertEqual(self.plugin.runs, 1)
        self.plugin.state = False
        self.advance()
        self.batch.tick()
        self.assertEqual(self.text(), "")

    def test_orphan_recovery_exists_is_existing_and_negative_waits_for_explicit_idle(self):
        self.add(1, "uncertain", attempts=1)
        self.add(2, "uncertain", attempts=1)
        self.cloud.responses[1] = Decision("exists")
        self.plugin.state = None
        result = self.batch.tick()
        self.assertEqual(result["existing"], 1)
        self.assertEqual(self.row(1)["state"], "existing")
        self.assertEqual(self.row(2)["state"], "uncertain")
        self.assertEqual(self.row(2)["next_retry"], self.now + 30)
        self.plugin.state = False
        self.advance(30)
        result = self.batch.tick()
        self.assertEqual(result["retry"], 1)
        self.assertEqual(self.row(2)["state"], "retry")
        self.assertEqual(self.row(2)["next_retry"], self.now + 30)
        self.assertEqual(self.plugin.runs, 0)
        self.assertEqual(self.lines(), ["# retry " + self.row(2)["normalized"]])

    def test_prepared_crash_before_and_after_publication_withdraws_only_when_idle(self):
        for after_rename in (False, True):
            with self.subTest(after_rename=after_rename):
                self.setUp()
                self.add(1)
                original = self.batch._write
                def crash_write(content, expected):
                    if after_rename:
                        original(content, expected)
                    raise Crash()
                with patch.object(self.batch, "_write", side_effect=crash_write):
                    with self.assertRaises(Crash):
                        self.batch.tick()
                self.assertEqual(self.store.get("plugin_batch")["phase"], "prepared")
                self.recover()
                self.plugin.state = None
                self.batch.tick()
                self.assertEqual(self.row()["state"], "uncertain")
                self.assertIsNotNone(self.store.get("plugin_batch"))
                self.plugin.state = False
                self.advance()
                self.batch.tick()
                self.assertEqual(self.row()["state"], "retry")
                self.assertEqual(self.lines(), ["# retry " + self.row()["normalized"]])
                self.assertEqual(self.plugin.runs, 0)
                self.assertEqual(self.store.get("plugin_last_batch")["phase"], "withdrawn")

    def test_closing_rename_crash_can_retire_without_reuploading(self):
        self.start_batch()
        self.cloud.responses[1] = Decision("exists")
        self.advance()
        original = self.batch._write
        def crash_write(content, expected):
            original(content, expected)
            raise Crash()
        with patch.object(self.batch, "_write", side_effect=crash_write):
            with self.assertRaises(Crash):
                self.batch.tick()
        self.assertEqual(self.store.get("plugin_batch")["phase"], "closing")
        self.assertEqual(self.text(), "")
        self.recover()
        self.add(2)
        self.batch.tick(start=False)
        self.assertIsNone(self.store.get("plugin_batch"))
        self.assertEqual(self.text(), "")
        self.batch.tick(start=False)
        self.assertEqual(self.lines(), ["# pending " + self.row(2)["normalized"]])
        self.assertEqual(self.plugin.runs, 1)


class BoundaryTests(TestFixture):
    def test_plugin_becomes_running_during_confirmation_keeps_file_frozen(self):
        self.start_batch()
        frozen = self.queue.read_bytes()
        def confirm(item):
            self.plugin.state = True
            return Decision("exists")
        self.cloud.responses[1] = confirm
        self.advance()
        result = self.batch.tick()
        self.assertEqual(result["reported"], 1)
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertIsNotNone(self.store.get("plugin_batch"))
        self.assertEqual(self.plugin.runs, 1)
        self.plugin.state = False
        self.advance()
        self.batch.tick()
        self.assertEqual(self.text(), "")

    def test_crash_after_retry_transition_retires_without_querying_or_reuploading_it(self):
        self.start_batch()
        self.advance()
        original = self.store.finish
        def crash_finish(item, state, now, **kwargs):
            original(item, state, now, **kwargs)
            if state == "retry":
                raise Crash()
        with patch.object(self.store, "finish", side_effect=crash_finish):
            with self.assertRaises(Crash):
                self.batch.tick()
        self.assertEqual(self.row()["state"], "retry")
        self.assertIsNotNone(self.store.get("plugin_batch"))
        calls = len(self.cloud.calls)
        self.recover()
        self.batch.tick()
        self.assertIsNone(self.store.get("plugin_batch"))
        self.assertEqual(len(self.cloud.calls), calls)
        self.assertEqual(self.lines(), ["# retry " + self.row()["normalized"]])
        self.assertEqual(self.plugin.runs, 1)

    def test_stop_after_dispatch_journal_prevents_plugin_network_call(self):
        self.add(1)
        stop = threading.Event()
        original = self.batch._save
        def stop_after_journal(batch, counts):
            original(batch, counts)
            if batch["phase"] == "dispatching":
                stop.set()
        with patch.object(self.batch, "_save", side_effect=stop_after_journal):
            self.batch.tick(stop=stop)
        self.assertEqual(self.plugin.runs, 0)
        self.assertFalse(self.store.get("plugin_batch")["submitted"])
        self.assertEqual(self.store.get("plugin_batch")["phase"], "prepared")
        self.recover()
        self.advance()
        self.batch.tick()
        self.assertEqual(self.row()["state"], "retry")
        self.assertEqual(self.plugin.runs, 0)

    def test_idle_rename_crash_recovers_exact_journaled_image(self):
        self.add(1)
        self.batch.tick(start=False)
        self.add(2)
        original = self.batch._write
        def crash_write(content, expected):
            original(content, expected)
            raise Crash()
        with patch.object(self.batch, "_write", side_effect=crash_write):
            with self.assertRaises(Crash):
                self.batch.tick(start=False)
        frozen = self.queue.read_bytes()
        self.recover()
        self.plugin.state = None
        self.batch.tick(start=False)
        self.assertIsNone(self.store.get("report_pause"))
        self.assertEqual(self.queue.read_bytes(), frozen)
        self.assertEqual(self.plugin.runs, 0)

    def test_external_mutation_during_prequery_is_preserved_at_replace_boundary(self):
        self.add(1)
        self.batch.tick(start=False)
        def mutate(item):
            self.queue.write_text("foreign content\n", encoding="utf-8")
            return NEEDS
        self.cloud.responses[1] = mutate
        result = self.batch.tick()
        self.assertTrue(result["paused"])
        self.assertEqual(self.text(), "foreign content\n")
        self.assertEqual(self.store.get("report_pause")["reason"], "plugin_queue_changed")
        self.assertEqual(self.plugin.runs, 0)


class DecisionTests(TestFixture):
    def test_all_stopped_decisions_and_unknown_material_are_not_dispatched(self):
        for ident, status in enumerate(("blocked", "ignored", "conflict"), 1):
            self.add(ident)
            self.cloud.responses[ident] = Decision(status)
        for ident, part in ((4, "sha1"), (5, "sha256"), (6, "unknown")):
            self.add(ident)
            self.cloud.responses[ident] = Decision("report_required", ("md4", part))
        result = self.batch.tick()
        self.assertEqual((result["blocked"], result["failed"]), (5, 1))
        self.assertEqual(self.plugin.runs, 0)
        self.assertEqual(self.row(3)["state"], "failed")
        for ident in (4, 5, 6):
            self.assertEqual(self.row(ident)["last_error"], "missing_hash_material")
        self.assertTrue(all(line.startswith("# ") for line in self.lines()))

    def test_non_decisions_and_invalid_parts_do_not_become_successes(self):
        for ident, response in enumerate((None, {"status": "exists"}, Decision("unknown"), Decision("exists", (None,))), 1):
            self.add(ident)
            self.cloud.responses[ident] = response
        result = self.batch.tick()
        self.assertEqual(result["retry"], 4)
        self.assertEqual((result["reported"], result["existing"], self.plugin.runs), (0, 0, 0))
        self.assertTrue(all(line.startswith("# retry ") for line in self.lines()))

    def test_real_ms_query_requires_business_code_and_does_not_call_write_apis(self):
        self.add(1)
        calls = []
        responses = iter([{"code": 20000, "data": {"status": "need_payload", "required_parts": ["media", "md4"]}},
                          {"code": "20000", "data": {"status": "exists"}},
                          {"code": 20000, "data": {"status": "exists"}}])
        def transport(url, payload, headers, **flags):
            calls.append((url, flags))
            return next(responses)
        secrets = {"ms_url": "http://localhost:8888", "ms_api_key": "fake", "email": "test@example.invalid", "slogan": "fake", "driver_name": "115 Open"}
        self.batch.cloud = MsReporter(secrets, transport=transport)
        self.batch.tick()
        self.advance()
        self.batch.tick()
        self.assertEqual(self.row()["state"], "uncertain")
        self.assertEqual(self.row()["next_retry"], self.now + 30)
        self.advance(30)
        self.batch.tick()
        self.assertEqual(self.row()["state"], "reported")
        self.assertEqual(self.plugin.runs, 1)
        self.assertTrue(all(url.endswith("/query_by_unique") and not flags["write"] for url, flags in calls))


if __name__ == "__main__":
    unittest.main()
