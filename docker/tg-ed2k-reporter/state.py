from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ed2k import Message, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channels (
  name TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
  since REAL NOT NULL, bootstrap_before INTEGER,
  bootstrap_done INTEGER NOT NULL DEFAULT 0, last_success REAL
);
CREATE TABLE IF NOT EXISTS messages (
  channel TEXT NOT NULL, message_id INTEGER NOT NULL, published_at REAL NOT NULL,
  digest TEXT NOT NULL, PRIMARY KEY(channel, message_id)
);
CREATE TABLE IF NOT EXISTS items (
  md4 TEXT NOT NULL, size INTEGER NOT NULL, name TEXT NOT NULL,
  normalized TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
  last_error TEXT, receipt TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(md4, size)
);
CREATE INDEX IF NOT EXISTS due_items ON items(state, next_retry, created_at);
CREATE TABLE IF NOT EXISTS sightings (
  channel TEXT NOT NULL, message_id INTEGER NOT NULL,
  md4 TEXT NOT NULL, size INTEGER NOT NULL, name TEXT NOT NULL,
  normalized TEXT NOT NULL, raw TEXT NOT NULL, repaired INTEGER NOT NULL,
  seen_at REAL NOT NULL, PRIMARY KEY(channel, message_id, md4, size)
);
CREATE TABLE IF NOT EXISTS parse_errors (
  channel TEXT NOT NULL, message_id INTEGER NOT NULL,
  fingerprint TEXT NOT NULL, raw TEXT NOT NULL, reason TEXT NOT NULL, seen_at REAL NOT NULL,
  PRIMARY KEY(channel, message_id, fingerprint)
);
"""


class Store:
    def __init__(self, directory: str | Path, *, readonly=False):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "state.sqlite3"
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(self.path, timeout=10)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
            if self.get("schema_version") not in (None, 1):
                raise RuntimeError("unsupported_state_version")
            self.set("schema_version", 1)
            os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self._set(key, value)

    def _set(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def ensure_channel(self, name, since):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO channels(name,since) VALUES(?,?)", (name, since))
        return self.channel(name)

    def channel(self, name):
        row = self.db.execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def _ingest(self, message: Message, now):
        previous = self.db.execute("SELECT digest FROM messages WHERE channel=? AND message_id=?", (message.channel, message.message_id)).fetchone()
        if previous and previous[0] == message.digest:
            return {"new": 0, "valid": 0, "repaired": 0, "invalid": 0}
        result = normalize(message.text)
        counts = {"new": 0, "valid": len(result.links), "repaired": sum(link.repaired for link in result.links), "invalid": len(result.errors)}
        for link in result.links:
            created = self.db.execute("INSERT OR IGNORE INTO items(md4,size,name,normalized,created_at,updated_at) VALUES(?,?,?,?,?,?)", (link.md4, link.size, link.name, link.normalized, now, now))
            counts["new"] += created.rowcount
            self.db.execute("""INSERT INTO sightings(channel,message_id,md4,size,name,normalized,raw,repaired,seen_at)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(channel,message_id,md4,size) DO UPDATE SET
              name=excluded.name,normalized=excluded.normalized,raw=excluded.raw,
              repaired=excluded.repaired,seen_at=excluded.seen_at""",
              (message.channel, message.message_id, link.md4, link.size, link.name, link.normalized, link.raw, int(link.repaired), now))
        # Retain invalid source fields for correction; do not print them in logs.
        for error in result.errors:
            fingerprint = hashlib.sha256(error.raw.encode("utf-8")).hexdigest()
            self.db.execute("INSERT OR IGNORE INTO parse_errors VALUES(?,?,?,?,?,?)", (message.channel, message.message_id, fingerprint, error.raw, error.reason, now))
        self.db.execute("""INSERT INTO messages VALUES(?,?,?,?) ON CONFLICT(channel,message_id)
          DO UPDATE SET published_at=excluded.published_at,digest=excluded.digest""",
          (message.channel, message.message_id, message.published_at, message.digest))
        return counts

    def ingest(self, messages, now, *, channel=None, cursor=None, bootstrap_before=None, bootstrap_done=None):
        totals = {"new": 0, "valid": 0, "repaired": 0, "invalid": 0}
        # Source records and cursor commit together. Any exception rolls back both.
        with self.db:
            for message in messages:
                for key, value in self._ingest(message, now).items():
                    totals[key] += value
            if channel is not None:
                fields, values = ["last_success=?"], [now]
                if cursor is not None:
                    fields.append("cursor=MAX(cursor,?)")
                    values.append(cursor)
                if bootstrap_done is not None:
                    fields.extend(["bootstrap_before=?", "bootstrap_done=?"])
                    values.extend([bootstrap_before, int(bootstrap_done)])
                values.append(channel)
                self.db.execute("UPDATE channels SET " + ",".join(fields) + " WHERE name=?", values)
        return totals

    def due(self, now, limit):
        return [dict(row) for row in self.db.execute("SELECT * FROM items WHERE state IN ('pending','retry') AND next_retry<=? ORDER BY created_at,md4,size LIMIT ?", (now, limit))]

    def begin(self, item, now):
        with self.db:
            changed = self.db.execute("UPDATE items SET state='inflight',attempts=attempts+1,updated_at=? WHERE md4=? AND size=? AND state IN ('pending','retry')", (now, item["md4"], item["size"]))
        return changed.rowcount == 1

    def finish(self, item, state, now, *, error=None, retry_at=0, receipt=None):
        with self.db:
            self.db.execute("UPDATE items SET state=?,last_error=?,next_retry=?,receipt=?,updated_at=? WHERE md4=? AND size=? AND state='inflight'", (state, error, retry_at, None if receipt is None else json.dumps(receipt), now, item["md4"], item["size"]))

    def recover(self, now):
        # A request may have reached the server before a crash. These records
        # require a read-back check by the reporting client before any retry.
        with self.db:
            self.db.execute("UPDATE items SET state='uncertain',last_error='interrupted_request',updated_at=? WHERE state='inflight'", (now,))

    def uncertain(self, now=None, limit=20):
        if now is None:
            return [dict(row) for row in self.db.execute("SELECT * FROM items WHERE state='uncertain' ORDER BY next_retry,updated_at LIMIT ?", (limit,))]
        return [dict(row) for row in self.db.execute("SELECT * FROM items WHERE state='uncertain' AND next_retry<=? ORDER BY next_retry,updated_at LIMIT ?", (now, limit))]

    def resolve(self, item, state, now, *, receipt=None, error=None, retry_at=0):
        with self.db:
            self.db.execute("UPDATE items SET state=?,receipt=?,last_error=?,updated_at=?,next_retry=? WHERE md4=? AND size=? AND state='uncertain'", (state, json.dumps(receipt) if receipt is not None else None, error, now, retry_at, item["md4"], item["size"]))

    def retry_failed(self, now, *, item=None):
        condition = " AND md4=? AND size=?" if item is not None else ""
        values = (now, now, item['md4'], item['size']) if item is not None else (now, now)
        with self.db:
            changed = self.db.execute("UPDATE items SET state='retry',attempts=0,next_retry=?,updated_at=?,last_error=NULL WHERE state IN ('failed','blocked')" + condition, values)
            if item is None or changed.rowcount:
                self._set("report_pause", None)
        return changed.rowcount

    def status(self):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT state,COUNT(*) FROM items GROUP BY state")}
        receipts = {row[0]: row[1] for row in self.db.execute("SELECT json_extract(receipt,'$.status'),COUNT(*) FROM items WHERE state='reported' AND json_valid(receipt) GROUP BY json_extract(receipt,'$.status')")}
        plugin = self.get('plugin_status')
        if isinstance(plugin, dict) and plugin.get('phase') == 'idle' and not plugin.get('error'):
            plugin = self.get('plugin_last_batch') or plugin
        if isinstance(plugin, dict):
            plugin = {key: plugin[key] for key in ('id', 'instance_id', 'phase', 'total', 'confirmed', 'failed', 'started_at', 'updated_at', 'next_check_at', 'error') if key in plugin}
        else:
            plugin = None
        return {
            "counts": counts,
            "total_unique": sum(counts.values()),
            "receipt_counts": {key: receipts.get(key, 0) for key in ('created', 'updated', 'confirmed')},
            "report_backend": self.get('report_backend', 'direct'),
            "plugin": plugin,
            "source_messages": self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "parse_errors": self.db.execute("SELECT COUNT(*) FROM parse_errors").fetchone()[0],
            "repaired_links": self.db.execute("SELECT COUNT(*) FROM sightings WHERE repaired=1").fetchone()[0],
            "channels": [dict(row) for row in self.db.execute("SELECT name,cursor,bootstrap_done,last_success FROM channels ORDER BY name")],
            "last_cycle": self.get("last_cycle"),
            "heartbeat": self.get("heartbeat"),
            "report_pause": self.get("report_pause"),
            "poll_seconds": self.get("poll_seconds", 300),
        }

    def export(self, destination):
        rows = self.db.execute("SELECT normalized FROM items ORDER BY created_at,md4,size")
        destination = Path(destination)
        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(row[0] + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)


@contextmanager
def run_lock(directory):
    path = Path(directory) / "worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("worker_already_running") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("worker_already_running") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
