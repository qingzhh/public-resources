#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import html
import http.cookies
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "config.properties")
DEFAULT_DB = os.path.join(ROOT_DIR, "state.db")
DEFAULT_LOG = os.path.join(ROOT_DIR, "u2.log")
DEFAULT_DEPLOY = os.path.join(ROOT_DIR, "deploy.jsonc")
DEFAULT_PID = os.path.join(ROOT_DIR, "u2_qb_bot.pid")
DEFAULT_SERVICE = os.path.join(ROOT_DIR, "u2_service.sh")
DEFAULT_CONFIG_TEMPLATE = os.path.join(ROOT_DIR, "config.template.properties")
DEFAULT_DEPLOY_TEMPLATE = os.path.join(ROOT_DIR, "deploy.template.jsonc")
DEFAULT_ARCHIVE_DIR = os.path.join(ROOT_DIR, "old_logs")
DEFAULT_TELEGRAM_OFFSET = os.path.join(ROOT_DIR, "telegram.offset")


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_message(message: str, log_file: str | None = None) -> None:
    line = f"[{now_text()}] {message}"
    print(line)
    if log_file:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def load_properties(path: str) -> dict[str, str]:
    config: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    return config


def get_int(config: dict[str, str], key: str, default: int) -> int:
    raw = config.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def ensure_text(value: str | None) -> str:
    return (value or "").strip()


