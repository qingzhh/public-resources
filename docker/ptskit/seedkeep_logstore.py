"""Private JSON-line logs. Callers own the worker-idle check and run.lock.

read returns {items, total}; total counts matching valid objects in scanned tails.
Each file contributes at most its last 4 MiB; truncated=True marks omitted bytes.
cleanup returns {mode, files, removed, kept, bytes_removed}. tick returns that
result when due, otherwise None. Malformed and oversized lines are never parsed.
All cooperating writers must use append; locks also cover policy and metadata.
"""
import contextlib
import datetime
import heapq
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:  # Windows uses its native byte-range lock instead.
    fcntl = None
    import msvcrt


LOG_NAMES = ('run.log.1', 'run.log', 'management.log.1', 'management.log')
DEFAULT_POLICY = {'auto_cleanup_enabled': False, 'retention_days': 30,
                  'cleanup_interval_hours': 24}
MAX_LINE_BYTES = 64 * 1024
MAX_READ_LIMIT = 1000
MAX_READ_BYTES = 4 * 1024 * 1024
_POLICY_NAME = 'log_policy.json'
_META_NAME = 'log_cleanup_meta.json'
_LOCK_NAME = '.logstore.lock'
_DOCUMENT_BYTES = 4096
_THREADS = {}
_THREADS_GUARD = threading.Lock()
_IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,95}\Z')
_SECRET = re.compile(
    r'''(?ix)\b(password|passwd|pwd|token|api[_-]?key|passkey|secret|cookie|authorization|connection|username|host|port|proxy[_-]?url)\b
        ["']?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''')
_URL = re.compile(r'(?i)\b(?:https?|socks[45]?|ftp)://[^\s<>"\']+')
_AUTH = re.compile(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=._-]+')
_IP = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])')
_LEVELS = frozenset(('debug', 'info', 'warning', 'error', 'critical'))
_COUNTERS = ('added_this_run', 'managed_active', 'errors', 'processed',
             'seeding_total', 'seeding_valid', 'seeding_invalid', 'seeding_unknown',
             'seeding_inactive', 'seeding_downloading', 'site_current', 'refill_allowance',
             'transfer_total', 'transfer_completed', 'transfer_failed', 'transfer_skipped',
             'transfer_cancelled', 'transfer_waiting')


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _reject_constant(value):
    raise ValueError('非标准 JSON 数值')


def _timestamp(value):
    if _finite_number(value):
        return float(value)
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _is_error(row):
    event = row.get('event')
    if isinstance(event, str):
        event = event.strip().lower()
        if event == 'failed' or event.endswith(('_error', '_failed')):
            return True
    errors = row.get('errors')
    if (type(errors) is int or _finite_number(errors)) and errors > 0:
        return True
    if type(row.get('transfer_failed')) is int and row['transfer_failed'] > 0:
        return True
    error = row.get('error')
    if isinstance(error, str):
        return bool(error.strip())
    return isinstance(error, (dict, list, bool, int, float)) and bool(error)


