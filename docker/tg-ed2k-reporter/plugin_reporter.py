from __future__ import annotations

import hashlib
import os
import tempfile
import time
import uuid
from pathlib import Path

from reporter import COMPLETE, NEEDED, STOPPED, Decision, ReportError

COUNTERS = ("processed", "reported", "existing", "retry", "failed", "blocked", "uncertain", "rechecked")
FINISHED = {"reported", "existing"}
TERMINAL = FINISHED | {"failed", "blocked"}
ERRORS = {
    "auth_failed", "cloud_network_error", "cloud_query_failed", "cloud_decision_invalid",
    "missing_hash_material", "plugin_network_error", "plugin_run_failed",
    "plugin_dispatch_uncertain", "plugin_response_invalid", "plugin_interrupted",
    "plugin_not_confirmed", "plugin_timeout", "plugin_running", "plugin_state_unknown",
    "plugin_queue_changed", "plugin_queue_io_error", "plugin_batch_invalid",
    "plugin_instance_changed", "plugin_prepared_recovered", "shutdown_before_write",
    *STOPPED,
}


class _QueueChanged(RuntimeError):
    pass


class PluginBatch:
    """Worker-owned SQLite journal and immutable TXT batches for the native plugin.

    The cloud client is used exclusively for query(). A dispatch journal means
    the call *may* have reached MS, including a crash immediately before run().
    Only run()['completed'] is True or running() is False permits retiring TXT.
    """

    def __init__(self, store, cloud, plugin, settings, queue_file, instance_id, *, clock=time.time, notices=None):
        self.store, self.cloud, self.plugin = store, cloud, plugin
        self.settings, self.clock = settings, clock
        self.notices = notices
        self.queue_file, self.instance_id = Path(queue_file), instance_id
        options = settings.get("ms_plugin") or {}
        self.check_seconds = max(1, float(options.get("check_seconds", 15)))
        self.timeout_seconds = max(1, float(options.get("timeout_seconds", 900)))

    def _rows(self, batch=None):
        if batch is None:
            return [dict(row) for row in self.store.db.execute(
                "SELECT * FROM items ORDER BY created_at,md4,size")]
        rows = []
        for key in batch["keys"]:
            row = self.store.db.execute("SELECT * FROM items WHERE md4=? AND size=?", key).fetchone()
            if row is None:
                raise ValueError("plugin_batch_invalid")
            rows.append(dict(row))
        return rows

    @staticmethod
    def _key(item):
        return item["md4"], item["size"]

    def _render(self, selected=()):
        selected = set(selected)
        lines = []
        for item in self._rows():
            if item["state"] in FINISHED:
                continue
            prefix = "" if self._key(item) in selected else f"# {item['state']} "
            lines.append(prefix + item["normalized"] + "\n")
        return "".join(lines).encode("utf-8")

    def _digest(self):
        try:
            with self.queue_file.open("rb") as stream:
                checksum = hashlib.sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    checksum.update(chunk)
                return checksum.hexdigest()
        except FileNotFoundError:
            return None

    def _write(self, content, expected):
        # Deployment owns directory creation. An absent mount must fail closed.
        descriptor, name = tempfile.mkstemp(prefix="." + self.queue_file.name + ".", suffix=".tmp", dir=self.queue_file.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if self._digest() != expected:
                raise _QueueChanged()
            os.replace(temporary, self.queue_file)
            if os.name != "nt":
                directory = os.open(self.queue_file.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _meta(self, key, value, counts):
        if self.store.get(key) != value:
            self.store.set(key, value)
            counts["changed"] = True

    def _save(self, batch, counts):
        batch["updated_at"] = self.clock()
        self._meta("plugin_batch", batch, counts)

    def _pause(self, reason, counts, batch=None):
        self._meta("report_pause", {"reason": reason, "at": self.clock()}, counts)
        if batch is not None:
            batch["error"] = reason
            self._save(batch, counts)
        counts["paused"] = True

    def _delay(self, item):
        base, cap = self.settings["retry_base_seconds"], self.settings["retry_cap_seconds"]
        exponent = max(0, item["attempts"] - 1)
        # Avoid constructing enormous integers from corrupt/very old attempt counts.
        return min(cap, base * 2 ** min(exponent, 30))

    def _change(self, item, state, counts, *, error=None, receipt=None, retry_at=0):
        method = self.store.finish if item["state"] == "inflight" else self.store.resolve
        if item["state"] not in {"inflight", "uncertain"}:
            raise ValueError("plugin_batch_invalid")
        method(item, state, self.clock(), error=error, receipt=receipt, retry_at=retry_at)
        counts[state] += 1
        counts["changed"] = True
        item.update(state=state, next_retry=retry_at, last_error=error)

    @staticmethod
    def _validate(decision):
        if not isinstance(decision, Decision) or not isinstance(decision.status, str) or decision.status not in COMPLETE | STOPPED | NEEDED:
            raise ReportError("cloud_decision_invalid", transient=True)
        if not isinstance(decision.parts, (tuple, list)) or any(not isinstance(part, str) for part in decision.parts):
            raise ReportError("cloud_decision_invalid", transient=True)
        return decision

    @staticmethod
    def _reason(error, *, plugin=False):
        if isinstance(error, ReportError) and error.auth:
            return "auth_failed"
        if isinstance(error, InterruptedError):
            return "plugin_interrupted" if plugin else "shutdown_before_write"
        if isinstance(error, ReportError) and str(error) == "cloud_decision_invalid":
            return "cloud_decision_invalid"
        if isinstance(error, ReportError) and error.uncertain and plugin:
            return "plugin_dispatch_uncertain"
        if isinstance(error, (TimeoutError, OSError)) or isinstance(error, ReportError) and error.transient:
            return "plugin_network_error" if plugin else "cloud_network_error"
        return "plugin_run_failed" if plugin else "cloud_query_failed"

    @staticmethod
    def _stopped(stop):
        return stop is not None and stop.is_set()

    def _checking_allowed(self):
        pause = self.store.get("report_pause")
        return not pause or isinstance(pause, dict) and pause.get("reason") == "manual"

    def _starting_allowed(self, start, stop):
        return start and not self._stopped(stop) and not self.store.get("report_pause")

    def _running(self, stop, counts, batch=None):
        if self._stopped(stop):
            return None
        try:
            result = self.plugin.running()
            return result if type(result) is bool else None
        except (ReportError, OSError, InterruptedError) as error:
            if isinstance(error, ReportError) and error.auth:
                self._pause("auth_failed", counts, batch)
            return None

    def _decision(self, item, decision, counts, batch=None):
        """Return True when a strict query decision resolves an item."""
        if decision.status in COMPLETE:
            submitted = batch is not None and batch["submitted"]
            receipt = {"via": "ms_plugin_query", "code": 20000, "status": "confirmed" if submitted else decision.status}
            if submitted:
                receipt["batch_id"] = batch["id"]
            self._change(item, "reported" if submitted else "existing", counts, receipt=receipt)
            return True
        if decision.status in STOPPED:
            self._change(item, "failed" if decision.status == "conflict" else "blocked", counts,
                         error=decision.status, receipt={"via": "ms_plugin_query", "code": 20000, "status": decision.status})
            return True
        if set(decision.parts) - {"media", "md4"}:
            self._change(item, "blocked", counts, error="missing_hash_material")
            return True
        return False

    def _query_error(self, item, error, counts, *, active=False):
        reason = self._reason(error)
        if active or item["state"] == "uncertain":
            state = "uncertain"
        else:
            transient = isinstance(error, (OSError, InterruptedError)) or isinstance(error, ReportError) and error.transient
            state = "blocked" if reason == "auth_failed" else "retry" if transient and item["attempts"] < self.settings["max_attempts"] else "failed"
        self._change(item, state, counts, error=reason, retry_at=self.clock() + self._delay(item))
        if reason == "auth_failed":
            self._pause(reason, counts)

    def _summary(self, batch=None, *, phase="idle", error=None):
        rows = self._rows(batch)
        if batch is None:
            rows = [item for item in rows if item["state"] not in FINISHED]
        result = {
            "id": batch["id"] if batch else None, "instance_id": self.instance_id,
            "phase": batch["phase"] if batch else phase,
            "total": len(rows), "confirmed": sum(item["state"] in FINISHED for item in rows),
            "failed": sum(item["state"] in {"failed", "blocked"} for item in rows),
            "started_at": batch["started_at"] if batch else None,
            "updated_at": batch["updated_at"] if batch else self.clock(),
            "next_check_at": batch["next_check_at"] if batch else None,
            "error": (batch.get("error") if batch else error),
        }
        if result["error"] not in ERRORS:
            result["error"] = None
        return result

    def _result(self, counts, start, stop, *, phase="idle", error=None):
        batch = self.store.get("plugin_batch")
        try:
            summary = self._summary(batch, phase=phase, error=error)
        except (KeyError, TypeError, ValueError):
            summary = self._summary(phase="paused", error="plugin_batch_invalid")
        pause = self.store.get("report_pause")
        if isinstance(pause, dict) and pause.get("reason") in ERRORS:
            summary["error"] = pause["reason"]
        previous = self.store.get("plugin_status")
        if isinstance(previous, dict) and "updated_at" in previous and all(previous.get(key) == value for key, value in summary.items() if key != "updated_at"):
            summary["updated_at"] = previous["updated_at"]
        self._meta("plugin_status", summary, counts)
        self.store.set("heartbeat", self.clock())
        counts["paused"] = bool(counts["paused"] or self.store.get("report_pause") or not start or self._stopped(stop))
        return {**counts, "plugin": summary}

    def _check_file(self, batch, counts):
        actual = self._digest()
        if batch is None:
            expected = self.store.get("plugin_queue_checksum")
            rewrite = self.store.get("plugin_queue_update")
            if rewrite is not None:
                if actual == rewrite["checksum"]:
                    with self.store.db:
                        self.store._set("plugin_queue_checksum", actual)
                        self.store._set("plugin_queue_update", None)
                    expected = actual
                    counts["changed"] = True
                elif actual == rewrite["previous"]:
                    self._meta("plugin_queue_update", None, counts)
                else:
                    raise _QueueChanged()
            if expected is None:
                # Adopt only an empty file or our exact, already commented view.
                if actual not in (None, hashlib.sha256(b"").hexdigest(), hashlib.sha256(self._render()).hexdigest()):
                    raise _QueueChanged()
            elif actual != expected:
                raise _QueueChanged()
            return actual
        if batch["instance_id"] != self.instance_id:
            self._pause("plugin_instance_changed", counts, batch)
            return actual
        if actual == batch["checksum"]:
            return actual
        # These two journaled rename windows accept only the precise next image.
        # They never accept arbitrary external file changes or dispatch a recovery.
        if batch["phase"] == "prepared" and not batch["published"] and actual == batch.get("publish_checksum"):
            batch.update(checksum=actual, published=True)
            self._save(batch, counts)
            return actual
        if batch["phase"] == "closing" and actual == batch.get("closing_checksum"):
            return actual
        raise _QueueChanged()

    def _idle_file(self, expected, counts):
        content = self._render()
        checksum = hashlib.sha256(content).hexdigest()
        if expected != checksum:
            self._meta("plugin_queue_update", {"previous": expected, "checksum": checksum}, counts)
            self._write(content, expected)
            with self.store.db:
                self.store._set("plugin_queue_checksum", checksum)
                self.store._set("plugin_queue_update", None)
            counts["changed"] = True
        else:
            self._meta("plugin_queue_checksum", checksum, counts)

    def _close(self, batch, counts):
        if batch["phase"] != "closing":
            rows = self._rows(batch)
            content = self._render()
            batch.update(phase="closing", closing_checksum=hashlib.sha256(content).hexdigest(),
                         close_phase="withdrawn" if not batch["submitted"] else "completed" if all(item["state"] in FINISHED for item in rows) else "failed")
            self._save(batch, counts)
        else:
            content = self._render()
        actual = self._digest()
        # A crash after rename must retire exactly that journaled image. New
        # imports can wait until the next idle tick instead of changing it here.
        if actual != batch["closing_checksum"]:
            if hashlib.sha256(content).hexdigest() != batch["closing_checksum"]:
                # No rename happened; keep the journal current with new comments.
                batch["closing_checksum"] = hashlib.sha256(content).hexdigest()
                self._save(batch, counts)
            self._write(content, batch["checksum"])
        summary = self._summary(batch)
        summary.update(phase=batch["close_phase"], updated_at=self.clock(), next_check_at=None)
        with self.store.db:
            self.store._set("plugin_queue_checksum", batch["closing_checksum"])
            self.store._set("plugin_last_batch", summary)
            if self.notices is not None:
                self.notices.enqueue(batch, self._rows(batch), summary)
            self.store._set("plugin_batch", None)
        counts["changed"] = True

    def _active(self, batch, limit, stop, counts):
        now = self.clock()
        if now < batch["next_check_at"] or self._stopped(stop) or not self._checking_allowed():
            return
        completed = batch.get("completed") is True
        running = False if completed else self._running(stop, counts, batch)
        if self._stopped(stop) or not self._checking_allowed():
            return
        terminal = running is False
        if batch["phase"] == "closing":
            if terminal:
                self._close(batch, counts)
            else:
                batch.update(next_check_at=now + self.check_seconds, error="plugin_running" if running else "plugin_state_unknown")
                self._save(batch, counts)
            return
        # Negative read-backs observed during execution cannot authorize retries.
        if not terminal or not batch.get("idle_observed"):
            batch["checked"] = []
        batch["idle_observed"] = terminal
        checked = {tuple(key) for key in batch.get("checked", [])}
        rows = self._rows(batch)
        eligible = [item for item in rows if item["state"] in {"inflight", "uncertain"} and item["next_retry"] <= now and self._key(item) not in checked]
        eligible.sort(key=lambda item: (item["next_retry"], item["updated_at"], item["md4"], item["size"]))
        for item in eligible[:limit]:
            if self._stopped(stop) or not self._checking_allowed():
                break
            try:
                decision = self._validate(self.cloud.query(item))
                if not self._decision(item, decision, counts, batch):
                    if terminal:
                        checked.add(self._key(item))
                    else:
                        self._change(item, "uncertain", counts, error="plugin_not_confirmed", retry_at=now + self.check_seconds)
            except (ReportError, OSError, InterruptedError) as error:
                self._query_error(item, error, counts, active=True)
            counts["rechecked"] += 1
        batch["checked"] = [list(key) for key in sorted(checked)]
        if self._stopped(stop) or not self._checking_allowed():
            batch["next_check_at"] = now + self.check_seconds
            self._save(batch, counts)
            return
        rows = self._rows(batch)
        # A committed retry can survive a crash before the closing journal.
        # It is already settled for this batch and remains commented on retirement.
        unresolved = [item for item in rows if item["state"] in {"inflight", "uncertain"}]
        ready = all(self._key(item) in checked for item in unresolved)
        if terminal and ready and not completed:
            running = self._running(stop, counts, batch)
            terminal = running is False
            if not terminal:
                checked.clear()
                batch.update(checked=[], idle_observed=False)
            if self._stopped(stop) or not self._checking_allowed():
                batch["next_check_at"] = now + self.check_seconds
                self._save(batch, counts)
                return
        if terminal and ready:
            reason = "plugin_prepared_recovered" if not batch["submitted"] else "plugin_timeout" if now >= batch["deadline"] else batch.get("error") or "plugin_not_confirmed"
            if reason not in ERRORS or reason in {"plugin_running", "plugin_state_unknown"}:
                reason = "plugin_not_confirmed"
            for item in unresolved:
                state = "failed" if item["attempts"] >= self.settings["max_attempts"] else "retry"
                self._change(item, state, counts, error=reason, retry_at=now + self._delay(item))
            if unresolved:
                batch["error"] = reason
            self._close(batch, counts)
            return
        batch["phase"] = "prepared" if not batch["submitted"] else "confirming" if not unresolved else "running" if batch["phase"] != "dispatching" else "dispatching"
        if now >= batch["deadline"]:
            batch["error"] = "plugin_timeout"
        elif running is not False:
            batch["error"] = "plugin_running" if running else "plugin_state_unknown"
        waits = [item["next_retry"] for item in unresolved if self._key(item) not in checked]
        batch["next_check_at"] = max(now + self.check_seconds, min(waits)) if terminal and waits else now + self.check_seconds
        self._save(batch, counts)

    def _orphans(self, limit, running, stop, counts):
        rows = [dict(row) for row in self.store.db.execute(
            "SELECT * FROM items WHERE state IN ('inflight','uncertain') AND next_retry<=? ORDER BY next_retry,updated_at,md4,size LIMIT ?", (self.clock(), limit))]
        for item in rows:
            if self._stopped(stop) or not self._checking_allowed():
                break
            try:
                decision = self._validate(self.cloud.query(item))
                if not self._decision(item, decision, counts):
                    state = "uncertain" if running is not False else "failed" if item["attempts"] >= self.settings["max_attempts"] else "retry"
                    self._change(item, state, counts, error="plugin_not_confirmed", retry_at=self.clock() + self._delay(item))
            except (ReportError, OSError, InterruptedError) as error:
                self._query_error(item, error, counts, active=True)
            counts["rechecked"] += 1

    def _dispatch(self, items, expected, stop, start, counts):
        # Querying can take time. Recheck idleness and pause at the write boundary.
        running = self._running(stop, counts)
        if running is not False or not self._starting_allowed(start, stop):
            for item in items:
                state = "failed" if item["attempts"] >= self.settings["max_attempts"] else "retry"
                self._change(item, state, counts, error="shutdown_before_write" if self._stopped(stop) else "plugin_state_unknown" if running is None else "plugin_running", retry_at=self.clock() + self._delay(item))
            return
        now = self.clock()
        keys = [self._key(item) for item in items]
        content = self._render(keys)
        batch = {
            "id": uuid.uuid4().hex, "instance_id": self.instance_id, "phase": "prepared",
            "keys": [list(key) for key in keys], "started_at": now, "updated_at": now,
            "next_check_at": now, "deadline": now + self.timeout_seconds,
            "checksum": expected, "publish_checksum": hashlib.sha256(content).hexdigest(),
            "published": False, "submitted": False, "completed": False, "checked": [], "error": None,
        }
        self._save(batch, counts)
        if not self._starting_allowed(start, stop):
            return
        self._write(content, expected)
        batch.update(checksum=batch["publish_checksum"], published=True)
        self._save(batch, counts)
        if not self._starting_allowed(start, stop):
            return  # Durable prepared batch; recovery can safely withdraw it.
        batch.update(phase="dispatching", submitted=True, next_check_at=now + self.check_seconds)
        self._save(batch, counts)
        if not self._starting_allowed(start, stop):
            batch.update(phase="prepared", submitted=False, next_check_at=self.clock())
            self._save(batch, counts)
            return
        try:
            result = self.plugin.run()
            if not isinstance(result, dict) or type(result.get("completed")) is not bool:
                batch["error"] = "plugin_response_invalid"
            else:
                batch.update(completed=result["completed"], phase="confirming" if result["completed"] else "running")
        except (ReportError, OSError, InterruptedError) as error:
            reason = self._reason(error, plugin=True)
            batch["error"] = reason
            for item in items:
                self._change(item, "uncertain", counts, error=reason, retry_at=self.clock() + self.check_seconds)
            if reason == "auth_failed":
                self._pause(reason, counts)
        self._save(batch, counts)

    def tick(self, *, limit=None, stop=None, start=True):
        counts = {**dict.fromkeys(COUNTERS, 0), "paused": False, "changed": False}
        limit = max(0, min(self.settings["max_reports_per_cycle"], limit)) if limit is not None else self.settings["max_reports_per_cycle"]
        batch = self.store.get("plugin_batch")
        try:
            expected = self._check_file(batch, counts)
            if self._stopped(stop) or not self._checking_allowed():
                return self._result(counts, start, stop, phase="paused")
            if batch is not None:
                self._active(batch, limit, stop, counts)
                # Retirement and new dispatch occupy separate ticks.
                return self._result(counts, start, stop)
            running = self._running(stop, counts)
            if self._stopped(stop) or not self._checking_allowed():
                return self._result(counts, start, stop, phase="paused")
            self._orphans(limit, running, stop, counts)
            if self._stopped(stop) or not self._checking_allowed():
                return self._result(counts, start, stop, phase="paused")
            if running is not False:
                counts["paused"] = True
                return self._result(counts, start, stop, phase="waiting", error="plugin_running" if running else "plugin_state_unknown")
            selected = []
            if self._starting_allowed(start, stop):
                for item in self.store.due(self.clock(), max(0, limit - counts["rechecked"])):
                    if not self._starting_allowed(start, stop):
                        break
                    if not self.store.begin(item, self.clock()):
                        continue
                    item.update(state="inflight", attempts=item["attempts"] + 1)
                    counts["processed"] += 1
                    counts["changed"] = True
                    try:
                        decision = self._validate(self.cloud.query(item))
                        if not self._decision(item, decision, counts):
                            selected.append(item)
                    except (ReportError, OSError, InterruptedError) as error:
                        self._query_error(item, error, counts)
            if selected:
                self._dispatch(selected, expected, stop, start, counts)
            elif not self._stopped(stop) and self._checking_allowed():
                # All-successful prequeries still need a fresh idle observation.
                if self._running(stop, counts) is False and not self._stopped(stop):
                    self._idle_file(expected, counts)
            return self._result(counts, start, stop, phase="paused" if not self._starting_allowed(start, stop) else "idle")
        except _QueueChanged:
            self._pause("plugin_queue_changed", counts, self.store.get("plugin_batch"))
        except OSError:
            self._pause("plugin_queue_io_error", counts, self.store.get("plugin_batch"))
        except (KeyError, TypeError, ValueError):
            self._pause("plugin_batch_invalid", counts)
        return self._result(counts, start, stop, phase="paused")