def get_float(config: dict[str, str], key: str, default: float) -> float:
    raw = config.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class StateDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurrence_key TEXT NOT NULL UNIQUE,
                    seed_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    matched_text TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    result TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_seed_created
                    ON events(seed_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON events(created_at DESC);
                CREATE TABLE IF NOT EXISTS qb_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_at TEXT NOT NULL,
                    alltime_ul INTEGER NOT NULL DEFAULT 0,
                    alltime_dl INTEGER NOT NULL DEFAULT 0,
                    dl_speed INTEGER NOT NULL DEFAULT 0,
                    up_speed INTEGER NOT NULL DEFAULT 0,
                    free_space INTEGER NOT NULL DEFAULT 0,
                    total_torrents INTEGER NOT NULL DEFAULT 0,
                    downloading_count INTEGER NOT NULL DEFAULT 0,
                    categories_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_qb_snapshots_at
                    ON qb_snapshots(snapshot_at DESC);
                """
            )

    def has_occurrence(self, occurrence_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE occurrence_key = ?",
                (occurrence_key,),
            ).fetchone()
        return row is not None

    def record_event(
        self,
        occurrence_key: str,
        seed_id: str,
        source: str,
        matched_text: str,
        action: str,
        result: str,
        note: str,
    ) -> None:
        timestamp = now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    occurrence_key, seed_id, source, matched_text,
                    action, result, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(occurrence_key) DO UPDATE SET
                    seed_id = excluded.seed_id,
                    source = excluded.source,
                    matched_text = excluded.matched_text,
                    action = excluded.action,
                    result = excluded.result,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    occurrence_key,
                    seed_id,
                    source,
                    matched_text,
                    action,
                    result,
                    note,
                    timestamp,
                    timestamp,
                ),
            )

    def last_success_at(self, seed_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at
                FROM events
                WHERE seed_id = ? AND result = 'added'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (seed_id,),
            ).fetchone()
        return row["created_at"] if row else None

    def recent_events(self, hours: int = 24, limit: int = 100) -> list[sqlite3.Row]:
        since = (dt.datetime.now() - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM events
                WHERE created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        return rows

    def recent_events_for_seed(self, seed_id: str, limit: int = 5) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM events
                WHERE seed_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (seed_id, limit),
            ).fetchall()
        return rows

    def last_event_for_seed(self, seed_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM events
                WHERE seed_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (seed_id,),
            ).fetchone()
        return row

    def summary(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            added = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE result = 'added'"
            ).fetchone()["c"]
            skipped = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE result LIKE 'skip_%' OR result = 'exists_in_qb'"
            ).fetchone()["c"]
            failed = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE result IN ('download_failed', 'qb_add_failed', 'error')"
            ).fetchone()["c"]
        return {"total": total, "added": added, "skipped": skipped, "failed": failed}

    def record_qb_snapshot(self, snapshot: dict[str, object]) -> None:
        timestamp = now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO qb_snapshots (
                    snapshot_at, alltime_ul, alltime_dl, dl_speed, up_speed,
                    free_space, total_torrents, downloading_count, categories_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    int(snapshot.get("alltime_ul", 0) or 0),
                    int(snapshot.get("alltime_dl", 0) or 0),
                    int(snapshot.get("dl_info_speed", 0) or 0),
                    int(snapshot.get("up_info_speed", 0) or 0),
                    int(snapshot.get("free_space_on_disk", 0) or 0),
                    int(snapshot.get("total_torrents", 0) or 0),
                    int(snapshot.get("downloading_count", 0) or 0),
                    json.dumps(snapshot.get("categories", {}), ensure_ascii=False),
                ),
            )

    def latest_qb_snapshot(self) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM qb_snapshots ORDER BY snapshot_at DESC, id DESC LIMIT 1"
            ).fetchone()

    def qb_stats_for_period(self, period: str) -> dict[str, object]:
        now = dt.datetime.now()
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        start_text = start.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            first_row = conn.execute(
                "SELECT * FROM qb_snapshots WHERE snapshot_at >= ? ORDER BY snapshot_at ASC, id ASC LIMIT 1",
                (start_text,),
            ).fetchone()
            last_row = conn.execute(
                "SELECT * FROM qb_snapshots WHERE snapshot_at >= ? ORDER BY snapshot_at DESC, id DESC LIMIT 1",
                (start_text,),
            ).fetchone()

        if not first_row or not last_row:
            return {"uploaded": 0, "downloaded": 0, "categories": []}

        first_categories = json.loads(first_row["categories_json"] or "{}")
        last_categories = json.loads(last_row["categories_json"] or "{}")
        category_rows = []
        for name in sorted(set(first_categories) | set(last_categories)):
            first_item = first_categories.get(name, {})
            last_item = last_categories.get(name, {})
            up = max(0, int(last_item.get("uploaded", 0)) - int(first_item.get("uploaded", 0)))
            down = max(0, int(last_item.get("downloaded", 0)) - int(first_item.get("downloaded", 0)))
            count = int(last_item.get("count", 0) or 0)
            category_rows.append({"category": name or "(未分类)", "uploaded": up, "downloaded": down, "count": count})

        category_rows.sort(key=lambda item: (item["uploaded"] + item["downloaded"]), reverse=True)
        return {
            "uploaded": max(0, int(last_row["alltime_ul"]) - int(first_row["alltime_ul"])),
            "downloaded": max(0, int(last_row["alltime_dl"]) - int(first_row["alltime_dl"])),
            "categories": category_rows,
        }


class QBClient:
    def __init__(self, config: dict[str, str]) -> None:
        self.base_url = ensure_text(config.get("qb.url", "")).rstrip("/")
        self.username = ensure_text(config.get("qb.user", ""))
        self.password = ensure_text(config.get("qb.pass", ""))
        self.category = ensure_text(config.get("qb.category", ""))
        self.up_limit_mb = ensure_text(config.get("qb.up_limit_mb", "0")) or "0"
        self.max_downloading = get_int(config, "qb.max_downloading", 0)
        self.session_refresh_seconds = get_int(config, "qb.session_refresh_seconds", 1500)
        self.cookie_jar = urllib.request.HTTPCookieProcessor()
        self.opener = urllib.request.build_opener(self.cookie_jar)
        self.last_login: dt.datetime | None = None

    @property
    def up_limit_bytes(self) -> int:
        try:
            mb = float(self.up_limit_mb)
        except ValueError:
            mb = 0.0
        return int(mb * 1024 * 1024 + 0.5)

    def _login_needed(self) -> bool:
        if self.last_login is None:
            return True
        if self.session_refresh_seconds <= 0:
            return False
        age = (dt.datetime.now() - self.last_login).total_seconds()
        return age >= self.session_refresh_seconds

    def login(self) -> None:
        body = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode("utf-8")
        headers = {
            "Referer": self.base_url,
            "Origin": self.base_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        code, data = self._raw_request(
            "POST",
            "/api/v2/auth/login",
            body=body,
            headers=headers,
            auto_login=False,
        )
        text = data.decode("utf-8", errors="ignore")
        if code != 200 or text.strip() != "Ok.":
            raise RuntimeError(f"qB 登录失败：HTTP={code}，返回内容={text}")
        self.last_login = dt.datetime.now()

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        auto_login: bool = True,
        retry_on_403: bool = True,
    ) -> tuple[int, bytes]:
        if auto_login and self._login_needed():
            self.login()

        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.getcode(), resp.read()
        except urllib.error.HTTPError as exc:
            data = exc.read()
            if exc.code == 403 and auto_login and retry_on_403:
                self.login()
                return self._raw_request(
                    method,
                    path,
                    params=params,
                    body=body,
                    headers=headers,
                    auto_login=False,
                    retry_on_403=False,
                )
            return exc.code, data

    def request_json(self, method: str, path: str, **kwargs) -> object:
        code, data = self._raw_request(method, path, **kwargs)
        if code != 200:
            text = data.decode("utf-8", errors="ignore")
            raise RuntimeError(f"qB 请求失败：{path} HTTP={code}，返回内容={text}")
        if not data:
            return {}
        return json.loads(data.decode("utf-8", errors="ignore"))

    def seed_exists(self, seed_id: str) -> bool:
        data = self.request_json(
            "GET",
            "/api/v2/torrents/info",
            params={"tag": f"u2id-{seed_id}"},
        )
        return isinstance(data, list) and len(data) > 0

    def current_downloading_count(self) -> int:
        data = self.request_json(
            "GET",
            "/api/v2/torrents/info",
            params={"filter": "downloading"},
        )
        return len(data) if isinstance(data, list) else 0

    def overview(self) -> dict[str, object]:
        data = self.request_json("GET", "/api/v2/sync/maindata", params={"rid": "0"})
        if not isinstance(data, dict):
            return {}
        server_state = data.get("server_state") or {}
        torrents = data.get("torrents") or {}
        return {
            "dl_info_speed": int(server_state.get("dl_info_speed", 0) or 0),
            "up_info_speed": int(server_state.get("up_info_speed", 0) or 0),
            "free_space_on_disk": int(server_state.get("free_space_on_disk", 0) or 0),
            "queued_io_jobs": int(server_state.get("queued_io_jobs", 0) or 0),
            "total_torrents": len(torrents) if isinstance(torrents, dict) else 0,
        }

    def transfer_info(self) -> dict[str, object]:
        data = self.request_json("GET", "/api/v2/transfer/info")
        return data if isinstance(data, dict) else {}

    def all_torrents(self) -> list[dict[str, object]]:
        data = self.request_json("GET", "/api/v2/torrents/info", params={"filter": "all"})
        return data if isinstance(data, list) else []

    def category_totals(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        for torrent in self.all_torrents():
            category = str(torrent.get("category") or "(未分类)")
            item = totals.setdefault(category, {"uploaded": 0, "downloaded": 0, "count": 0})
            item["uploaded"] += int(torrent.get("uploaded", 0) or 0)
            item["downloaded"] += int(torrent.get("downloaded", 0) or 0)
            item["count"] += 1
        return totals

    def snapshot(self) -> dict[str, object]:
        overview = self.overview()
        transfer = self.transfer_info()
        overview["downloading_count"] = self.current_downloading_count()
        overview["alltime_ul"] = int(transfer.get("alltime_ul", 0) or 0)
        overview["alltime_dl"] = int(transfer.get("alltime_dl", 0) or 0)
        overview["categories"] = self.category_totals()
        return overview

    def delete_seed(self, seed_id: str, delete_files: bool = True) -> tuple[bool, str]:
        torrents = self.request_json(
            "GET",
            "/api/v2/torrents/info",
            params={"tag": f"u2id-{seed_id}"},
        )
        if not isinstance(torrents, list) or not torrents:
            return False, "qB 中未找到对应种子"

        hashes = "|".join(str(item.get("hash") or "") for item in torrents if item.get("hash"))
        body = urllib.parse.urlencode(
            {"hashes": hashes, "deleteFiles": "true" if delete_files else "false"}
        ).encode("utf-8")
        code, data = self._raw_request(
            "POST",
            "/api/v2/torrents/delete",
            body=body,
            headers={
                "Referer": self.base_url,
                "Origin": self.base_url,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        text = data.decode("utf-8", errors="ignore")
        return code == 200, text

    def add_torrent_file(self, torrent_path: str, filename: str, seed_id: str, source: str) -> tuple[int, str]:
        fields = {
            "category": self.category,
            "upLimit": str(self.up_limit_bytes),
            "tags": ",".join(
                [
                    "u2",
                    f"u2id-{seed_id}",
                    "u2-auto" if source == "auto" else "u2-manual",
                ]
            ),
        }
        with open(torrent_path, "rb") as fh:
            body, content_type = build_multipart_body(
                fields,
                {"torrents": (filename, fh.read(), "application/x-bittorrent")},
            )
        code, data = self._raw_request(
            "POST",
            "/api/v2/torrents/add",
            body=body,
            headers={
                "Referer": self.base_url,
                "Origin": self.base_url,
                "Content-Type": content_type,
            },
        )
        return code, data.decode("utf-8", errors="ignore")


def build_multipart_body(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----u2qb{uuid.uuid4().hex}"
    buffer = io.BytesIO()
    for key, value in fields.items():
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
        )
        buffer.write(str(value).encode("utf-8"))
        buffer.write(b"\r\n")
    for key, (filename, content, content_type) in files.items():
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(
            (
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        buffer.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        buffer.write(content)
        buffer.write(b"\r\n")
    buffer.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), f"multipart/form-data; boundary={boundary}"


def download_torrent(seed_id: str, passkey: str) -> str:
    url = f"https://u2.dmhy.org/download.php?id={seed_id}&passkey={passkey}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        content = resp.read()
        code = resp.getcode()
    if code != 200:
        raise RuntimeError(f"下载种子文件失败：种子ID={seed_id}，HTTP={code}")
    if not content:
        raise RuntimeError(f"下载到的种子文件为空：种子ID={seed_id}")
    prefix = content[:200].lower()
    if b"<html" in prefix or b"<!doctype" in prefix or b"<body" in prefix:
        raise RuntimeError(f"下载到的内容不是种子文件：种子ID={seed_id}")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".torrent")
    tmp.write(content)
    tmp.close()
    return tmp.name


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def read_tail(path: str, max_lines: int = 200) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return "".join(fh.readlines()[-max_lines:])


def is_process_running(pid_file: str) -> bool:
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except Exception:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def build_occurrence_key(seed_id: str, source: str, matched_text: str) -> str:
    payload = f"{source}\n{seed_id}\n{matched_text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()


def read_text_file(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def write_text_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def strip_jsonc_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*(//|#)", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def load_jsonc_file(path: str) -> dict:
    raw = read_text_file(path)
    if not raw.strip():
        return {}
    return json.loads(strip_jsonc_comments(raw))


def dump_json_pretty(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def parse_event_meta(matched_text: str) -> dict[str, str]:
    text = matched_text or ""
    shout_time = ""
    magic = ""
    seed_name = ""

    m = re.search(r"^\[\s*([^\]]+?)\s*\]", text)
    if m:
        shout_time = m.group(1).strip()

    m = re.search(r"(?:上传|上傳)([0-9.]+)(?:下载|下載)([0-9.]+)", text)
    if m:
        magic = f"上传 {m.group(1)} / 下载 {m.group(2)}"

    m = re.search(r"对种子\s+(.+?)\s+完成了一次", text)
    if m:
        seed_name = m.group(1).strip()

    return {"shout_time": shout_time, "magic": magic, "seed_name": seed_name}


def parse_relative_minutes(text: str) -> int | None:
    if not text:
        return None
    compact = text.replace(" ", "")
    total_seconds = 0
    matched = False
    for pattern, multiplier in [
        (r"(\d+)天", 86400),
        (r"(\d+)小时", 3600),
        (r"(\d+)分钟", 60),
        (r"(\d+)秒", 1),
    ]:
        for m in re.finditer(pattern, compact):
            total_seconds += int(m.group(1)) * multiplier
            matched = True
    if not matched:
        return None
    return int(total_seconds // 60)


def translate_source(source: str) -> str:
    return {"auto": "自动监控", "manual": "手动添加"}.get(source, source)


def translate_result(result: str) -> str:
    mapping = {
        "added": "已加入 qB",
        "exists_in_qb": "qB 已存在",
        "skip_cooldown": "命中冷却时间",
        "skip_max_downloading": "下载数到上限",
        "qb_add_failed": "加种失败",
        "error": "处理失败",
        "ignored_baseline": "首次基线跳过",
        "duplicate_occurrence": "重复事件已跳过",
        "deleted": "已删除种子",
        "delete_failed": "删除失败",
    }
    return mapping.get(result, result)


def summarize_note(result: str, note: str) -> str:
    if note:
        if result == "exists_in_qb":
            return "这个种子已经在 qB 里"
        if result == "skip_cooldown":
            return "刚加过不久，先不重复加入"
        if result == "skip_max_downloading":
            return "当前下载任务数达到上限"
        if result == "ignored_baseline":
            return "这是首次启动时看到的旧消息"
        if result == "deleted":
            return "已从 qB 删除，并删除数据文件"
    return note or "-"


def translate_source(source: str) -> str:
    return {
        "auto": "自动监控",
        "manual": "手动添加",
        "telegram": "Telegram Bot",
    }.get(source, source)


def translate_result(result: str) -> str:
    mapping = {
        "added": "已加入 qB",
        "exists_in_qb": "qB 已存在",
        "skip_cooldown": "命中冷却时间",
        "skip_low_space": "当前剩余空间不足",
        "skip_low_space_after_add": "预计加种后空间不足",
        "skip_max_downloading": "下载数到上限",
        "skip_dynamic_rule": "命中动态规则",
        "qb_add_failed": "加种失败",
        "error": "处理失败",
        "ignored_baseline": "首次基线跳过",
        "duplicate_occurrence": "重复事件已跳过",
        "deleted": "已删除种子",
        "delete_failed": "删除失败",
    }
    return mapping.get(result, result)


def summarize_note(result: str, note: str) -> str:
    if note:
        return note
    if result == "exists_in_qb":
        return "这个种子已经在 qB 里了"
    if result == "skip_cooldown":
        return "24 小时冷却期内不重复加种"
    if result == "skip_low_space":
        return "当前剩余空间已经低于安全线"
    if result == "skip_low_space_after_add":
        return "按种子体积估算，加种后会低于安全线"
    if result == "skip_max_downloading":
        return "当前下载任务数已经达到上限"
    if result == "skip_dynamic_rule":
        return "当前空间档位和魔法/体积规则不允许加入"
    if result == "ignored_baseline":
        return "这是首次启动时看到的旧消息"
    if result == "deleted":
        return "已从 qB 删除，且删除数据文件"
    return "-"


def normalize_seed_ids_input(text: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.search(r"(?:^|id\s*=\s*)(\d+)", line, re.I)
        if not m:
            continue
        seed_id = m.group(1)
        if seed_id not in seen:
            ids.append(seed_id)
            seen.add(seed_id)
    return ids


def parse_magic_rates_from_text(text: str) -> tuple[float | None, float | None]:
    m = re.search(r"(?:上传|上傳)\s*([0-9.]+)\s*(?:下载|下載)\s*([0-9.]+)", text or "")
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def bdecode(data: bytes, index: int = 0):
    token = data[index:index + 1]
    if token == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if token == b"l":
        index += 1
        result = []
        while data[index:index + 1] != b"e":
            item, index = bdecode(data, index)
            result.append(item)
        return result, index + 1
    if token == b"d":
        index += 1
        result = {}
        while data[index:index + 1] != b"e":
            key, index = bdecode(data, index)
            value, index = bdecode(data, index)
            if isinstance(key, bytes):
                try:
                    key = key.decode("utf-8", errors="ignore")
                except Exception:
                    key = str(key)
            result[key] = value
        return result, index + 1
    if token.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end
    raise ValueError("invalid bencode")


def torrent_total_size_bytes(torrent_path: str) -> int:
    with open(torrent_path, "rb") as fh:
        root, _ = bdecode(fh.read(), 0)
    info = root.get("info", {}) if isinstance(root, dict) else {}
    if not isinstance(info, dict):
        return 0
    if "length" in info:
        return int(info.get("length") or 0)
    total = 0
    for item in info.get("files", []) or []:
        if isinstance(item, dict):
            total += int(item.get("length") or 0)
    return total


def load_dynamic_rules(config: dict[str, str]) -> list[dict[str, float | int]]:
    rules = []
    for idx in (1, 2):
        prefix = f"dynamic.rule{idx}"
        enabled = ensure_text(config.get(f"{prefix}.enabled", "0")).lower() in {"1", "true", "yes", "on"}
        if not enabled:
            continue
        try:
            free_space_le_gb = float(ensure_text(config.get(f"{prefix}.free_space_le_gb", "")) or "0")
            min_size_gb = float(ensure_text(config.get(f"{prefix}.min_size_gb", "")) or "0")
            max_size_gb = float(ensure_text(config.get(f"{prefix}.max_size_gb", "")) or "0")
            min_up_rate = float(ensure_text(config.get(f"{prefix}.min_up_rate", "")) or "0")
            max_down_rate = float(ensure_text(config.get(f"{prefix}.max_down_rate", "")) or "999")
        except ValueError:
            continue
        rules.append(
            {
                "index": idx,
                "free_space_le_gb": free_space_le_gb,
                "min_size_gb": min_size_gb,
                "max_size_gb": max_size_gb,
                "min_up_rate": min_up_rate,
                "max_down_rate": max_down_rate,
            }
        )
    rules.sort(key=lambda item: float(item["free_space_le_gb"]))
    return rules


def archive_logs(current_logs: list[str], archive_dir: str, keep_count: int) -> None:
    if keep_count <= 0:
        return
    os.makedirs(archive_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    for log_path in current_logs:
        if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
            continue
        base = os.path.splitext(os.path.basename(log_path))[0]
        target = os.path.join(archive_dir, f"{base}_{stamp}.log")
        with open(log_path, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
    files = sorted(
        [os.path.join(archive_dir, name) for name in os.listdir(archive_dir)],
        key=os.path.getmtime,
        reverse=True,
    )
    for old in files[keep_count:]:
        try:
            os.unlink(old)
        except OSError:
            pass


def load_template_defaults() -> dict[str, object]:
    config_defaults = load_properties(DEFAULT_CONFIG_TEMPLATE) if os.path.exists(DEFAULT_CONFIG_TEMPLATE) else {}
    deploy_defaults = load_jsonc_file(DEFAULT_DEPLOY_TEMPLATE) if os.path.exists(DEFAULT_DEPLOY_TEMPLATE) else {}
    return {"config": config_defaults, "deploy": deploy_defaults}


def build_config_properties_from_form(form: dict[str, str]) -> str:
    magic_rates = []
    for rate in ("1.00", "2.00", "2.33"):
        if ensure_text(form.get(f"magic_rate_{rate.replace('.', '_')}")) == "1":
            magic_rates.append(rate)
    if not magic_rates:
        magic_rates = ["1.00", "2.00", "2.33"]
    magic_use_thresholds = "1" if ensure_text(form.get("magic_use_thresholds")) == "1" else "0"
    dynamic_rule_1_enabled = "1" if ensure_text(form.get("dynamic_rule_1_enabled")) == "1" else "0"
    dynamic_rule_2_enabled = "1" if ensure_text(form.get("dynamic_rule_2_enabled")) == "1" else "0"

    lines = [
        "# ===== U2 =====",
        f"cookiecloud.url={ensure_text(form.get('cookiecloud_url'))}",
        f"cookiecloud.key={ensure_text(form.get('cookiecloud_key'))}",
        f"cookiecloud.password={ensure_text(form.get('cookiecloud_password'))}",
        "",
        f"cookie={ensure_text(form.get('cookie'))}",
        f"cookie_file={ensure_text(form.get('cookie_file'))}",
        f"passkey={ensure_text(form.get('passkey'))}",
        "",
        "# ===== QB =====",
        f"qb.url={ensure_text(form.get('qb_url'))}",
        f"qb.user={ensure_text(form.get('qb_user'))}",
        f"qb.pass={ensure_text(form.get('qb_pass'))}",
        "",
        "# ===== QB Torrent Settings =====",
        f"qb.category={ensure_text(form.get('qb_category'))}",
        f"qb.up_limit_mb={ensure_text(form.get('qb_up_limit_mb'))}",
        f"qb.max_downloading={ensure_text(form.get('qb_max_downloading'))}",
        f"seed.readd_cooldown_minutes={ensure_text(form.get('seed_readd_cooldown_minutes'))}",
        f"shout.max_age_minutes={ensure_text(form.get('shout_max_age_minutes'))}",
        f"qb.min_free_space_gb={ensure_text(form.get('qb_min_free_space_gb'))}",
        f"magic.up_rates={','.join(magic_rates)}",
        f"magic.use_thresholds={magic_use_thresholds}",
        f"magic.min_up_rate={ensure_text(form.get('magic_min_up_rate'))}",
        f"magic.max_down_rate={ensure_text(form.get('magic_max_down_rate'))}",
        f"dynamic.rule1.enabled={dynamic_rule_1_enabled}",
        f"dynamic.rule1.free_space_le_gb={ensure_text(form.get('dynamic_rule_1_free_space_le_gb'))}",
        f"dynamic.rule1.min_size_gb={ensure_text(form.get('dynamic_rule_1_min_size_gb'))}",
        f"dynamic.rule1.max_size_gb={ensure_text(form.get('dynamic_rule_1_max_size_gb'))}",
        f"dynamic.rule1.min_up_rate={ensure_text(form.get('dynamic_rule_1_min_up_rate'))}",
        f"dynamic.rule1.max_down_rate={ensure_text(form.get('dynamic_rule_1_max_down_rate'))}",
        f"dynamic.rule2.enabled={dynamic_rule_2_enabled}",
        f"dynamic.rule2.free_space_le_gb={ensure_text(form.get('dynamic_rule_2_free_space_le_gb'))}",
        f"dynamic.rule2.min_size_gb={ensure_text(form.get('dynamic_rule_2_min_size_gb'))}",
        f"dynamic.rule2.max_size_gb={ensure_text(form.get('dynamic_rule_2_max_size_gb'))}",
        f"dynamic.rule2.min_up_rate={ensure_text(form.get('dynamic_rule_2_min_up_rate'))}",
        f"dynamic.rule2.max_down_rate={ensure_text(form.get('dynamic_rule_2_max_down_rate'))}",
        f"qb.session_refresh_seconds={ensure_text(form.get('qb_session_refresh_seconds'))}",
        "",
        "# ===== Web UI =====",
        f"web.host={ensure_text(form.get('web_host'))}",
        f"web.port={ensure_text(form.get('web_port'))}",
        f"web.username={ensure_text(form.get('web_username'))}",
        f"web.password={ensure_text(form.get('web_password'))}",
        f"web.session_hours={ensure_text(form.get('web_session_hours'))}",
        f"web.qb_refresh_seconds={ensure_text(form.get('web_qb_refresh_seconds'))}",
        f"log.archive_interval_minutes={ensure_text(form.get('log_archive_interval_minutes'))}",
        f"log.archive_keep_count={ensure_text(form.get('log_archive_keep_count'))}",
        "web.token=",
        "",
        "# ===== Telegram =====",
        f"telegram.bot_token={ensure_text(form.get('telegram_bot_token'))}",
        f"telegram.chat_id={ensure_text(form.get('telegram_chat_id'))}",
        f"telegram.poll_seconds={ensure_text(form.get('telegram_poll_seconds'))}",
        "",
        f"poll_interval={ensure_text(form.get('poll_interval'))}",
        "",
    ]
    return "\n".join(lines)


def build_deploy_json_from_form(form: dict[str, str]) -> str:
    magic_rates = []
    for rate in ("1.00", "2.00", "2.33"):
        if ensure_text(form.get(f"magic_rate_{rate.replace('.', '_')}")) == "1":
            magic_rates.append(rate)
    if not magic_rates:
        magic_rates = ["1.00", "2.00", "2.33"]
    magic_use_thresholds = "1" if ensure_text(form.get("magic_use_thresholds")) == "1" else "0"
    dynamic_rule_1_enabled = "1" if ensure_text(form.get("dynamic_rule_1_enabled")) == "1" else "0"
    dynamic_rule_2_enabled = "1" if ensure_text(form.get("dynamic_rule_2_enabled")) == "1" else "0"

    data = {
        "ssh": {
            "host": ensure_text(form.get("ssh_host")),
            "port": int(ensure_text(form.get("ssh_port")) or "22"),
            "user": ensure_text(form.get("ssh_user")),
            "password": ensure_text(form.get("ssh_password")),
        },
        "install_dir": ensure_text(form.get("install_dir")),
        "service_name": ensure_text(form.get("service_name")),
        "u2": {
            "passkey": ensure_text(form.get("passkey")),
            "cookiecloud": {
                "url": ensure_text(form.get("cookiecloud_url")),
                "key": ensure_text(form.get("cookiecloud_key")),
                "password": ensure_text(form.get("cookiecloud_password")),
            },
        },
        "qb": {
            "url": ensure_text(form.get("qb_url")),
            "user": ensure_text(form.get("qb_user")),
            "pass": ensure_text(form.get("qb_pass")),
            "category": ensure_text(form.get("qb_category")),
            "up_limit_mb": int(float(ensure_text(form.get("qb_up_limit_mb")) or "0")),
            "max_downloading": int(ensure_text(form.get("qb_max_downloading")) or "0"),
            "min_free_space_gb": float(ensure_text(form.get("qb_min_free_space_gb")) or "0"),
            "session_refresh_seconds": int(ensure_text(form.get("qb_session_refresh_seconds")) or "1500"),
        },
        "seed": {
            "readd_cooldown_minutes": int(ensure_text(form.get("seed_readd_cooldown_minutes")) or "1440"),
        },
        "shout": {
            "max_age_minutes": int(ensure_text(form.get("shout_max_age_minutes")) or "120"),
        },
        "magic": {
            "up_rates": ",".join(magic_rates),
            "use_thresholds": magic_use_thresholds == "1",
            "min_up_rate": float(ensure_text(form.get("magic_min_up_rate")) or "1.0"),
            "max_down_rate": float(ensure_text(form.get("magic_max_down_rate")) or "0.0"),
        },
        "dynamic": {
            "rule1": {
                "enabled": dynamic_rule_1_enabled == "1",
                "free_space_le_gb": float(ensure_text(form.get("dynamic_rule_1_free_space_le_gb")) or "0"),
                "min_size_gb": float(ensure_text(form.get("dynamic_rule_1_min_size_gb")) or "0"),
                "max_size_gb": float(ensure_text(form.get("dynamic_rule_1_max_size_gb")) or "0"),
                "min_up_rate": float(ensure_text(form.get("dynamic_rule_1_min_up_rate")) or "0"),
                "max_down_rate": float(ensure_text(form.get("dynamic_rule_1_max_down_rate")) or "999"),
            },
            "rule2": {
                "enabled": dynamic_rule_2_enabled == "1",
                "free_space_le_gb": float(ensure_text(form.get("dynamic_rule_2_free_space_le_gb")) or "0"),
                "min_size_gb": float(ensure_text(form.get("dynamic_rule_2_min_size_gb")) or "0"),
                "max_size_gb": float(ensure_text(form.get("dynamic_rule_2_max_size_gb")) or "0"),
                "min_up_rate": float(ensure_text(form.get("dynamic_rule_2_min_up_rate")) or "0"),
                "max_down_rate": float(ensure_text(form.get("dynamic_rule_2_max_down_rate")) or "999"),
            },
        },
        "web": {
            "host": ensure_text(form.get("web_host")),
            "port": int(ensure_text(form.get("web_port")) or "18081"),
            "username": ensure_text(form.get("web_username")),
            "password": ensure_text(form.get("web_password")),
            "session_hours": int(ensure_text(form.get("web_session_hours")) or "12"),
            "qb_refresh_seconds": int(ensure_text(form.get("web_qb_refresh_seconds")) or "5"),
        },
        "log": {
            "archive_interval_minutes": int(ensure_text(form.get("log_archive_interval_minutes")) or "60"),
            "archive_keep_count": int(ensure_text(form.get("log_archive_keep_count")) or "20"),
        },
        "telegram": {
            "bot_token": ensure_text(form.get("telegram_bot_token")),
            "chat_id": ensure_text(form.get("telegram_chat_id")),
            "poll_seconds": int(ensure_text(form.get("telegram_poll_seconds")) or "3"),
        },
        "bot": {
            "poll_interval": int(ensure_text(form.get("poll_interval")) or "60"),
        },
    }
    return dump_json_pretty(data)


def add_seed(
    *,
    config_path: str,
    db_path: str,
    seed_id: str,
    occurrence_key: str,
    source: str,
    matched_text: str,
    force: bool,
    log_file: str | None,
) -> dict[str, str]:
    config = load_properties(config_path)
    db = StateDB(db_path)
    if db.has_occurrence(occurrence_key):
        return {"status": "duplicate_occurrence", "message": f"已跳过重复聊天事件：种子ID={seed_id}"}

    passkey = ensure_text(config.get("passkey", ""))
    if not passkey:
        raise RuntimeError("缺少配置：passkey")

    client = QBClient(config)
    cooldown_minutes = get_int(config, "seed.readd_cooldown_minutes", 60)
    min_free_space_gb = float(ensure_text(config.get("qb.min_free_space_gb", "0")) or "0")
    dynamic_rules = load_dynamic_rules(config)
    up_rate, down_rate = parse_magic_rates_from_text(matched_text)

    try:
        snapshot = client.snapshot()
        free_space_bytes = int(snapshot.get("free_space_on_disk", 0) or 0)
        free_space_gb = free_space_bytes / (1024 ** 3)

        if not force and min_free_space_gb > 0 and free_space_gb < min_free_space_gb:
            db.record_event(
                occurrence_key,
                seed_id,
                source,
                matched_text,
                "skip",
                "skip_low_space",
                f"剩余硬盘 {free_space_gb:.1f} GB，小于最小安全空间 {min_free_space_gb:.1f} GB",
            )
            message = f"剩余硬盘过低，跳过加种：种子ID={seed_id}"
            log_message(message, log_file)
            return {"status": "skip_low_space", "message": message}

        if not force and client.seed_exists(seed_id):
            db.record_event(
                occurrence_key,
                seed_id,
                source,
                matched_text,
                "skip",
                "exists_in_qb",
                "qB 中已存在同 seed_id 标签的种子",
            )
            message = f"qB 中已存在，跳过本次加种：种子ID={seed_id}"
            log_message(message, log_file)
            return {"status": "exists_in_qb", "message": message}

        if not force and cooldown_minutes > 0:
            last_success = parse_timestamp(db.last_success_at(seed_id))
            if last_success is not None:
                age_minutes = (dt.datetime.now() - last_success).total_seconds() / 60.0
                if age_minutes < cooldown_minutes:
                    db.record_event(
                        occurrence_key,
                        seed_id,
                        source,
                        matched_text,
                        "skip",
                        "skip_cooldown",
                        f"距离上次成功加种仅 {age_minutes:.1f} 分钟，小于冷却时间 {cooldown_minutes} 分钟",
                    )
                    message = f"命中冷却时间，跳过本次加种：种子ID={seed_id}"
                    log_message(message, log_file)
                    return {"status": "skip_cooldown", "message": message}

        if not force and client.max_downloading > 0:
            current_downloading = client.current_downloading_count()
            if current_downloading >= client.max_downloading:
                db.record_event(
                    occurrence_key,
                    seed_id,
                    source,
                    matched_text,
                    "skip",
                    "skip_max_downloading",
                    f"当前下载数 {current_downloading} 已达到上限 {client.max_downloading}",
                )
                message = f"当前下载数达到上限，跳过本次加种：种子ID={seed_id}"
                log_message(message, log_file)
                return {"status": "skip_max_downloading", "message": message}

        log_message(f"开始加入种子：种子ID={seed_id}，来源={source}", log_file)
        torrent_path = download_torrent(seed_id, passkey)
        try:
            torrent_size_bytes = torrent_total_size_bytes(torrent_path)
            torrent_size_gb = torrent_size_bytes / (1024 ** 3) if torrent_size_bytes > 0 else 0.0

            if not force and dynamic_rules:
                for rule in dynamic_rules:
                    if free_space_gb > float(rule["free_space_le_gb"]):
                        continue
                    min_size = float(rule["min_size_gb"])
                    max_size = float(rule["max_size_gb"])
                    min_up = float(rule["min_up_rate"])
                    max_down = float(rule["max_down_rate"])
                    size_ok = (min_size <= 0 or torrent_size_gb >= min_size) and (max_size <= 0 or torrent_size_gb <= max_size)
                    rate_ok = (up_rate is None or up_rate >= min_up) and (down_rate is None or down_rate <= max_down)
                    if not (size_ok and rate_ok):
                        db.record_event(
                            occurrence_key,
                            seed_id,
                            source,
                            matched_text,
                            "skip",
                            "skip_dynamic_rule",
                            f"触发动态规则{int(rule['index'])}，剩余空间 {free_space_gb:.1f} GB，种子大小 {torrent_size_gb:.1f} GB，不满足当前档位要求",
                        )
                        message = f"触发动态空间规则，跳过加种：种子ID={seed_id}"
                        log_message(message, log_file)
                        return {"status": "skip_dynamic_rule", "message": message}
                    break

            code, body = client.add_torrent_file(torrent_path, f"{seed_id}.torrent", seed_id, source)
        finally:
            try:
                os.unlink(torrent_path)
            except OSError:
                pass

        if code != 200:
            db.record_event(
                occurrence_key,
                seed_id,
                source,
                matched_text,
                "add",
                "qb_add_failed",
                f"qB 返回 HTTP={code} 内容={body}",
            )
            message = f"上传到 qB 失败：种子ID={seed_id}，HTTP={code}，返回内容={body}"
            log_message(message, log_file)
            return {"status": "qb_add_failed", "message": message}

        db.record_event(
            occurrence_key,
            seed_id,
            source,
            matched_text,
            "add",
            "added",
            f"qB 返回 HTTP={code} 内容={body}",
        )
        message = f"已成功上传到 qB：种子ID={seed_id}"
        log_message(message, log_file)
        return {"status": "added", "message": message}
    except Exception as exc:
        db.record_event(
            occurrence_key,
            seed_id,
            source,
            matched_text,
            "add",
            "error",
            str(exc),
        )
        log_message(f"处理种子失败：种子ID={seed_id}，错误={exc}", log_file)
        return {"status": "error", "message": str(exc)}


def add_seed(
    *,
    config_path: str,
    db_path: str,
    seed_id: str,
    occurrence_key: str,
    source: str,
    matched_text: str,
    force: bool,
    log_file: str | None,
) -> dict[str, str]:
    config = load_properties(config_path)
    db = StateDB(db_path)
    if db.has_occurrence(occurrence_key):
        return {"status": "duplicate_occurrence", "message": f"重复事件已跳过：种子ID={seed_id}"}

    passkey = ensure_text(config.get("passkey", ""))
    if not passkey:
        raise RuntimeError("缺少配置：passkey")

    client = QBClient(config)
    cooldown_minutes = get_int(config, "seed.readd_cooldown_minutes", 1440)
    min_free_space_gb = get_float(config, "qb.min_free_space_gb", 0.0)
    dynamic_rules = load_dynamic_rules(config)
    up_rate, down_rate = parse_magic_rates_from_text(matched_text)

    try:
        snapshot = client.snapshot()
        free_space_bytes = int(snapshot.get("free_space_on_disk", 0) or 0)
        free_space_gb = free_space_bytes / (1024 ** 3)

        if not force and min_free_space_gb > 0 and free_space_gb < min_free_space_gb:
            note = f"当前剩余空间 {free_space_gb:.1f} GB，低于安全线 {min_free_space_gb:.1f} GB"
            db.record_event(occurrence_key, seed_id, source, matched_text, "skip", "skip_low_space", note)
            message = f"剩余空间过低，跳过加种：种子ID={seed_id}"
            log_message(message, log_file)
            return {"status": "skip_low_space", "message": message}

        if not force and client.seed_exists(seed_id):
            note = "qB 中已存在带 u2id 标签的同种子"
            db.record_event(occurrence_key, seed_id, source, matched_text, "skip", "exists_in_qb", note)
            message = f"qB 中已存在，跳过本次加种：种子ID={seed_id}"
            log_message(message, log_file)
            return {"status": "exists_in_qb", "message": message}

        if not force and cooldown_minutes > 0:
            last_success = parse_timestamp(db.last_success_at(seed_id))
            if last_success is not None:
                age_minutes = (dt.datetime.now() - last_success).total_seconds() / 60.0
                if age_minutes < cooldown_minutes:
                    note = f"距离上次成功加种仅 {age_minutes:.1f} 分钟，小于冷却时间 {cooldown_minutes} 分钟"
                    db.record_event(occurrence_key, seed_id, source, matched_text, "skip", "skip_cooldown", note)
                    message = f"24 小时冷却期内，跳过本次加种：种子ID={seed_id}"
                    log_message(message, log_file)
                    return {"status": "skip_cooldown", "message": message}

        if not force and client.max_downloading > 0:
            current_downloading = client.current_downloading_count()
            if current_downloading >= client.max_downloading:
                note = f"当前下载数 {current_downloading} 已达到上限 {client.max_downloading}"
                db.record_event(occurrence_key, seed_id, source, matched_text, "skip", "skip_max_downloading", note)
                message = f"当前下载任务数到上限，跳过本次加种：种子ID={seed_id}"
                log_message(message, log_file)
                return {"status": "skip_max_downloading", "message": message}

        log_message(f"开始加入种子：种子ID={seed_id}，来源={source}", log_file)
        torrent_path = download_torrent(seed_id, passkey)
        try:
            torrent_size_bytes = torrent_total_size_bytes(torrent_path)
            torrent_size_gb = torrent_size_bytes / (1024 ** 3) if torrent_size_bytes > 0 else 0.0
            projected_free_gb = free_space_gb - torrent_size_gb

            if not force and min_free_space_gb > 0 and projected_free_gb < min_free_space_gb:
                note = (
                    f"当前剩余 {free_space_gb:.1f} GB，种子约 {torrent_size_gb:.1f} GB，"
                    f"预计加入后剩余 {projected_free_gb:.1f} GB，低于安全线 {min_free_space_gb:.1f} GB"
                )
                db.record_event(
                    occurrence_key,
                    seed_id,
                    source,
                    matched_text,
                    "skip",
                    "skip_low_space_after_add",
                    note,
                )
                message = f"按种子体积估算会压低可用空间，跳过加种：种子ID={seed_id}"
                log_message(message, log_file)
                return {"status": "skip_low_space_after_add", "message": message}

            if not force and dynamic_rules:
                for rule in dynamic_rules:
                    if free_space_gb > float(rule["free_space_le_gb"]):
                        continue
                    min_size = float(rule["min_size_gb"])
                    max_size = float(rule["max_size_gb"])
                    min_up = float(rule["min_up_rate"])
                    max_down = float(rule["max_down_rate"])
                    size_ok = (min_size <= 0 or torrent_size_gb >= min_size) and (
                        max_size <= 0 or torrent_size_gb <= max_size
                    )
                    rate_ok = (up_rate is None or up_rate >= min_up) and (
                        down_rate is None or down_rate <= max_down
                    )
                    if not (size_ok and rate_ok):
                        note = (
                            f"触发动态规则{int(rule['index'])}，剩余空间 {free_space_gb:.1f} GB，"
                            f"种子大小 {torrent_size_gb:.1f} GB，不满足当前档位要求"
                        )
                        db.record_event(
                            occurrence_key,
                            seed_id,
                            source,
                            matched_text,
                            "skip",
                            "skip_dynamic_rule",
                            note,
                        )
                        message = f"触发动态规则，跳过加种：种子ID={seed_id}"
                        log_message(message, log_file)
                        return {"status": "skip_dynamic_rule", "message": message}
                    break

            code, body = client.add_torrent_file(torrent_path, f"{seed_id}.torrent", seed_id, source)
        finally:
            try:
                os.unlink(torrent_path)
            except OSError:
                pass

        if code != 200:
            note = f"qB 返回 HTTP={code} 内容={body}"
            db.record_event(occurrence_key, seed_id, source, matched_text, "add", "qb_add_failed", note)
            message = f"上传到 qB 失败：种子ID={seed_id}，HTTP={code}，返回内容={body}"
            log_message(message, log_file)
            return {"status": "qb_add_failed", "message": message}

        note = f"qB 返回 HTTP={code} 内容={body}"
        db.record_event(occurrence_key, seed_id, source, matched_text, "add", "added", note)
        message = f"已成功上传到 qB：种子ID={seed_id}"
        log_message(message, log_file)
        return {"status": "added", "message": message}
    except Exception as exc:
        db.record_event(occurrence_key, seed_id, source, matched_text, "add", "error", str(exc))
        log_message(f"处理种子失败：种子ID={seed_id}，错误：{exc}", log_file)
        return {"status": "error", "message": str(exc)}


def delete_seed_from_qb(
    *,
    config_path: str,
    db_path: str,
    seed_id: str,
    log_file: str | None,
) -> dict[str, str]:
    config = load_properties(config_path)
    db = StateDB(db_path)
    client = QBClient(config)
    try:
        ok, message = client.delete_seed(seed_id, delete_files=True)
        if ok:
            db.record_event(
                occurrence_key=f"delete:{seed_id}:{uuid.uuid4().hex}",
                seed_id=seed_id,
                source="manual",
                matched_text="手动删除种子与文件",
                action="delete",
                result="deleted",
                note="已从 qB 删除，并删除数据文件",
            )
            log_message(f"已删除种子和文件：种子ID={seed_id}", log_file)
            return {"status": "deleted", "message": f"已删除种子和文件：种子ID={seed_id}"}

        db.record_event(
            occurrence_key=f"delete-failed:{seed_id}:{uuid.uuid4().hex}",
            seed_id=seed_id,
            source="manual",
            matched_text="手动删除种子与文件",
            action="delete",
            result="delete_failed",
            note=message,
        )
        return {"status": "delete_failed", "message": f"删除失败：{message}"}
    except Exception as exc:
        return {"status": "delete_failed", "message": str(exc)}


def parse_event_meta(matched_text: str) -> dict[str, str]:
    text = matched_text or ""
    shout_time = ""
    magic = ""
    seed_name = ""

    m = re.search(r"^\[\s*([^\]]+?)\s*\]", text)
    if m:
        shout_time = m.group(1).strip()

    m = re.search(r"(?:上传|上傳)\s*([0-9.]+)\s*(?:下载|下載)\s*([0-9.]+)", text)
    if m:
        magic = f"上传 {m.group(1)} / 下载 {m.group(2)}"

    m = re.search(r"(?:对种子|對種子)\s+(.+?)\s+完成了一次", text)
    if m:
        seed_name = m.group(1).strip()

    return {"shout_time": shout_time, "magic": magic, "seed_name": seed_name}


def parse_relative_minutes(text: str) -> int | None:
    if not text:
        return None
    compact = text.replace(" ", "")
    total_seconds = 0
    matched = False
    for pattern, multiplier in [
        (r"(\d+)天前", 86400),
        (r"(\d+)天", 86400),
        (r"(\d+)小时前", 3600),
        (r"(\d+)小时", 3600),
        (r"(\d+)分鐘前", 60),
        (r"(\d+)分鐘", 60),
        (r"(\d+)分钟前", 60),
        (r"(\d+)分钟", 60),
        (r"(\d+)秒前", 1),
        (r"(\d+)秒", 1),
    ]:
        for m in re.finditer(pattern, compact):
            total_seconds += int(m.group(1)) * multiplier
            matched = True
    if not matched:
        return None
    return int(total_seconds // 60)


def parse_magic_rates_from_text(text: str) -> tuple[float | None, float | None]:
    m = re.search(r"(?:上传|上傳)\s*([0-9.]+)\s*(?:下载|下載)\s*([0-9.]+)", text or "")
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def format_event_brief(row: sqlite3.Row) -> str:
    meta = parse_event_meta(row["matched_text"])
    magic = meta["magic"] or "-"
    return (
        f"{row['created_at']} | #{row['seed_id']} | {magic} | "
        f"{translate_source(row['source'])} | {translate_result(row['result'])}"
    )


def build_recent_summary_text(db_path: str, limit: int = 8) -> str:
    rows = StateDB(db_path).recent_events(hours=48, limit=limit)
    if not rows:
        return "最近 48 小时还没有事件记录。"
    lines = ["最近事件："]
    for row in rows:
        lines.append(f"- {format_event_brief(row)}")
    return "\n".join(lines)


def build_qb_summary_text(config_path: str) -> str:
    config = load_properties(config_path)
    snapshot = QBClient(config).snapshot()
    free_space = int(snapshot.get("free_space_on_disk", 0) or 0)
    downloading = int(snapshot.get("downloading_count", 0) or 0)
    total = int(snapshot.get("total_torrents", 0) or 0)
    max_downloading = get_int(config, "qb.max_downloading", 0)
    limit_text = "不限" if max_downloading <= 0 else str(max_downloading)
    return "\n".join(
        [
            "qB 状态：",
            f"- 剩余空间：{format_bytes(free_space)}",
            f"- 当前下载：{downloading} / {limit_text}",
            f"- 总种子数：{total}",
        ]
    )


def build_seed_status_text(config_path: str, db_path: str, seed_id: str) -> str:
    db = StateDB(db_path)
    rows = db.recent_events_for_seed(seed_id, limit=5)
    lines = [f"种子 #{seed_id} 状态："]

    try:
        exists_in_qb = QBClient(load_properties(config_path)).seed_exists(seed_id)
        lines.append(f"- qB 中状态：{'已存在' if exists_in_qb else '当前不在 qB'}")
    except Exception as exc:
        lines.append(f"- qB 中状态：检查失败（{exc}）")

    if not rows:
        lines.append("- 历史记录：暂无")
        return "\n".join(lines)

    latest = rows[0]
    latest_meta = parse_event_meta(latest["matched_text"])
    lines.append(f"- 最近结果：{translate_result(latest['result'])}")
    lines.append(f"- 最近来源：{translate_source(latest['source'])}")
    lines.append(f"- 最近时间：{latest['created_at']}")
    if latest_meta["magic"]:
        lines.append(f"- 最近魔法：{latest_meta['magic']}")
    if latest_meta["shout_time"]:
        lines.append(f"- 聊天时间：{latest_meta['shout_time']}")
    if latest_meta["seed_name"]:
        lines.append(f"- 种子名称：{latest_meta['seed_name']}")
    if latest["note"]:
        lines.append(f"- 说明：{latest['note']}")

    lines.append("最近记录：")
    for row in rows:
        lines.append(f"- {format_event_brief(row)}")
    return "\n".join(lines)


def generate_health_report(config_path: str, db_path: str) -> tuple[str, list[str]]:
    config = load_properties(config_path)
    warnings: list[str] = []
    errors: list[str] = []
    lines: list[str] = []

    cooldown_minutes = get_int(config, "seed.readd_cooldown_minutes", 1440)
    min_free_space_gb = get_float(config, "qb.min_free_space_gb", 0.0)
    max_downloading = get_int(config, "qb.max_downloading", 0)
    poll_interval = get_int(config, "poll_interval", 60)
    telegram_token = ensure_text(config.get("telegram.bot_token", ""))
    telegram_chat_id = ensure_text(config.get("telegram.chat_id", ""))

    if cooldown_minutes < 1440:
        warnings.append(f"seed.readd_cooldown_minutes 当前为 {cooldown_minutes}，建议至少 1440")
    else:
        lines.append(f"[OK] 24 小时去重冷却已开启：{cooldown_minutes} 分钟")

    if min_free_space_gb <= 0:
        warnings.append("qb.min_free_space_gb 未设置，无法防止磁盘被压满")
    else:
        lines.append(f"[OK] 最小安全剩余空间：{min_free_space_gb:.1f} GB")

    if max_downloading <= 0:
        warnings.append("qb.max_downloading 仍为 0（不限），高峰时可能堆积过多下载任务")
    else:
        lines.append(f"[OK] 最大并发下载任务数：{max_downloading}")

    if poll_interval <= 0:
        errors.append("poll_interval 必须大于 0")
    else:
        lines.append(f"[OK] 轮询间隔：{poll_interval} 秒")

    if not telegram_token or not telegram_chat_id:
        warnings.append("Telegram Bot 未完整配置，手机端暂时不能直接加种/查状态")
    else:
        try:
            info = telegram_api_request(telegram_token, "getMe")
            bot_user = (
                info.get("result", {}).get("username")
                if isinstance(info.get("result"), dict)
                else ""
            )
            lines.append(f"[OK] Telegram Bot 配置可用：@{bot_user or 'unknown'}")
        except Exception as exc:
            warnings.append(f"Telegram Bot 配置已填写，但校验失败：{exc}")

    dynamic_rules = load_dynamic_rules(config)
    if dynamic_rules:
        lines.append(f"[OK] 已启用动态规则：{len(dynamic_rules)} 条")
    else:
        warnings.append("动态规则未启用，当前只靠固定安全空间阈值兜底")

    try:
        snapshot = QBClient(config).snapshot()
        free_space = int(snapshot.get("free_space_on_disk", 0) or 0)
        downloading = int(snapshot.get("downloading_count", 0) or 0)
        total = int(snapshot.get("total_torrents", 0) or 0)
        lines.append(f"[OK] qB 可连接，剩余空间 {format_bytes(free_space)}，下载中 {downloading}，总种子 {total}")
        if min_free_space_gb > 0 and free_space < int(min_free_space_gb * 1024 ** 3):
            warnings.append("qB 当前剩余空间已经低于配置的安全线")
    except Exception as exc:
        errors.append(f"qB 检查失败：{exc}")

    db = StateDB(db_path)
    summary = db.summary()
    lines.append(
        f"[OK] state.db 可读，累计事件 {summary['total']}，成功 {summary['added']}，跳过 {summary['skipped']}"
    )

    if errors:
        status = "ERROR"
    elif warnings:
        status = "WARN"
    else:
        status = "OK"

    report_lines = [f"健康检查结果：{status}"] + lines
    report_lines.extend(f"[WARN] {item}" for item in warnings)
    report_lines.extend(f"[ERROR] {item}" for item in errors)
    return status, report_lines


def telegram_api_request(
    token: str,
    method: str,
    payload: dict[str, object] | None = None,
    *,
    timeout: int = 30,
) -> dict[str, object]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        encoded = {}
        for key, value in payload.items():
            encoded[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
        data = urllib.parse.urlencode(encoded).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    result = json.loads(body)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description") or f"Telegram API 调用失败：{method}"))
    return result


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    telegram_api_request(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        },
    )


def load_telegram_offset(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or "0")
    except (OSError, ValueError):
        return 0


def save_telegram_offset(path: str, offset: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(offset))


def handle_telegram_command(text: str, config_path: str, db_path: str) -> str:
    first_line = ensure_text((text or "").splitlines()[0] if text else "")
    if not first_line:
        return "请输入命令。可用：/help"

    parts = first_line.split()
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if re.fullmatch(r"\d+", command):
        arg = command
        command = "/add"

    if command in {"/start", "/help"}:
        return "\n".join(
            [
                "可用命令：",
                "/add 123456  直接加种",
                "/force 123456  强制加种",
                "/status 123456  查看种子最近状态和魔法",
                "/recent  查看最近事件",
                "/qb  查看 qB 空间和下载数",
                "/check  检查 VPS 配置是否安全",
            ]
        )

    if command in {"/add", "/force"}:
        if not re.fullmatch(r"\d+", arg):
            return "用法：/add 123456 或 /force 123456"
        result = add_seed(
            config_path=config_path,
            db_path=db_path,
            seed_id=arg,
            occurrence_key=f"telegram:{arg}:{int(time.time())}",
            source="telegram",
            matched_text="Telegram Bot 手动加种",
            force=command == "/force",
            log_file=DEFAULT_LOG,
        )
        return f"{translate_result(result['status'])}\n{result['message']}"

    if command == "/status":
        if not re.fullmatch(r"\d+", arg):
            return "用法：/status 123456"
        return build_seed_status_text(config_path, db_path, arg)

    if command == "/recent":
        return build_recent_summary_text(db_path)

    if command == "/qb":
        try:
            return build_qb_summary_text(config_path)
        except Exception as exc:
            return f"qB 状态检查失败：{exc}"

    if command == "/check":
        _, lines = generate_health_report(config_path, db_path)
        return "\n".join(lines[:20])

    return "未知命令。发送 /help 查看可用命令。"


def run_telegram_bot(
    *,
    config_path: str,
    db_path: str,
    offset_file: str,
    log_file: str | None,
) -> None:
    config = load_properties(config_path)
    token = ensure_text(config.get("telegram.bot_token", ""))
    chat_id = ensure_text(config.get("telegram.chat_id", ""))
    poll_seconds = max(2, get_int(config, "telegram.poll_seconds", 3))
    if not token or not chat_id:
        raise RuntimeError("缺少 Telegram 配置：telegram.bot_token 或 telegram.chat_id")

    me = telegram_api_request(token, "getMe")
    bot_name = (
        me.get("result", {}).get("username")
        if isinstance(me.get("result"), dict)
        else ""
    )
    log_message(f"Telegram Bot 已启动：@{bot_name or 'unknown'}", log_file)

    offset = load_telegram_offset(offset_file)
    while True:
        try:
            payload = {"timeout": 20}
            if offset > 0:
                payload["offset"] = offset
            result = telegram_api_request(token, "getUpdates", payload, timeout=35)
            updates = result.get("result", [])
            if not isinstance(updates, list):
                updates = []
            for item in updates:
                if not isinstance(item, dict):
                    continue
                update_id = int(item.get("update_id", 0) or 0)
                message = item.get("message") or {}
                if not isinstance(message, dict):
                    continue
                offset = max(offset, update_id + 1)
                save_telegram_offset(offset_file, offset)

                text = ensure_text(message.get("text"))
                incoming_chat_id = str((message.get("chat") or {}).get("id") or "")
                if not text or incoming_chat_id != chat_id:
                    continue

                reply = handle_telegram_command(text, config_path, db_path)
                send_telegram_message(token, incoming_chat_id, reply)

            if not updates:
                time.sleep(poll_seconds)
        except Exception as exc:
            log_message(f"Telegram Bot 轮询失败：{exc}", log_file)
            time.sleep(max(5, poll_seconds))


class WebApp:
    def __init__(
        self,
        *,
        config_path: str,
        db_path: str,
        log_file: str,
        deploy_file: str,
        pid_file: str,
        service_script: str,
    ) -> None:
        self.config_path = config_path
        self.db_path = db_path
        self.log_file = log_file
        self.deploy_file = deploy_file
        self.pid_file = pid_file
        self.service_script = service_script
        self.lock = threading.Lock()
        self.sessions: dict[str, dt.datetime] = {}
        self.latest_qb: dict[str, object] = {}
        self.sampler_started = False

    def current_config(self) -> dict[str, str]:
        return load_properties(self.config_path)

    def auth_token(self) -> str:
        return ensure_text(self.current_config().get("web.token", ""))

    def auth_username(self) -> str:
        return ensure_text(self.current_config().get("web.username", "")) or "admin"

    def auth_password(self) -> str:
        password = ensure_text(self.current_config().get("web.password", ""))
        if password:
            return password
        token = self.auth_token()
        if token:
            return token
        return ""

    def session_hours(self) -> int:
        return max(1, get_int(self.current_config(), "web.session_hours", 12))

    def host(self) -> str:
        return ensure_text(self.current_config().get("web.host", "0.0.0.0")) or "0.0.0.0"

    def port(self) -> int:
        return get_int(self.current_config(), "web.port", 18081)

    def qb_refresh_seconds(self) -> int:
        return max(2, get_int(self.current_config(), "web.qb_refresh_seconds", 5))

    def current_deploy_config(self) -> dict:
        return load_jsonc_file(self.deploy_file)

    def current_magic_rates(self) -> set[str]:
        raw = ensure_text(self.current_config().get("magic.up_rates", "1.00,2.00,2.33"))
        return {part.strip() for part in raw.split(",") if part.strip()}

    def sample_qb_status(self) -> tuple[dict[str, object], str]:
        config = self.current_config()
        try:
            client = QBClient(config)
            snapshot = client.snapshot()
            StateDB(self.db_path).record_qb_snapshot(snapshot)
            self.latest_qb = snapshot
            return snapshot, ""
        except Exception as exc:
            return self.latest_qb, str(exc)

    def dashboard_data(self) -> dict[str, object]:
        db = StateDB(self.db_path)
        config = self.current_config()
        deploy = self.current_deploy_config()
        defaults = load_template_defaults()
        qb_info, qb_error = self.sample_qb_status()
        today_stats = db.qb_stats_for_period("day")
        month_stats = db.qb_stats_for_period("month")

        return {
            "summary": db.summary(),
            "events": db.recent_events(hours=24, limit=80),
            "log_tail": read_tail(self.log_file, 120),
            "config_text": read_text_file(self.config_path),
            "deploy_text": read_text_file(self.deploy_file),
            "config_template_text": read_text_file(DEFAULT_CONFIG_TEMPLATE),
            "deploy_template_text": read_text_file(DEFAULT_DEPLOY_TEMPLATE),
            "config": config,
            "deploy": deploy,
            "defaults": defaults,
            "bot_running": is_process_running(self.pid_file),
            "qb_info": qb_info,
            "qb_error": qb_error,
            "web_port": self.port(),
            "qb_refresh_seconds": self.qb_refresh_seconds(),
            "today_stats": today_stats,
            "month_stats": month_stats,
            "magic_rates": self.current_magic_rates(),
        }

    def validate_login(self, username: str, password: str) -> bool:
        return username == self.auth_username() and password == self.auth_password()

    def create_session(self) -> tuple[str, int]:
        session_id = secrets.token_urlsafe(32)
        max_age = self.session_hours() * 3600
        self.sessions[session_id] = dt.datetime.now() + dt.timedelta(seconds=max_age)
        self.cleanup_sessions()
        return session_id, max_age

    def cleanup_sessions(self) -> None:
        now = dt.datetime.now()
        expired = [sid for sid, expires_at in self.sessions.items() if expires_at <= now]
        for sid in expired:
            self.sessions.pop(sid, None)

    def is_valid_session(self, session_id: str) -> bool:
        self.cleanup_sessions()
        expires_at = self.sessions.get(session_id)
        return expires_at is not None and expires_at > dt.datetime.now()

    def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def start_background_sampler(self) -> None:
        if self.sampler_started:
            return
        self.sampler_started = True

        def _loop() -> None:
            while True:
                try:
                    self.sample_qb_status()
                except Exception:
                    pass
                time.sleep(self.qb_refresh_seconds())

        thread = threading.Thread(target=_loop, daemon=True, name="qb-sampler")
        thread.start()

    def start_log_archiver(self) -> None:
        interval = max(0, get_int(self.current_config(), "log.archive_interval_minutes", 60))
        keep_count = max(0, get_int(self.current_config(), "log.archive_keep_count", 20))
        if interval <= 0 or keep_count <= 0:
            return

        def _loop() -> None:
            while True:
                try:
                    archive_logs(
                        [
                            self.log_file,
                            os.path.join(ROOT_DIR, "u2_web.log"),
                            os.path.join(ROOT_DIR, "u2_telegram.log"),
                        ],
                        DEFAULT_ARCHIVE_DIR,
                        keep_count,
                    )
                except Exception:
                    pass
                time.sleep(interval * 60)

        threading.Thread(target=_loop, daemon=True, name="log-archiver").start()

    def schedule_restart(self, restart_bot: bool, restart_web: bool, restart_telegram: bool) -> None:
        if not restart_bot and not restart_web and not restart_telegram:
            return
        if os.name == "nt":
            return

        service_script = os.path.abspath(self.service_script)
        workdir = os.path.dirname(service_script)

        def _worker() -> None:
            time.sleep(1)
            try:
                if restart_bot:
                    subprocess.run(["bash", service_script, "restart"], cwd=workdir, check=False)
                if restart_web:
                    subprocess.run(["bash", service_script, "web-restart"], cwd=workdir, check=False)
                if restart_telegram:
                    subprocess.run(["bash", service_script, "telegram-restart"], cwd=workdir, check=False)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="service-restart").start()


def render_login_page(app: WebApp, message: str = "") -> str:
    msg_html = ""
    if message:
        msg_html = f'<div style="margin-bottom:12px;color:#9a3412;">{html.escape(message)}</div>'
    username_hint = html.escape(app.auth_username())
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>登录控制台</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background:
      radial-gradient(circle at top, #ffd48a 0, transparent 30%),
      linear-gradient(180deg, #f8f5ed 0%, #efe7d6 100%); font-family: "Segoe UI", "PingFang SC", sans-serif; }}
    .card {{ width: min(460px, calc(100vw - 32px)); background: rgba(255,255,255,.94); border: 1px solid #d6d3c7; border-radius: 20px; padding: 26px; box-shadow: 0 22px 50px rgba(0,0,0,.10); }}
    .eyebrow {{ color: #a16207; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }}
    h1 {{ margin: 8px 0 10px; }}
    p {{ color: #4b5563; }}
    label {{ display: block; margin: 14px 0 6px; color: #374151; font-size: 14px; }}
    input {{ width: 100%; padding: 12px; border-radius: 12px; border: 1px solid #d6d3c7; box-sizing: border-box; background: #fffdf8; }}
    button {{ margin-top: 16px; width: 100%; padding: 12px; border: 0; border-radius: 12px; background: #1f6f5f; color: #fff; font-weight: 600; }}
    .hint {{ margin-top: 12px; color: #6b7280; font-size: 13px; }}
  </style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <div class="eyebrow">U2 Control Panel</div>
    <h1>登录控制台</h1>
    <p>请输入 Web 管理账号和密码。</p>
    {msg_html}
    <label for="username">用户名</label>
    <input id="username" type="text" name="username" value="{username_hint}" autocomplete="username" />
    <label for="password">密码</label>
    <input id="password" type="password" name="password" autocomplete="current-password" />
    <button type="submit">登录</button>
    <div class="hint">会话默认有效 {app.session_hours()} 小时。退出后需要重新登录。</div>
  </form>
</body>
</html>"""


def render_dashboard(app: WebApp, flash: str = "", flash_level: str = "info") -> str:
    data = app.dashboard_data()
    summary = data["summary"]
    qb_info = data["qb_info"]
    qb_error = data["qb_error"]
    events = data["events"]
    today_stats = data["today_stats"]
    month_stats = data["month_stats"]
    config = data["config"]
    deploy = data["deploy"]
    magic_rates = data["magic_rates"]
    magic_defaults = data["defaults"].get("magic", {}) if isinstance(data["defaults"], dict) else {}

    def deploy_get(*keys: str, default: str = "") -> str:
        current = deploy
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
        return "" if current is None else str(current)

    flash_html = ""
    if flash:
        color = "#14532d" if flash_level == "success" else "#7c2d12" if flash_level == "error" else "#1e3a8a"
        bg = "#dcfce7" if flash_level == "success" else "#ffedd5" if flash_level == "error" else "#dbeafe"
        flash_html = f'<div class="flash" style="background:{bg};color:{color};">{html.escape(flash)}</div>'

    event_rows = []
    for row in events:
        seed_id = row["seed_id"]
        meta = parse_event_meta(row["matched_text"] or "")
        seed_name = meta["seed_name"] or "-"
        shout_minutes = parse_relative_minutes(meta["shout_time"] or "")
        result_text = translate_result(row["result"])
        source_text = translate_source(row["source"])
        magic_key = "-"
        if meta["magic"]:
            magic_key = meta["magic"].split("/")[0].replace("上传", "").strip()
        event_rows.append(
            f"""
            <tr data-search="{html.escape((seed_id + ' ' + seed_name + ' ' + (row['matched_text'] or '')).lower())}" data-minutes="{'' if shout_minutes is None else shout_minutes}" data-magic="{html.escape(magic_key)}" data-source="{html.escape(source_text)}" data-result="{html.escape(result_text)}">
              <td>{html.escape(row["created_at"])}</td>
              <td>{html.escape(meta["shout_time"] or "-")}</td>
              <td>{html.escape(meta["magic"] or "-")}</td>
              <td>{html.escape(seed_id)}</td>
              <td><div class="seed-name" title="{html.escape(seed_name)}">{html.escape(seed_name)}</div></td>
              <td>{html.escape(source_text)}</td>
              <td>{html.escape(result_text)}</td>
              <td>{html.escape(summarize_note(row["result"], row["note"] or ""))}</td>
              <td class="text" title="{html.escape(row["matched_text"] or "")}">{html.escape(row["matched_text"] or "")}</td>
              <td>
                <form method="post" action="/action/add" class="inline">
                  <input type="hidden" name="seed_input" value="{html.escape(seed_id)}" />
                  <button type="submit">加入</button>
                </form>
                <form method="post" action="/action/add" class="inline">
                  <input type="hidden" name="seed_input" value="{html.escape(seed_id)}" />
                  <input type="hidden" name="force" value="1" />
                  <button type="submit">强制加入</button>
                </form>
                <form method="post" action="/action/delete-seed" class="inline" onsubmit="return confirm('确认删除这个种子和数据文件吗？');">
                  <input type="hidden" name="seed_id" value="{html.escape(seed_id)}" />
                  <button type="submit" class="danger">删种+文件</button>
                </form>
              </td>
            </tr>
            """
        )

    def render_category_rows(items: list[dict[str, object]]) -> str:
        if not items:
            return '<tr><td colspan="4">暂无统计</td></tr>'
        rows = []
        for item in items[:12]:
            rows.append(
                f"<tr><td>{html.escape(str(item['category']))}</td><td>{format_bytes(int(item['uploaded']))}</td><td>{format_bytes(int(item['downloaded']))}</td><td>{int(item['count'])}</td></tr>"
            )
        return "".join(rows)

    category_names = sorted({str(item["category"]) for item in today_stats["categories"]} | {str(item["category"]) for item in month_stats["categories"]})
    category_options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>' for name in category_names
    )

    if qb_error:
        qb_html = f'<div class="warn">qB 状态读取失败：{html.escape(qb_error)}</div>'
    else:
        qb_html = f"""
        <div class="grid">
          <div class="card"><div class="label">当前下载数</div><div class="value" id="qb-downloading">{qb_info.get("downloading_count", 0)}</div></div>
          <div class="card"><div class="label">总种子数</div><div class="value" id="qb-total">{qb_info.get("total_torrents", 0)}</div></div>
          <div class="card"><div class="label">下载速度</div><div class="value" id="qb-dl">{format_bytes(int(qb_info.get("dl_info_speed", 0) or 0))}/s</div></div>
          <div class="card"><div class="label">上传速度</div><div class="value" id="qb-up">{format_bytes(int(qb_info.get("up_info_speed", 0) or 0))}/s</div></div>
          <div class="card"><div class="label">剩余磁盘</div><div class="value" id="qb-free">{format_bytes(int(qb_info.get("free_space_on_disk", 0) or 0))}</div></div>
          <div class="card"><div class="label">今日上传</div><div class="value" id="today-upload">{format_bytes(int(today_stats["uploaded"]))}</div></div>
          <div class="card"><div class="label">今日下载</div><div class="value" id="today-download">{format_bytes(int(today_stats["downloaded"]))}</div></div>
          <div class="card"><div class="label">本月上传</div><div class="value" id="month-upload">{format_bytes(int(month_stats["uploaded"]))}</div></div>
          <div class="card"><div class="label">本月下载</div><div class="value" id="month-download">{format_bytes(int(month_stats["downloaded"]))}</div></div>
        </div>
        <div class="two" style="margin-top:16px;">
          <div class="toolbar" style="grid-column:1 / -1;">
            <h2>分类统计</h2>
            <select id="qbCategoryFilter" onchange="applyCategoryFilter()">
              <option value="">全部分类</option>
              {category_options}
            </select>
          </div>
          <div>
            <h2>今日分类流量</h2>
            <div class="scroll"><table><thead><tr><th>分类</th><th>上传</th><th>下载</th><th>种子数</th></tr></thead><tbody id="today-category-body">{render_category_rows(today_stats["categories"])}</tbody></table></div>
          </div>
          <div>
            <h2>本月分类流量</h2>
            <div class="scroll"><table><thead><tr><th>分类</th><th>上传</th><th>下载</th><th>种子数</th></tr></thead><tbody id="month-category-body">{render_category_rows(month_stats["categories"])}</tbody></table></div>
          </div>
        </div>
        """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>U2 自动跟车控制台</title>
  <style>
    :root {{ --bg: #f7f4ea; --paper: #fffdf7; --ink: #1f2937; --muted: #6b7280; --line: #d6d3c7; --accent: #1f6f5f; --accent-2: #d97706; --danger: #b91c1c; }}
    * {{ box-sizing: border-box; }} body {{ margin: 0; font-family: "Segoe UI", "PingFang SC", sans-serif; background: radial-gradient(circle at top left, #efe6cf 0, transparent 35%), linear-gradient(180deg, #f8f5ed 0%, #f1ead9 100%); color: var(--ink); }}
    .wrap {{ max-width: 1500px; margin: 0 auto; padding: 24px; }} h1 {{ margin: 0 0 16px; font-size: 32px; }} h2 {{ margin: 0 0 12px; font-size: 20px; }}
    .sub {{ color: var(--muted); margin-bottom: 20px; }} .section {{ background: var(--paper); border: 1px solid var(--line); border-radius: 16px; padding: 18px; margin-bottom: 18px; box-shadow: 0 12px 30px rgba(31, 41, 55, 0.05); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }} .card {{ border: 1px solid var(--line); border-radius: 14px; padding: 14px; background: #fff; }}
    .label {{ color: var(--muted); font-size: 13px; }} .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }} .flash {{ border-radius: 12px; padding: 12px 14px; margin-bottom: 16px; }}
    .warn {{ border-radius: 12px; padding: 12px 14px; background: #fff7ed; color: #9a3412; }} .inline {{ display: inline-block; margin: 0 4px 4px 0; }} .actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }}
    input[type=text], input[type=password], textarea {{ width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line); background: #fff; }} textarea {{ min-height: 220px; font-family: Consolas, monospace; font-size: 13px; }}
    button {{ border: 0; border-radius: 10px; background: var(--accent); color: white; padding: 10px 14px; cursor: pointer; }} button.alt {{ background: var(--accent-2); }} button.danger {{ background: var(--danger); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }} th, td {{ border-top: 1px solid var(--line); vertical-align: top; text-align: left; padding: 10px 8px; }} th {{ color: var(--muted); font-weight: 600; }}
    td.text {{ max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }} .seed-name {{ max-width: 240px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: help; }}
    .scroll {{ max-height: 420px; overflow: auto; border: 1px solid var(--line); border-radius: 12px; }} .toolbar {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }}
    pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; background: #111827; color: #e5e7eb; padding: 14px; border-radius: 12px; min-height: 240px; overflow: auto; max-height: 320px; }}
    .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }} .status-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 999px; background: {"#16a34a" if data["bot_running"] else "#dc2626"}; margin-right: 8px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }}
    .ghost {{ background: #f3f4f6; color: #111827; }}
    .settings-panel {{ display: none; }} .settings-panel.open {{ display: block; }}
    @media (max-width: 980px) {{ .two {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>U2 自动跟车控制台</h1>
        <div class="sub"><span class="status-dot"></span>Bot {"运行中" if data["bot_running"] else "未运行"}，Web 端口 {data["web_port"]}</div>
      </div>
      <div>
        <button type="button" class="ghost" onclick="toggleSettings()">设置</button>
        <form method="get" action="/logout">
          <button type="submit" class="ghost">退出登录</button>
        </form>
      </div>
    </div>
    {flash_html}
    <div id="settingsPanel" class="section settings-panel">
      <div class="toolbar">
        <h2>设置</h2>
        <div style="color:#6b7280;">常用配置、模板和默认恢复都收在这里</div>
      </div>
      <form method="post" action="/action/save-settings">
        <div class="two">
          <div class="section">
            <h2>Web 控制台</h2>
            <div class="grid">
              <div class="card"><div class="label">用户名</div><input type="text" name="web_username" value="{html.escape(config.get('web.username','admin'))}" /></div>
              <div class="card"><div class="label">密码</div><input type="password" name="web_password" value="{html.escape(config.get('web.password',''))}" /></div>
              <div class="card"><div class="label">会话时长（小时）</div><input type="text" name="web_session_hours" value="{html.escape(config.get('web.session_hours','12'))}" /></div>
              <div class="card"><div class="label">qB 状态刷新（秒）</div><input type="text" name="web_qb_refresh_seconds" value="{html.escape(config.get('web.qb_refresh_seconds','5'))}" /></div>
              <div class="card"><div class="label">Web Host</div><input type="text" name="web_host" value="{html.escape(config.get('web.host','0.0.0.0'))}" /></div>
              <div class="card"><div class="label">Web 端口</div><input type="text" name="web_port" value="{html.escape(config.get('web.port','18081'))}" /></div>
            </div>
          </div>
          <div class="section">
            <h2>qB / 运行</h2>
            <div class="grid">
              <div class="card"><div class="label">qB 地址</div><input type="text" name="qb_url" value="{html.escape(config.get('qb.url',''))}" /></div>
              <div class="card"><div class="label">qB 用户名</div><input type="text" name="qb_user" value="{html.escape(config.get('qb.user',''))}" /></div>
              <div class="card"><div class="label">qB 密码</div><input type="password" name="qb_pass" value="{html.escape(config.get('qb.pass',''))}" /></div>
              <div class="card"><div class="label">分类</div><input type="text" name="qb_category" value="{html.escape(config.get('qb.category','pt-u2'))}" /></div>
              <div class="card"><div class="label">上传限速 MB/s</div><input type="text" name="qb_up_limit_mb" value="{html.escape(config.get('qb.up_limit_mb','50'))}" /></div>
              <div class="card"><div class="label">最大下载任务数</div><input type="text" name="qb_max_downloading" value="{html.escape(config.get('qb.max_downloading','0'))}" /></div>
              <div class="card"><div class="label">会话刷新秒数</div><input type="text" name="qb_session_refresh_seconds" value="{html.escape(config.get('qb.session_refresh_seconds','1500'))}" /></div>
              <div class="card"><div class="label">轮询间隔秒数</div><input type="text" name="poll_interval" value="{html.escape(config.get('poll_interval','60'))}" /></div>
              <div class="card"><div class="label">重新加入冷却（分钟）</div><input type="text" name="seed_readd_cooldown_minutes" value="{html.escape(config.get('seed.readd_cooldown_minutes','1440'))}" /></div>
              <div class="card"><div class="label">聊天消息最大年龄（分钟）</div><input type="text" name="shout_max_age_minutes" value="{html.escape(config.get('shout.max_age_minutes','120'))}" /></div>
              <div class="card"><div class="label">最小安全硬盘空间（GB）</div><input type="text" name="qb_min_free_space_gb" value="{html.escape(config.get('qb.min_free_space_gb','0'))}" /></div>
            </div>
          </div>
        </div>
        <div class="section">
          <h2>魔法匹配</h2>
          <div class="actions">
            <label><input type="checkbox" name="magic_rate_1_00" value="1" {"checked" if "1.00" in magic_rates else ""} /> 1.00 / 0.00</label>
            <label><input type="checkbox" name="magic_rate_2_00" value="1" {"checked" if "2.00" in magic_rates else ""} /> 2.00 / 0.00</label>
            <label><input type="checkbox" name="magic_rate_2_33" value="1" {"checked" if "2.33" in magic_rates else ""} /> 2.33 / 0.00</label>
            <label><input type="checkbox" name="magic_use_thresholds" value="1" {"checked" if config.get('magic.use_thresholds','0') == '1' else ""} /> 启用阈值识别</label>
          </div>
          <div class="grid" style="margin-top:12px;">
            <div class="card"><div class="label">上传倍率 >=</div><input type="text" name="magic_min_up_rate" value="{html.escape(config.get('magic.min_up_rate','1.00'))}" /></div>
            <div class="card"><div class="label">下载倍率 <=</div><input type="text" name="magic_max_down_rate" value="{html.escape(config.get('magic.max_down_rate','0.00'))}" /></div>
          </div>
          <div style="margin-top:10px; color:#6b7280;">勾选阈值后，未来出现的新倍率魔法，只要满足“上传大于等于 / 下载小于等于”也会识别。</div>
          <div class="two" style="margin-top:14px;">
            <div class="section">
              <h3>动态规则 1</h3>
              <div class="actions"><label><input type="checkbox" name="dynamic_rule_1_enabled" value="1" {"checked" if config.get('dynamic.rule1.enabled','0') == '1' else ""} /> 启用</label></div>
              <div class="grid">
                <div class="card"><div class="label">剩余空间 <= GB</div><input type="text" name="dynamic_rule_1_free_space_le_gb" value="{html.escape(config.get('dynamic.rule1.free_space_le_gb','0'))}" /></div>
                <div class="card"><div class="label">种子最小 GB</div><input type="text" name="dynamic_rule_1_min_size_gb" value="{html.escape(config.get('dynamic.rule1.min_size_gb','0'))}" /></div>
                <div class="card"><div class="label">种子最大 GB</div><input type="text" name="dynamic_rule_1_max_size_gb" value="{html.escape(config.get('dynamic.rule1.max_size_gb','0'))}" /></div>
                <div class="card"><div class="label">上传倍率 >=</div><input type="text" name="dynamic_rule_1_min_up_rate" value="{html.escape(config.get('dynamic.rule1.min_up_rate','0'))}" /></div>
                <div class="card"><div class="label">下载倍率 <=</div><input type="text" name="dynamic_rule_1_max_down_rate" value="{html.escape(config.get('dynamic.rule1.max_down_rate','999'))}" /></div>
              </div>
            </div>
            <div class="section">
              <h3>动态规则 2</h3>
              <div class="actions"><label><input type="checkbox" name="dynamic_rule_2_enabled" value="1" {"checked" if config.get('dynamic.rule2.enabled','0') == '1' else ""} /> 启用</label></div>
              <div class="grid">
                <div class="card"><div class="label">剩余空间 <= GB</div><input type="text" name="dynamic_rule_2_free_space_le_gb" value="{html.escape(config.get('dynamic.rule2.free_space_le_gb','0'))}" /></div>
                <div class="card"><div class="label">种子最小 GB</div><input type="text" name="dynamic_rule_2_min_size_gb" value="{html.escape(config.get('dynamic.rule2.min_size_gb','0'))}" /></div>
                <div class="card"><div class="label">种子最大 GB</div><input type="text" name="dynamic_rule_2_max_size_gb" value="{html.escape(config.get('dynamic.rule2.max_size_gb','0'))}" /></div>
                <div class="card"><div class="label">上传倍率 >=</div><input type="text" name="dynamic_rule_2_min_up_rate" value="{html.escape(config.get('dynamic.rule2.min_up_rate','0'))}" /></div>
                <div class="card"><div class="label">下载倍率 <=</div><input type="text" name="dynamic_rule_2_max_down_rate" value="{html.escape(config.get('dynamic.rule2.max_down_rate','999'))}" /></div>
              </div>
            </div>
          </div>
        </div>
        <div class="section">
          <h2>U2 / 部署</h2>
          <div class="grid">
            <div class="card"><div class="label">Passkey</div><input type="text" name="passkey" value="{html.escape(config.get('passkey',''))}" /></div>
            <div class="card"><div class="label">CookieCloud URL</div><input type="text" name="cookiecloud_url" value="{html.escape(config.get('cookiecloud.url',''))}" /></div>
            <div class="card"><div class="label">CookieCloud Key</div><input type="text" name="cookiecloud_key" value="{html.escape(config.get('cookiecloud.key',''))}" /></div>
            <div class="card"><div class="label">CookieCloud 密码</div><input type="password" name="cookiecloud_password" value="{html.escape(config.get('cookiecloud.password',''))}" /></div>
            <div class="card"><div class="label">原始 Cookie</div><input type="text" name="cookie" value="{html.escape(config.get('cookie',''))}" /></div>
            <div class="card"><div class="label">Cookie 文件路径</div><input type="text" name="cookie_file" value="{html.escape(config.get('cookie_file',''))}" /></div>
            <div class="card"><div class="label">SSH Host</div><input type="text" name="ssh_host" value="{html.escape(deploy_get('ssh','host', default=''))}" /></div>
            <div class="card"><div class="label">SSH 端口</div><input type="text" name="ssh_port" value="{html.escape(deploy_get('ssh','port', default='22'))}" /></div>
            <div class="card"><div class="label">SSH 用户</div><input type="text" name="ssh_user" value="{html.escape(deploy_get('ssh','user', default=''))}" /></div>
            <div class="card"><div class="label">SSH 密码</div><input type="password" name="ssh_password" value="{html.escape(deploy_get('ssh','password', default=''))}" /></div>
            <div class="card"><div class="label">安装目录</div><input type="text" name="install_dir" value="{html.escape(deploy_get('install_dir', default='/opt/u2-qb-bot'))}" /></div>
            <div class="card"><div class="label">服务名</div><input type="text" name="service_name" value="{html.escape(deploy_get('service_name', default='u2-qb-bot'))}" /></div>
            <div class="card"><div class="label">Telegram Bot Token</div><input type="password" name="telegram_bot_token" value="{html.escape(config.get('telegram.bot_token',''))}" /></div>
            <div class="card"><div class="label">Telegram Chat ID</div><input type="text" name="telegram_chat_id" value="{html.escape(config.get('telegram.chat_id',''))}" /></div>
            <div class="card"><div class="label">Telegram Poll Seconds</div><input type="text" name="telegram_poll_seconds" value="{html.escape(config.get('telegram.poll_seconds','3'))}" /></div>
          </div>
          <div class="actions" style="margin-top:14px;">
            <label><input type="checkbox" name="restart_bot_after_save" value="1" /> 保存后自动重启 Bot</label>
            <label><input type="checkbox" name="restart_web_after_save" value="1" /> 保存后自动重启 Web</label>
            <label><input type="checkbox" name="restart_telegram_after_save" value="1" /> 保存后自动重启 Telegram</label>
            <button type="submit">保存设置</button>
            <button type="button" class="alt" onclick="restoreDefaults()">恢复默认</button>
            <button type="button" class="ghost" onclick="toggleAdvanced()">高级脚本</button>
          </div>
        </div>
        <div class="section">
          <h2>日志归档</h2>
          <div class="grid">
            <div class="card"><div class="label">归档间隔（分钟）</div><input type="text" name="log_archive_interval_minutes" value="{html.escape(config.get('log.archive_interval_minutes','60'))}" /></div>
            <div class="card"><div class="label">最大保留数量</div><input type="text" name="log_archive_keep_count" value="{html.escape(config.get('log.archive_keep_count','20'))}" /></div>
          </div>
          <div class="actions" style="margin-top:12px;">
            <button type="button" class="ghost" onclick="window.location='/export/events.csv'">导出事件 CSV</button>
            <button type="button" class="ghost" onclick="window.location='/export/log.txt'">导出当前日志</button>
            <button type="submit" class="ghost" formaction="/action/archive-log" formmethod="post">立即归档日志</button>
          </div>
        </div>
      </form>
      <div id="advancedPanel" style="display:none;">
        <div class="toolbar">
          <button type="button" class="alt" onclick="restoreTemplate('configEditor','configTemplate')">恢复 config 模板</button>
          <button type="button" class="alt" onclick="restoreTemplate('deployEditor','deployTemplate')">恢复 deploy 模板</button>
        </div>
        <div class="two">
          <div>
            <h2>config.properties</h2>
            <form method="post" action="/action/save-file">
              <input type="hidden" name="target" value="config" />
              <textarea id="configEditor" name="content">{html.escape(data["config_text"])}</textarea>
              <div style="margin-top:12px;"><button type="submit" class="alt">保存 config</button></div>
            </form>
          </div>
          <div>
            <h2>deploy.jsonc</h2>
            <form method="post" action="/action/save-file">
              <input type="hidden" name="target" value="deploy" />
              <textarea id="deployEditor" name="content">{html.escape(data["deploy_text"])}</textarea>
              <div style="margin-top:12px;"><button type="submit" class="alt">保存 deploy</button></div>
            </form>
          </div>
        </div>
        <details style="margin-top:14px;">
          <summary>查看模板与说明</summary>
          <div class="two" style="margin-top:12px;">
            <div>
              <h2>config 模板</h2>
              <textarea id="configTemplate" readonly>{html.escape(data["config_template_text"])}</textarea>
            </div>
            <div>
              <h2>deploy 模板</h2>
              <textarea id="deployTemplate" readonly>{html.escape(data["deploy_template_text"])}</textarea>
            </div>
          </div>
        </details>
      </div>
    </div>
    <div class="section"><h2>总览</h2><div class="grid">
      <div class="card"><div class="label">事件总数</div><div class="value">{summary["total"]}</div></div>
      <div class="card"><div class="label">成功加种</div><div class="value">{summary["added"]}</div></div>
      <div class="card"><div class="label">跳过次数</div><div class="value">{summary["skipped"]}</div></div>
      <div class="card"><div class="label">失败次数</div><div class="value">{summary["failed"]}</div></div>
    </div></div>
    <div class="section"><h2>qB 状态</h2>{qb_html}</div>
    <div class="section"><h2>手工添加种子</h2><form method="post" action="/action/add"><div class="actions">
      <div style="min-width:340px; flex:1;"><label>支持多行输入，接受 `123456` 或 `ID=123456`</label><textarea class="multi" name="seed_input" placeholder="123456&#10;ID=234567&#10;id = 345678"></textarea></div>
      <label><input type="checkbox" name="force" value="1" /> 强制加入</label><button type="submit">批量加入 qB</button>
    </div></form></div>
    <div class="section">
      <div class="toolbar">
        <h2>最近 24 小时聊天 / 加种事件</h2>
        <div class="actions">
          <input type="text" id="eventSearch" placeholder="搜索种子名、ID、聊天文本" oninput="filterRows('eventSearch')" />
          <select id="eventMinutes" onchange="filterRows('eventSearch')">
            <option value="">全部时间</option>
            <option value="10">最近 10 分钟</option>
            <option value="30">最近 30 分钟</option>
            <option value="60">最近 60 分钟</option>
            <option value="120">最近 120 分钟</option>
          </select>
          <select id="eventMagic" onchange="filterRows('eventSearch')">
            <option value="">全部魔法</option>
            <option value="1.00">上传 1.00</option>
            <option value="2.00">上传 2.00</option>
            <option value="2.33">上传 2.33</option>
          </select>
          <select id="eventSource" onchange="filterRows('eventSearch')">
            <option value="">全部来源</option>
            <option value="自动监控">自动监控</option>
            <option value="手动添加">手动添加</option>
            <option value="Telegram Bot">Telegram Bot</option>
          </select>
          <select id="eventResult" onchange="filterRows('eventSearch')">
            <option value="">全部结果</option>
            <option value="已加入 qB">已加入 qB</option>
            <option value="qB 已存在">qB 已存在</option>
            <option value="命中冷却时间">命中冷却时间</option>
            <option value="已删除种子">已删除种子</option>
          </select>
        </div>
      </div>
      <div class="scroll"><table><thead><tr><th>记录时间</th><th>喊话时间</th><th>魔法</th><th>种子ID</th><th>种子名称</th><th>来源</th><th>结果</th><th>说明</th><th>聊天文本</th><th>操作</th></tr></thead><tbody id="eventTableBody">
        {''.join(event_rows) if event_rows else '<tr><td colspan="10">暂无数据</td></tr>'}
      </tbody></table></div>
    </div>
    <div class="section">
      <div class="toolbar">
        <h2>运行日志</h2>
        <input type="text" id="logSearch" placeholder="搜索日志内容" oninput="filterLog()" />
      </div>
      <pre id="logContent">{html.escape(data["log_tail"])}</pre>
    </div>
  </div>
  <script>
    const rawLog = document.getElementById('logContent').textContent;
    const qbRefreshSeconds = {int(data["qb_refresh_seconds"])};
    let lastQbPayload = null;
    function toggleSettings() {{
      document.getElementById('settingsPanel').classList.toggle('open');
    }}
    function toggleAdvanced() {{
      const el = document.getElementById('advancedPanel');
      el.style.display = el.style.display === 'none' ? 'block' : 'none';
    }}
    function restoreTemplate(targetId, templateId) {{
      document.getElementById(targetId).value = document.getElementById(templateId).value;
    }}
    function restoreDefaults() {{
      const form = document.querySelector('form[action="/action/save-settings"]');
      if (!form) return;
      form.reset();
      const cfg = {json.dumps(data["defaults"].get("config", {}), ensure_ascii=False)};
      const dep = {json.dumps(data["defaults"].get("deploy", {}), ensure_ascii=False)};
      const set = (name, value) => {{ const el = form.querySelector(`[name="${{name}}"]`); if (el) el.value = value ?? ''; }};
      set('qb_url', cfg['qb.url']); set('qb_user', cfg['qb.user']); set('qb_pass', cfg['qb.pass']); set('qb_category', cfg['qb.category']);
      set('qb_up_limit_mb', cfg['qb.up_limit_mb']); set('qb_max_downloading', cfg['qb.max_downloading']); set('qb_session_refresh_seconds', cfg['qb.session_refresh_seconds']); set('qb_min_free_space_gb', cfg['qb.min_free_space_gb']);
      set('seed_readd_cooldown_minutes', cfg['seed.readd_cooldown_minutes']); set('shout_max_age_minutes', cfg['shout.max_age_minutes']); set('poll_interval', cfg['poll_interval']);
      set('cookie', cfg['cookie']); set('cookie_file', cfg['cookie_file']);
      set('web_host', cfg['web.host']); set('web_port', cfg['web.port']); set('web_username', cfg['web.username']); set('web_password', cfg['web.password']);
      set('web_session_hours', cfg['web.session_hours']); set('web_qb_refresh_seconds', cfg['web.qb_refresh_seconds']); set('log_archive_interval_minutes', cfg['log.archive_interval_minutes']); set('log_archive_keep_count', cfg['log.archive_keep_count']);
      set('magic_min_up_rate', cfg['magic.min_up_rate']); set('magic_max_down_rate', cfg['magic.max_down_rate']);
      set('dynamic_rule_1_free_space_le_gb', cfg['dynamic.rule1.free_space_le_gb']); set('dynamic_rule_1_min_size_gb', cfg['dynamic.rule1.min_size_gb']); set('dynamic_rule_1_max_size_gb', cfg['dynamic.rule1.max_size_gb']); set('dynamic_rule_1_min_up_rate', cfg['dynamic.rule1.min_up_rate']); set('dynamic_rule_1_max_down_rate', cfg['dynamic.rule1.max_down_rate']);
      set('dynamic_rule_2_free_space_le_gb', cfg['dynamic.rule2.free_space_le_gb']); set('dynamic_rule_2_min_size_gb', cfg['dynamic.rule2.min_size_gb']); set('dynamic_rule_2_max_size_gb', cfg['dynamic.rule2.max_size_gb']); set('dynamic_rule_2_min_up_rate', cfg['dynamic.rule2.min_up_rate']); set('dynamic_rule_2_max_down_rate', cfg['dynamic.rule2.max_down_rate']);
      set('passkey', cfg['passkey']); set('cookiecloud_url', cfg['cookiecloud.url']); set('cookiecloud_key', cfg['cookiecloud.key']); set('cookiecloud_password', cfg['cookiecloud.password']);
      set('telegram_bot_token', cfg['telegram.bot_token']); set('telegram_chat_id', cfg['telegram.chat_id']); set('telegram_poll_seconds', cfg['telegram.poll_seconds']);
      set('ssh_host', dep?.ssh?.host); set('ssh_port', dep?.ssh?.port); set('ssh_user', dep?.ssh?.user); set('ssh_password', dep?.ssh?.password);
      set('install_dir', dep?.install_dir); set('service_name', dep?.service_name);
      const rates = new Set(String(cfg['magic.up_rates'] || '1.00,2.00,2.33').split(',').map(x => x.trim()));
      const c1 = form.querySelector('[name="magic_rate_1_00"]'); if (c1) c1.checked = rates.has('1.00');
      const c2 = form.querySelector('[name="magic_rate_2_00"]'); if (c2) c2.checked = rates.has('2.00');
      const c3 = form.querySelector('[name="magic_rate_2_33"]'); if (c3) c3.checked = rates.has('2.33');
      const ct = form.querySelector('[name="magic_use_thresholds"]'); if (ct) ct.checked = String(cfg['magic.use_thresholds'] || '0') === '1';
      const d1 = form.querySelector('[name="dynamic_rule_1_enabled"]'); if (d1) d1.checked = String(cfg['dynamic.rule1.enabled'] || '0') === '1';
      const d2 = form.querySelector('[name="dynamic_rule_2_enabled"]'); if (d2) d2.checked = String(cfg['dynamic.rule2.enabled'] || '0') === '1';
    }}
    function filterRows(inputId) {{
      const keyword = document.getElementById(inputId).value.trim().toLowerCase();
      const maxMinutes = document.getElementById('eventMinutes') ? document.getElementById('eventMinutes').value : '';
      const magic = document.getElementById('eventMagic') ? document.getElementById('eventMagic').value : '';
      const source = document.getElementById('eventSource') ? document.getElementById('eventSource').value : '';
      const result = document.getElementById('eventResult') ? document.getElementById('eventResult').value : '';
      document.querySelectorAll('#eventTableBody tr').forEach(tr => {{
        const text = (tr.dataset.search || tr.innerText || '').toLowerCase();
        const minutes = tr.dataset.minutes ? Number(tr.dataset.minutes) : null;
        const rowMagic = tr.dataset.magic || '';
        const rowSource = tr.dataset.source || '';
        const rowResult = tr.dataset.result || '';
        const okKeyword = !keyword || text.includes(keyword);
        const okMinutes = !maxMinutes || (minutes !== null && minutes <= Number(maxMinutes));
        const okMagic = !magic || rowMagic === magic;
        const okSource = !source || rowSource === source;
        const okResult = !result || rowResult === result;
        tr.style.display = (okKeyword && okMinutes && okMagic && okSource && okResult) ? '' : 'none';
      }});
    }}
    function filterLog() {{
      const keyword = document.getElementById('logSearch').value.trim().toLowerCase();
      if (!keyword) {{
        document.getElementById('logContent').textContent = rawLog;
        return;
      }}
      const lines = rawLog.split('\\n').filter(line => line.toLowerCase().includes(keyword));
      document.getElementById('logContent').textContent = lines.join('\\n');
    }}
    function renderCategoryRows(items) {{
      if (!items || !items.length) return '<tr><td colspan="4">暂无统计</td></tr>';
      return items.map(item => `<tr><td>${{item.category}}</td><td>${{item.uploaded_text}}</td><td>${{item.downloaded_text}}</td><td>${{item.count}}</td></tr>`).join('');
    }}
    function applyCategoryFilter() {{
      if (!lastQbPayload) return;
      const selected = document.getElementById('qbCategoryFilter') ? document.getElementById('qbCategoryFilter').value : '';
      const todayItems = lastQbPayload.today?.categories || [];
      const monthItems = lastQbPayload.month?.categories || [];
      const todayFiltered = selected ? todayItems.filter(item => item.category === selected) : todayItems;
      const monthFiltered = selected ? monthItems.filter(item => item.category === selected) : monthItems;
      if (document.getElementById('today-category-body')) document.getElementById('today-category-body').innerHTML = renderCategoryRows(todayFiltered);
      if (document.getElementById('month-category-body')) document.getElementById('month-category-body').innerHTML = renderCategoryRows(monthFiltered);
      if (selected) {{
        const todayOne = todayFiltered[0] || {{uploaded_text:'0 B', downloaded_text:'0 B'}};
        const monthOne = monthFiltered[0] || {{uploaded_text:'0 B', downloaded_text:'0 B'}};
        if (document.getElementById('today-upload')) document.getElementById('today-upload').textContent = todayOne.uploaded_text || '0 B';
        if (document.getElementById('today-download')) document.getElementById('today-download').textContent = todayOne.downloaded_text || '0 B';
        if (document.getElementById('month-upload')) document.getElementById('month-upload').textContent = monthOne.uploaded_text || '0 B';
        if (document.getElementById('month-download')) document.getElementById('month-download').textContent = monthOne.downloaded_text || '0 B';
      }} else {{
        if (document.getElementById('today-upload')) document.getElementById('today-upload').textContent = lastQbPayload.today?.uploaded_text || '0 B';
        if (document.getElementById('today-download')) document.getElementById('today-download').textContent = lastQbPayload.today?.downloaded_text || '0 B';
        if (document.getElementById('month-upload')) document.getElementById('month-upload').textContent = lastQbPayload.month?.uploaded_text || '0 B';
        if (document.getElementById('month-download')) document.getElementById('month-download').textContent = lastQbPayload.month?.downloaded_text || '0 B';
      }}
    }}
    async function refreshQb() {{
      try {{
        const resp = await fetch('/api/qb-status', {{ credentials: 'same-origin' }});
        if (!resp.ok) return;
        const payload = await resp.json();
        lastQbPayload = payload;
        const info = payload.info || {{}};
        if (document.getElementById('qb-downloading')) document.getElementById('qb-downloading').textContent = info.downloading_count ?? 0;
        if (document.getElementById('qb-total')) document.getElementById('qb-total').textContent = info.total_torrents ?? 0;
        if (document.getElementById('qb-dl')) document.getElementById('qb-dl').textContent = (info.dl_info_speed_text || '0 B') + '/s';
        if (document.getElementById('qb-up')) document.getElementById('qb-up').textContent = (info.up_info_speed_text || '0 B') + '/s';
        if (document.getElementById('qb-free')) document.getElementById('qb-free').textContent = info.free_space_text || '0 B';
        if (document.getElementById('today-upload')) document.getElementById('today-upload').textContent = payload.today?.uploaded_text || '0 B';
        if (document.getElementById('today-download')) document.getElementById('today-download').textContent = payload.today?.downloaded_text || '0 B';
        if (document.getElementById('month-upload')) document.getElementById('month-upload').textContent = payload.month?.uploaded_text || '0 B';
        if (document.getElementById('month-download')) document.getElementById('month-download').textContent = payload.month?.downloaded_text || '0 B';
        applyCategoryFilter();
      }} catch (err) {{
      }}
    }}
    refreshQb();
    setInterval(refreshQb, Math.max(2000, qbRefreshSeconds * 1000));
  </script>
</body>
</html>"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "U2Manager/1.0"
    SESSION_COOKIE_NAME = "u2_session"

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if self.path == "/export/events.csv":
            if not self._is_authenticated():
                self._html(render_login_page(self.app, "请先登录"), status=401)
                return
            rows = self.app.dashboard_data()["events"]
            output = io.StringIO()
            output.write("记录时间,喊话时间,魔法,种子ID,种子名称,来源,结果,说明,聊天文本\n")
            for row in rows:
                meta = parse_event_meta(row["matched_text"] or "")
                values = [
                    row["created_at"],
                    meta["shout_time"],
                    meta["magic"],
                    row["seed_id"],
                    meta["seed_name"],
                    translate_source(row["source"]),
                    translate_result(row["result"]),
                    summarize_note(row["result"], row["note"] or ""),
                    row["matched_text"] or "",
                ]
                output.write(",".join('"' + str(v).replace('"', '""') + '"' for v in values) + "\n")
            self._binary(output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "u2-events.csv")
            return

        if self.path == "/export/log.txt":
            if not self._is_authenticated():
                self._html(render_login_page(self.app, "请先登录"), status=401)
                return
            self._binary(read_text_file(self.app.log_file).encode("utf-8", errors="ignore"), "text/plain; charset=utf-8", "u2.log")
            return

        if self.path == "/api/qb-status":
            if not self._is_authenticated():
                self._json({"error": "unauthorized"}, status=401)
                return
            data = self.app.dashboard_data()
            qb_info = dict(data["qb_info"] or {})
            qb_info["dl_info_speed_text"] = format_bytes(int(qb_info.get("dl_info_speed", 0) or 0))
            qb_info["up_info_speed_text"] = format_bytes(int(qb_info.get("up_info_speed", 0) or 0))
            qb_info["free_space_text"] = format_bytes(int(qb_info.get("free_space_on_disk", 0) or 0))
            today_categories = []
            for item in data["today_stats"]["categories"]:
                today_categories.append({
                    "category": item["category"],
                    "uploaded_text": format_bytes(int(item["uploaded"])),
                    "uploaded": int(item["uploaded"]),
                    "downloaded_text": format_bytes(int(item["downloaded"])),
                    "downloaded": int(item["downloaded"]),
                    "count": int(item["count"]),
                })
            month_categories = []
            for item in data["month_stats"]["categories"]:
                month_categories.append({
                    "category": item["category"],
                    "uploaded_text": format_bytes(int(item["uploaded"])),
                    "uploaded": int(item["uploaded"]),
                    "downloaded_text": format_bytes(int(item["downloaded"])),
                    "downloaded": int(item["downloaded"]),
                    "count": int(item["count"]),
                })
            payload = {
                "info": qb_info,
                "refreshSeconds": data["qb_refresh_seconds"],
                "today": {
                    "uploaded_text": format_bytes(int(data["today_stats"]["uploaded"])),
                    "downloaded_text": format_bytes(int(data["today_stats"]["downloaded"])),
                    "categories": today_categories,
                },
                "month": {
                    "uploaded_text": format_bytes(int(data["month_stats"]["uploaded"])),
                    "downloaded_text": format_bytes(int(data["month_stats"]["downloaded"])),
                    "categories": month_categories,
                },
            }
            self._json(payload)
            return
        if self.path == "/logout":
            session_id = self._session_id_from_cookie()
            if session_id:
                self.app.delete_session(session_id)
            self.send_response(303)
            self.send_header("Set-Cookie", f"{self.SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if not self._is_authenticated():
            self._html(render_login_page(self.app), status=200)
            return
        self._html(render_dashboard(self.app))

    def do_POST(self) -> None:
        form = self._read_form()
        if self.path == "/login":
            username = ensure_text(form.get("username"))
            password = ensure_text(form.get("password"))
            if self.app.validate_login(username, password):
                session_id, max_age = self.app.create_session()
                self.send_response(303)
                self.send_header(
                    "Set-Cookie",
                    f"{self.SESSION_COOKIE_NAME}={session_id}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax",
                )
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._html(render_login_page(self.app, "用户名或密码错误"), status=200)
            return

        if not self._is_authenticated():
            self._html(render_login_page(self.app, "请先登录"), status=401)
            return

        if self.path == "/action/add":
            seed_input = ensure_text(form.get("seed_input")) or ensure_text(form.get("seed_id"))
            force = ensure_text(form.get("force")) == "1"
            seed_ids = normalize_seed_ids_input(seed_input)
            if not seed_ids:
                self._html(render_dashboard(self.app, "没有识别到有效的种子 ID", "error"))
                return
            messages = []
            level = "info"
            for seed_id in seed_ids:
                result = add_seed(
                    config_path=self.app.config_path,
                    db_path=self.app.db_path,
                    seed_id=seed_id,
                    occurrence_key=f"manual:{seed_id}:{uuid.uuid4().hex}",
                    source="manual",
                    matched_text="手工加入",
                    force=force,
                    log_file=self.app.log_file,
                )
                messages.append(f"{seed_id}: {result['message']}")
                if result["status"] == "added":
                    level = "success"
                elif result["status"] in {"qb_add_failed", "error"}:
                    level = "error"
            self._html(render_dashboard(self.app, "；".join(messages[:8]), level))
            return

        if self.path == "/action/delete-seed":
            seed_id = ensure_text(form.get("seed_id"))
            if not seed_id.isdigit():
                self._html(render_dashboard(self.app, "删除失败：种子 ID 不合法", "error"))
                return
            result = delete_seed_from_qb(
                config_path=self.app.config_path,
                db_path=self.app.db_path,
                seed_id=seed_id,
                log_file=self.app.log_file,
            )
            level = "success" if result["status"] == "deleted" else "error"
            self._html(render_dashboard(self.app, result["message"], level))
            return

        if self.path == "/action/save-settings":
            try:
                config_text = build_config_properties_from_form(form)
                deploy_text = build_deploy_json_from_form(form)
                restart_bot = ensure_text(form.get("restart_bot_after_save")) == "1"
                restart_web = ensure_text(form.get("restart_web_after_save")) == "1"
                restart_telegram = ensure_text(form.get("restart_telegram_after_save")) == "1"
                with self.app.lock:
                    write_text_file(self.app.config_path, config_text)
                    write_text_file(self.app.deploy_file, deploy_text)
                self.app.schedule_restart(restart_bot, restart_web, restart_telegram)
                message = "设置已保存。"
                if restart_bot or restart_web or restart_telegram:
                    restart_targets = []
                    if restart_bot:
                        restart_targets.append("Bot")
                    if restart_web:
                        restart_targets.append("Web")
                    if restart_telegram:
                        restart_targets.append("Telegram")
                    message += " 已安排自动重启：" + " / ".join(restart_targets)
                else:
                    message += " 如修改了端口、账号、密码或抓取规则，建议手动重启服务。"
                self._html(render_dashboard(self.app, message, "success"))
            except Exception as exc:
                self._html(render_dashboard(self.app, f"保存设置失败：{exc}", "error"))
            return

        if self.path == "/action/archive-log":
            archive_logs(
                [
                    self.app.log_file,
                    os.path.join(ROOT_DIR, "u2_web.log"),
                    os.path.join(ROOT_DIR, "u2_telegram.log"),
                ],
                DEFAULT_ARCHIVE_DIR,
                max(1, get_int(self.app.current_config(), "log.archive_keep_count", 20)),
            )
            self._html(render_dashboard(self.app, "已手动归档当前日志到 old_logs", "success"))
            return

        if self.path == "/action/save-file":
            target = ensure_text(form.get("target"))
            content = form.get("content", "")
            if target == "config":
                path = self.app.config_path
            elif target == "deploy":
                path = self.app.deploy_file
            else:
                self._html(render_dashboard(self.app, "未知的保存目标", "error"))
                return
            with self.app.lock:
                write_text_file(path, content)
            self._html(render_dashboard(self.app, f"已保存 {os.path.basename(path)}，如改动运行参数请手动重启服务", "success"))
            return

        self.send_error(404)

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def _is_authenticated(self) -> bool:
        if not self.app.auth_password():
            return True
        session_id = self._session_id_from_cookie()
        return bool(session_id and self.app.is_valid_session(session_id))

    def _session_id_from_cookie(self) -> str:
        cookies = http.cookies.SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        if cookies.get(self.SESSION_COOKIE_NAME) is None:
            return ""
        return cookies[self.SESSION_COOKIE_NAME].value

    def _html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            return

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            return

    def _binary(self, data: bytes, content_type: str, filename: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            return

    def log_message(self, fmt: str, *args) -> None:
        return


def run_server(
    *,
    config_path: str,
    db_path: str,
    log_file: str,
    deploy_file: str,
    pid_file: str,
    service_script: str,
) -> None:
    app = WebApp(
        config_path=config_path,
        db_path=db_path,
        log_file=log_file,
        deploy_file=deploy_file,
        pid_file=pid_file,
        service_script=service_script,
    )
    app.start_background_sampler()
    app.start_log_archiver()
    server = ThreadingHTTPServer((app.host(), app.port()), WebHandler)
    server.app = app  # type: ignore[attr-defined]
    log_message(f"Web 控制台已启动：http://{app.host()}:{app.port()}", log_file)
    server.serve_forever()


def cmd_record_event(args: argparse.Namespace) -> int:
    db = StateDB(args.db)
    db.record_event(
        args.occurrence_key,
        args.seed_id,
        args.source,
        args.matched_text,
        args.action,
        args.result,
        args.note,
    )
    return 0


def cmd_occurrence_exists(args: argparse.Namespace) -> int:
    db = StateDB(args.db)
    return 0 if db.has_occurrence(args.occurrence_key) else 1


def cmd_add_seed(args: argparse.Namespace) -> int:
    result = add_seed(
        config_path=args.config,
        db_path=args.db,
        seed_id=args.seed_id,
        occurrence_key=args.occurrence_key,
        source=args.source,
        matched_text=args.matched_text,
        force=args.force,
        log_file=args.log_file,
    )
    print(json.dumps(result, ensure_ascii=False))
    ok_statuses = {
        "added",
        "exists_in_qb",
        "skip_cooldown",
        "skip_low_space",
        "skip_low_space_after_add",
        "skip_max_downloading",
        "skip_dynamic_rule",
        "duplicate_occurrence",
    }
    return 0 if result["status"] in ok_statuses else 1


def cmd_serve(args: argparse.Namespace) -> int:
    run_server(
        config_path=args.config,
        db_path=args.db,
        log_file=args.log_file,
        deploy_file=args.deploy_file,
        pid_file=args.pid_file,
        service_script=args.service_script,
    )
    return 0


def cmd_health_check(args: argparse.Namespace) -> int:
    status, lines = generate_health_report(args.config, args.db)
    print("\n".join(lines))
    return 0 if status != "ERROR" else 1


def cmd_seed_status(args: argparse.Namespace) -> int:
    print(build_seed_status_text(args.config, args.db, args.seed_id))
    return 0


def cmd_telegram_bot(args: argparse.Namespace) -> int:
    run_telegram_bot(
        config_path=args.config,
        db_path=args.db,
        offset_file=args.offset_file,
        log_file=args.log_file,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="U2 自动跟车辅助管理器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_event = subparsers.add_parser("record-event")
    record_event.add_argument("--db", default=DEFAULT_DB)
    record_event.add_argument("--occurrence-key", required=True)
    record_event.add_argument("--seed-id", required=True)
    record_event.add_argument("--source", required=True)
    record_event.add_argument("--matched-text", default="")
    record_event.add_argument("--action", required=True)
    record_event.add_argument("--result", required=True)
    record_event.add_argument("--note", default="")
    record_event.set_defaults(func=cmd_record_event)

    occurrence_exists = subparsers.add_parser("occurrence-exists")
    occurrence_exists.add_argument("--db", default=DEFAULT_DB)
    occurrence_exists.add_argument("--occurrence-key", required=True)
    occurrence_exists.set_defaults(func=cmd_occurrence_exists)

    add_seed_parser = subparsers.add_parser("add-seed")
    add_seed_parser.add_argument("--config", default=DEFAULT_CONFIG)
    add_seed_parser.add_argument("--db", default=DEFAULT_DB)
    add_seed_parser.add_argument("--log-file", default=DEFAULT_LOG)
    add_seed_parser.add_argument("--seed-id", required=True)
    add_seed_parser.add_argument("--occurrence-key", required=True)
    add_seed_parser.add_argument("--source", default="manual")
    add_seed_parser.add_argument("--matched-text", default="")
    add_seed_parser.add_argument("--force", action="store_true")
    add_seed_parser.set_defaults(func=cmd_add_seed)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", default=DEFAULT_CONFIG)
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--log-file", default=DEFAULT_LOG)
    serve.add_argument("--deploy-file", default=DEFAULT_DEPLOY)
    serve.add_argument("--pid-file", default=DEFAULT_PID)
    serve.add_argument("--service-script", default=DEFAULT_SERVICE)
    serve.set_defaults(func=cmd_serve)

    health_check = subparsers.add_parser("health-check")
    health_check.add_argument("--config", default=DEFAULT_CONFIG)
    health_check.add_argument("--db", default=DEFAULT_DB)
    health_check.set_defaults(func=cmd_health_check)

    seed_status = subparsers.add_parser("seed-status")
    seed_status.add_argument("--config", default=DEFAULT_CONFIG)
    seed_status.add_argument("--db", default=DEFAULT_DB)
    seed_status.add_argument("--seed-id", required=True)
    seed_status.set_defaults(func=cmd_seed_status)

    telegram_bot = subparsers.add_parser("telegram-bot")
    telegram_bot.add_argument("--config", default=DEFAULT_CONFIG)
    telegram_bot.add_argument("--db", default=DEFAULT_DB)
    telegram_bot.add_argument("--offset-file", default=DEFAULT_TELEGRAM_OFFSET)
    telegram_bot.add_argument("--log-file", default=None)
    telegram_bot.set_defaults(func=cmd_telegram_bot)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