def _safe_reason(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value[:4096]
    text = _URL.sub('[连接已隐藏]', text)
    text = _AUTH.sub('[认证已隐藏]', text)
    text = _SECRET.sub(lambda match: match.group(1) + '=[已隐藏]', text)
    text = _IP.sub('[地址已隐藏]', text)
    text = re.sub(r'(?<![A-Za-z0-9])[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?(?![A-Za-z0-9])', '[种子标识已隐藏]', text)
    return ''.join(char for char in text if char >= ' ' or char in '\n\t')[:1024]


def _public(row):
    """Only scalar, type-checked fields may leave the log store."""
    result = {}
    if _timestamp(row.get('time')) is not None:
        result['time'] = row['time']
    event = row.get('event')
    if isinstance(event, str) and _IDENTIFIER.fullmatch(event):
        result['event'] = event
    if 'reason' in row:
        result['reason'] = _safe_reason(row['reason'])
    for key in _COUNTERS:
        value = row.get(key)
        if type(value) is int and 0 <= value <= 2 ** 63 - 1:
            result[key] = value
    level = row.get('level')
    result['level'] = 'error' if _is_error(row) else level if isinstance(level, str) and level in _LEVELS else 'info'
    return result


def _records(stream):
    """Yield bounded fragments, parsed rows, and physical-line-start flags."""
    oversized = False
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        start = not oversized
        too_large = oversized or len(raw) > MAX_LINE_BYTES
        row = None
        if not too_large:
            try:
                value = json.loads(raw, parse_constant=_reject_constant)
                if isinstance(value, dict):
                    row = value
            except (ValueError, UnicodeError, RecursionError):
                pass
        oversized = too_large and not raw.endswith(b'\n')
        yield raw, row, start


def _validate_policy(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULT_POLICY):
        raise ValueError('日志策略必须包含完整且唯一的三个字段')
    if type(values['auto_cleanup_enabled']) is not bool:
        raise ValueError('自动清理开关必须是布尔值')
    for key, maximum in (('retention_days', 3650), ('cleanup_interval_hours', 8760)):
        if type(values[key]) is not int or not 1 <= values[key] <= maximum:
            raise ValueError('日志保留天数或清理间隔超出范围')
    return dict(values)


class Store:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(os.path.abspath(directory))
        self.clock = clock
        key = os.path.normcase(str(self.directory))
        with _THREADS_GUARD:
            self._thread_lock = _THREADS.setdefault(key, threading.RLock())

    def _check_directory(self):
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise ValueError('日志目录不允许软链接')
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.directory.is_dir():
            raise ValueError('日志目录无效')

    def _check(self, name):
        path = self.directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('日志文件必须是独立普通文件，不允许链接')
        return info

    def _open(self, name, flags):
        before = self._check(name)
        fd = os.open(self.directory / name, flags | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            current = os.fstat(fd)
            after = self._check(name)
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or after is None
                    or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
                    or before is not None and (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('日志文件在打开时发生变化')
            if os.name != 'nt' and hasattr(os, 'fchmod'):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(self.directory / name, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            self._check_directory()
            fd = self._open(_LOCK_NAME, os.O_RDWR | os.O_CREAT)
            acquired = False
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'\0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                acquired = True
                yield
            finally:
                if acquired:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                os.close(fd)

    def _document(self, name):
        if self._check(name) is None:
            return None
        with os.fdopen(self._open(name, os.O_RDONLY), 'rb') as stream:
            raw = stream.read(_DOCUMENT_BYTES + 1)
        if len(raw) > _DOCUMENT_BYTES:
            raise ValueError('日志策略或元数据过大')
        try:
            value = json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError('日志策略或元数据格式错误') from exc
        if not isinstance(value, dict):
            raise ValueError('日志策略或元数据格式错误')
        return value

    def _atomic(self, name, writer):
        before = self._check(name)
        fd, temporary = tempfile.mkstemp(prefix='.logstore-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            after = self._check(name)
            if ((before is None) != (after is None)
                    or before is not None and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                raise ValueError('日志文件在清理时发生变化')
            os.replace(temporary, self.directory / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write_document(self, name, values):
        raw = (json.dumps(values, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        self._atomic(name, lambda stream: stream.write(raw))

    def _policy(self):
        values = self._document(_POLICY_NAME)
        return dict(DEFAULT_POLICY) if values is None else _validate_policy(values)

    def policy(self):
        with self._locked():
            return self._policy()

    def update_policy(self, values):
        values = _validate_policy(values)
        with self._locked():
            self._write_document(_POLICY_NAME, values)
        return dict(values)

    def read(self, limit=100, kind='all'):
        if type(limit) is not int or not 1 <= limit <= MAX_READ_LIMIT:
            raise ValueError('日志条数必须是 1 到 1000 的整数')
        if kind not in ('all', 'error'):
            raise ValueError('未知日志筛选类型')
        heap, total, truncated = [], 0, False
        with self._locked():
            for name in LOG_NAMES:
                if self._check(name) is None:
                    continue
                with os.fdopen(self._open(name, os.O_RDONLY), 'rb') as stream:
                    offset = max(0, os.fstat(stream.fileno()).st_size - MAX_READ_BYTES)
                    partial = False
                    if offset:
                        truncated = True
                        stream.seek(offset - 1)
                        partial = stream.read(1) != b'\n'
                    for raw, row, _ in _records(stream):
                        if partial:
                            partial = not raw.endswith(b'\n')
                            continue
                        if row is None or kind == 'error' and not _is_error(row):
                            continue
                        total += 1
                        stamp = _timestamp(row.get('time'))
                        entry = (stamp if stamp is not None else -math.inf, total, _public(row))
                        if len(heap) < limit:
                            heapq.heappush(heap, entry)
                        elif entry[:2] > heap[0][:2]:
                            heapq.heapreplace(heap, entry)
        result = {'items': [entry[2] for entry in sorted(heap, reverse=True)], 'total': total}
        if truncated:
            result['truncated'] = True
        return result

    def _cleanup(self, mode, now, policy):
        cutoff = now - policy['retention_days'] * 86400
        result = {'mode': mode, 'files': 0, 'removed': 0, 'kept': 0, 'bytes_removed': 0}
        # Refuse unsafe files before touching any of the four managed logs.
        existing = [name for name in LOG_NAMES if self._check(name) is not None]
        self._check(_META_NAME)
        for name in existing:
            def rewrite(output):
                with os.fdopen(self._open(name, os.O_RDONLY), 'rb') as source:
                    for raw, row, start in _records(source):
                        stamp = _timestamp(row.get('time')) if row is not None else None
                        remove = (mode == 'all' or row is not None and
                                  (mode == 'errors' and _is_error(row) or
                                   mode == 'expired' and stamp is not None and stamp < cutoff))
                        if remove:
                            result['removed'] += int(start)
                            result['bytes_removed'] += len(raw)
                        else:
                            output.write(raw)
                            result['kept'] += int(start)
            self._atomic(name, rewrite)
            result['files'] += 1
        self._write_document(_META_NAME, {'last_cleanup_at': now})
        return result

    def cleanup(self, mode='expired'):
        if mode not in ('expired', 'errors', 'all'):
            raise ValueError('未知日志清理类型')
        with self._locked():
            now = self.clock()
            if not _finite_number(now):
                raise ValueError('日志清理时间无效')
            return self._cleanup(mode, now, self._policy())

    def tick(self):
        with self._locked():
            policy = self._policy()
            if not policy['auto_cleanup_enabled']:
                return None
            now = self.clock()
            if not _finite_number(now):
                raise ValueError('日志清理时间无效')
            meta = self._document(_META_NAME)
            last = meta.get('last_cleanup_at') if meta else None
            if last is not None and not _finite_number(last):
                raise ValueError('日志清理元数据时间无效')
            if last is not None and 0 <= now - last < policy['cleanup_interval_hours'] * 3600:
                return None
            return self._cleanup('expired', now, policy)


def append(directory, name, event_dict):
    """Append a sanitized event under the same lock used by cleanup/read."""
    if name not in LOG_NAMES:
        raise ValueError('不是允许写入的日志文件')
    if not isinstance(event_dict, dict):
        raise ValueError('日志事件必须是字典')
    row = _public(event_dict)
    if 'time' not in row:
        row['time'] = time.time()
    # Keep errors detectable without persisting a potentially private error object.
    if _is_error(event_dict) and not row.get('errors'):
        row['errors'] = 1
    raw = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    store = Store(directory)
    with store._locked():
        maximum = {'run.log': 5 * 1024 * 1024, 'management.log': 512 * 1024}.get(name)
        current = store._check(name)
        if maximum and current is not None and current.st_size >= maximum:
            store._check(name + '.1')
            os.replace(store.directory / name, store.directory / (name + '.1'))
        fd = store._open(name, os.O_RDWR | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, 'a+b') as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b'\n':
                    stream.write(b'\n')
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
