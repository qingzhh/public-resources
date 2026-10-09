"""Bounded, read-only ownership inspection; file records are private to callers."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading

from seedkeep_downloaders import ManagementError
import seedkeep_pull as pull

__all__ = ['StorageError', 'validate', 'Storage']
_ID = re.compile(r'[a-zA-Z0-9_-]{1,64}')
_HASH = re.compile(r'[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?')
_MAX_FILES = 20000
_MAX_REFERENCES = 250000
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_BAD = '存储检查参数或状态无效'
_UNSAFE = '无法确认文件独占且安全，已阻止数据清理'


class StorageError(ManagementError):
    """Errors contain fixed messages, never paths or downloader responses."""
    def __init__(self, message=_BAD, status=400):
        # Only internal fixed messages are permitted, including direct construction.
        if message not in (_BAD, _UNSAFE, '存储设置已变化，请刷新后重试',
                           '存储设置无法读取', '存储设置无法保存'):
            message = _BAD
        super().__init__(message, status)


def _fail():
    raise StorageError()


def _text(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail()
    return value


def _remote(value, *, relative=False):
    _text(value)
    if '\\' in value or (value.startswith('/') if relative else not value.startswith('/')):
        _fail()
    if value == '/' and not relative:
        return value
    parts = value.split('/') if relative else value[1:].split('/')
    if any(part in ('', '.', '..') for part in parts):
        _fail()
    return value


def _native(value):
    _text(value)
    if (not os.path.isabs(value) or os.path.normpath(value) != value
            or (os.name != 'nt' and value.startswith('//'))):
        _fail()
    # Windows names must not alias alternate streams, DOS devices or trimmed names.
    if os.name == 'nt':
        drive, tail = os.path.splitdrive(value)
        if not re.fullmatch(r'[A-Za-z]:', drive) or '/' in tail:
            _fail()
        for part in tail.split('\\'):
            if part and (part.endswith((' ', '.')) or any(c in part for c in ':<>"|?*')
                         or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
                _fail()
    return value


def _under(path, root, *, strict=False, remote=False):
    if remote:
        return (not strict and path == root) or path.startswith(root.rstrip('/') + '/')
    path, root = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath((path, root)) == root and (not strict or path != root)
    except ValueError:
        return False


def _roots(values):
    if values is None:
        default_root = os.path.abspath(os.sep + 'inspect')
        values = tuple(os.getenv('SEEDKEEP_INSPECT_ROOTS', default_root).split(os.pathsep))
    if not isinstance(values, (tuple, list)) or not values or len(values) > 100:
        _fail()
    result = []
    for root in values:
        root = _native(root)
        if root in ('/', '/data', '/app') or os.path.dirname(root) == root:
            _fail()
        if os.path.normcase(root) in {os.path.normcase(p) for p in result}:
            _fail()
        result.append(root)
    return tuple(result)


def validate(values, *, allowed_roots=None):
    """Validate structure and lexical containment without touching content directories."""
    roots = _roots(allowed_roots)
    if not isinstance(values, dict) or set(values) != {'mappings'}:
        _fail()
    rows = values['mappings']
    if not isinstance(rows, list) or len(rows) > 100:
        _fail()
    result, seen, protections = [], set(), 0
    fields = {'instance_id', 'download_root', 'inspect_root', 'protected_paths'}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            _fail()
        identity = row['instance_id']
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            _fail()
        remote = _remote(row['download_root'])
        if remote in ('/', '/data', '/app') or (identity, remote) in seen:
            _fail()
        seen.add((identity, remote))
        local = _native(row['inspect_root'])
        if not any(_under(local, root, strict=True) for root in roots):
            _fail()
        protected = row['protected_paths']
        if not isinstance(protected, list) or len(protected) > 100:
            _fail()
        protections += len(protected)
        if protections > 1000:
            _fail()
        checked = []
        for path in protected:
            path = _remote(path)
            if not _under(path, remote, remote=True) or path in checked:
                _fail()
            checked.append(path)
        result.append({'instance_id': identity, 'download_root': remote,
                       'inspect_root': local, 'protected_paths': checked})
    return {'mappings': result}


def _unsafe_stat(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _accessible(path):
    if not os.access(path, os.R_OK | os.X_OK):
        _fail()
    # Opening, without enumerating, also detects unreadable directories.
    with os.scandir(path):
        pass


def _walk(path, *, missing=False):
    """lstat every component; absence is accepted only under an observable directory."""
    path = _native(path)
    parts = Path(path).parts
    current = parts[0]
    for index, part in enumerate(parts):
        if index:
            current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not missing or index == 0:
                raise
            _accessible(os.path.dirname(current))
            return None
        if _unsafe_stat(info):
            _fail()
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                _fail()
            _accessible(current)
    return info


def _readonly(path):
    try:
        if hasattr(os, 'statvfs') and os.statvfs(path).f_flag & getattr(os, 'ST_RDONLY', 1):
            return True
    except OSError:
        return False
    # Bind mounts can have distinct flags; prefer the longest matching mount point.
    try:
        with open('/proc/self/mountinfo', 'r', encoding='utf-8') as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            return False
        best = None
        for line in data.splitlines():
            left, right = line.split(' - ', 1)
            fields, extra = left.split(), right.split()
            mount = re.sub(r'\\(040|011|012|134)',
                           lambda match: chr(int(match.group(1), 8)), fields[4])
            if _under(path, mount) and (best is None or len(mount) > best[0]):
                best = (len(mount), 'ro' in fields[5].split(',') or 'ro' in extra[2].split(','))
        return bool(best and best[1])
    except (OSError, ValueError, IndexError, UnicodeError):
        return False


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _revision(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _descriptor(value):
    required = {'instance_id', 'type', 'hash', 'download_dir', 'files'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'aliases'}):
        _fail()
    identity, digest = value['instance_id'], value['hash']
    if (not isinstance(identity, str) or not _ID.fullmatch(identity)
            or not isinstance(value['type'], str) or value['type'] not in ('qb', 'tr')
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
        _fail()
    directory = _remote(value['download_dir'])
    files, aliases = value['files'], value.get('aliases', [])
    if not isinstance(files, list) or not 1 <= len(files) <= _MAX_FILES:
        _fail()
    if not isinstance(aliases, list) or len(aliases) > 64:
        _fail()
    if any(not isinstance(alias, str) or not _HASH.fullmatch(alias) for alias in aliases):
        _fail()
    checked, names = [], set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {'name', 'size'}:
            _fail()
        name, size = _remote(item['name'], relative=True), item['size']
        if (type(size) is not int or not 0 <= size <= 2 ** 63 - 1 or name in names
                or (os.name == 'nt' and any(':' in p for p in name.split('/')))):
            _fail()
        names.add(name)
        checked.append({'name': name, 'size': size})
    return {'instance_id': identity, 'type': value['type'], 'hash': digest.lower(),
            'download_dir': directory, 'files': checked,
            'aliases': sorted(set(alias.lower() for alias in aliases))}


def _record(path, remote, info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_ino <= 0:
        _fail()
    return {'path': path, 'remote_path': remote, 'size': info.st_size,
            'dev': info.st_dev, 'ino': info.st_ino, 'mtime_ns': info.st_mtime_ns,
            'nlink': info.st_nlink}


class Storage:
    def __init__(self, directory, *, allowed_roots=None, require_readonly=True):
        if type(require_readonly) is not bool or (not require_readonly and allowed_roots is None):
            _fail()
        self.allowed_roots = _roots(allowed_roots)
        self.require_readonly = require_readonly
        try:
            self.directory = Path(_native(os.path.abspath(os.fspath(directory))))
        except (TypeError, ValueError, OSError):
            raise StorageError() from None
        self.path = self.directory / 'storage_mappings.json'
        self._lock = threading.RLock()
        self._load()  # Corruption must fail at construction, not turn into defaults.

    def _load(self):
        try:
            info = _walk(str(self.path), missing=True)
            if info is None:
                return {'mappings': []}, False
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
                _fail()
            with self.path.open('rb') as stream:
                data = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(data) > _MAX_CONFIG_BYTES:
                _fail()
            values = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_json)
            return validate(values, allowed_roots=self.allowed_roots), True
        except (OSError, ValueError, UnicodeError, RecursionError, StorageError):
            raise StorageError('存储设置无法读取') from None

    def public(self):
        with self._lock:
            values, saved = self._load()
            return {'values': values, 'revision': _revision(values), 'saved': saved,
                    'allowed_roots': list(self.allowed_roots)}

    def update(self, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'values'}:
            _fail()
        supplied = body['revision']
        if not isinstance(supplied, str) or len(supplied) > 256:
            _fail()
        try:
            supplied = supplied.encode('utf-8')
        except UnicodeError:
            raise StorageError() from None
        values = validate(body['values'], allowed_roots=self.allowed_roots)
        encoded = json.dumps(values, ensure_ascii=False).encode('utf-8')
        if len(encoded) > _MAX_CONFIG_BYTES:
            _fail()
        with self._lock:
            current, _ = self._load()
            if not hmac.compare_digest(supplied, _revision(current).encode('ascii')):
                raise StorageError('存储设置已变化，请刷新后重试', 409)
            if any(_under(str(self.directory), root) for root in self.allowed_roots):
                _fail()  # Settings writes must never target an inspection/content root.
            try:
                _walk(str(self.directory), missing=True)
                temporary = str(self.path.with_suffix(self.path.suffix + '.tmp'))
                if _walk(temporary, missing=True) is not None:
                    _fail()  # Refuse stale files/links before the fixed-name atomic save helper.
                self.directory.mkdir(parents=True, exist_ok=True)
                pull.save_json(self.path, values)
            except (OSError, ValueError):
                raise StorageError('存储设置无法保存') from None
            return dict(self.public(), ok=True)

    def _ready(self, mapping):
        info = _walk(mapping['inspect_root'])
        if not stat.S_ISDIR(info.st_mode):
            _fail()
        _accessible(mapping['inspect_root'])
        if self.require_readonly and not _readonly(mapping['inspect_root']):
            _fail()

    def capability(self, instance_id):
        if not isinstance(instance_id, str) or not _ID.fullmatch(instance_id):
            return False
        try:
            values, _ = self._load()
            for mapping in values['mappings']:
                if mapping['instance_id'] == instance_id:
                    try:
                        self._ready(mapping)
                        return True
                    except (StorageError, OSError, ValueError):
                        continue
        except (StorageError, OSError, ValueError):
            pass
        return False

    def _content(self, path, *, missing=False):
        info = _walk(path, missing=missing)
        if self.require_readonly:
            probe, observed = path, info
            while observed is None:
                probe = os.path.dirname(probe)
                observed = _walk(probe, missing=True)
            if not _readonly(probe):
                _fail()
        return info

    @staticmethod
    def _is_protected(path, protected):
        protected_paths, identities = protected
        path = os.path.normcase(path)
        while True:
            if path in protected_paths:
                return True
            info = _walk(path, missing=True)
            if info is not None and (info.st_dev, info.st_ino) in identities:
                return True
            parent = os.path.dirname(path)
            if parent == path:
                return False
            path = parent

    @staticmethod
    def _project(mapping, remote):
        suffix = remote[len(mapping['download_root']):].lstrip('/')
        path = os.path.join(mapping['inspect_root'], *suffix.split('/')) if suffix else mapping['inspect_root']
        path = _native(path)
        if not _under(path, mapping['inspect_root']):
            _fail()
        return path

    def _resolve(self, rows, instance, remote, ready):
        choices = [row for row in rows if row['instance_id'] == instance
                   and _under(remote, row['download_root'], remote=True)]
        if not choices:
            _fail()
        mapping = max(choices, key=lambda row: len(row['download_root']))
        key = (mapping['instance_id'], mapping['download_root'])
        if key not in ready:
            self._ready(mapping)
            ready.add(key)
        return self._project(mapping, remote)

    def _protected(self, rows):
        result = set()
        for row in rows:
            for remote in row['protected_paths']:
                for other in rows:
                    if other['instance_id'] != row['instance_id']:
                        continue
                    if _under(remote, other['download_root'], remote=True):
                        result.add(os.path.normcase(self._project(other, remote)))
                    elif _under(other['download_root'], remote, remote=True):
                        result.add(os.path.normcase(other['inspect_root']))
        identities = set()
        for path in result:
            info = self._content(path)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or not info.st_ino:
                _fail()
            identities.add((info.st_dev, info.st_ino))
        return result, identities

    def inspect(self, candidate, references):
        failure = {'ok': False, 'reason': _UNSAFE, 'files': [], 'logical_bytes': 0}
        # Invalid descriptors raise; operational uncertainty returns an opaque failure.
        candidate = _descriptor(candidate)
        if not isinstance(references, list) or not 1 <= len(references) <= _MAX_REFERENCES:
            _fail()
        checked, count, identities, self_count = [], 0, set(), 0
        own = (candidate['instance_id'], candidate['hash'])
        for reference in references:
            reference = _descriptor(reference)
            count += len(reference['files'])
            if count > _MAX_REFERENCES:
                _fail()
            identity = (reference['instance_id'], reference['hash'])
            if identity in identities:
                _fail()
            identities.add(identity)
            if identity == own:
                if reference != candidate:
                    _fail()
                self_count += 1
            checked.append(reference)
        if self_count != 1:
            _fail()
        try:
            values, _ = self._load()
            rows, ready = values['mappings'], set()
            protected = self._protected(rows)
            paths, inodes, records = set(), set(), []
            aliases = set(candidate['aliases']) | {candidate['hash']}
            for item in candidate['files']:
                remote = candidate['download_dir'].rstrip('/') + '/' + item['name']
                remote = _remote(remote)
                path = self._resolve(rows, candidate['instance_id'], remote, ready)
                if self._is_protected(path, protected):
                    _fail()
                info = self._content(path)
                record = _record(path, remote, info)
                key, inode = os.path.normcase(path), (info.st_dev, info.st_ino)
                if record['size'] != item['size'] or key in paths or inode in inodes:
                    _fail()
                paths.add(key)
                inodes.add(inode)
                records.append(record)
            for reference in checked:
                if (reference['instance_id'], reference['hash']) == own:
                    continue
                if aliases & (set(reference['aliases']) | {reference['hash']}):
                    _fail()
                for item in reference['files']:
                    remote = reference['download_dir'].rstrip('/') + '/' + item['name']
                    path = self._resolve(rows, reference['instance_id'], _remote(remote), ready)
                    if os.path.normcase(path) in paths:
                        _fail()
                    info = self._content(path, missing=True)
                    if info is not None:
                        if not stat.S_ISREG(info.st_mode):
                            _fail()
                        if (info.st_dev, info.st_ino) in inodes:
                            _fail()
            snapshot = {'ok': True, 'reason': '已确认文件独占且安全',
                        'files': records, 'logical_bytes': sum(record['size'] for record in records)}
            if not self.recheck(snapshot):
                return failure
            return snapshot
        except (StorageError, OSError, ValueError, OverflowError):
            return failure

    def _snapshot(self, snapshot):
        if (not isinstance(snapshot, dict)
                or set(snapshot) != {'ok', 'reason', 'files', 'logical_bytes'}
                or type(snapshot['ok']) is not bool
                or not isinstance(snapshot['reason'], str) or len(snapshot['reason']) > 256
                or type(snapshot['logical_bytes']) is not int or snapshot['logical_bytes'] < 0
                or not isinstance(snapshot['files'], list) or len(snapshot['files']) > _MAX_FILES):
            _fail()
        records = snapshot['files']
        if not snapshot['ok']:
            if records or snapshot['logical_bytes']:
                _fail()
            return []
        if not records:
            _fail()
        paths, inodes, total = set(), set(), 0
        for record in records:
            if (not isinstance(record, dict)
                    or set(record) != {'path', 'remote_path', 'size', 'dev', 'ino', 'mtime_ns', 'nlink'}):
                _fail()
            path = _native(record['path'])
            _remote(record['remote_path'])
            if not any(_under(path, root, strict=True) for root in self.allowed_roots):
                _fail()
            if any(type(record[k]) is not int or record[k] < 0
                   for k in ('size', 'dev', 'ino', 'mtime_ns', 'nlink')):
                _fail()
            if record['ino'] == 0 or record['nlink'] != 1 or record['size'] > 2 ** 63 - 1:
                _fail()
            key, inode = os.path.normcase(path), (record['dev'], record['ino'])
            if key in paths or inode in inodes:
                _fail()
            paths.add(key)
            inodes.add(inode)
            total += record['size']
        if total != snapshot['logical_bytes']:
            _fail()
        return copy.deepcopy(records)

    def _check_snapshot(self, snapshot, *, absent):
        records = self._snapshot(snapshot)  # Corrupt persisted state must raise.
        if not records:
            return False
        try:
            values, _ = self._load()
            rows, ready = values['mappings'], set()
            protected = self._protected(rows)
            for record in records:
                # Resolve the current longest prefix separately for each mapped instance.
                choices = {}
                for row in rows:
                    if _under(record['remote_path'], row['download_root'], remote=True):
                        previous = choices.get(row['instance_id'])
                        if previous is None or len(row['download_root']) > len(previous['download_root']):
                            choices[row['instance_id']] = row
                matches = [row for row in choices.values()
                           if os.path.normcase(self._project(row, record['remote_path']))
                           == os.path.normcase(record['path'])]
                if not matches or self._is_protected(record['path'], protected):
                    return False
                for row in matches:
                    key = (row['instance_id'], row['download_root'])
                    if key not in ready:
                        self._ready(row)
                        ready.add(key)
                info = self._content(record['path'], missing=absent)
                if absent:
                    if info is not None:
                        return False
                elif _record(record['path'], record['remote_path'], info) != record:
                    return False
            return True
        except (StorageError, OSError, ValueError, OverflowError):
            return False

    def recheck(self, snapshot):
        return self._check_snapshot(snapshot, absent=False)

    def gone(self, snapshot):
        return self._check_snapshot(snapshot, absent=True)

    def covers(self, references):
        """Check exact inventory coverage before enabling; content may still be incomplete."""
        try:
            refs = list(references)
            if len(refs) > _MAX_REFERENCES:
                _fail()
            values, _ = self._load()
            rows, ready, count = values['mappings'], set(), 0
            for value in refs:
                descriptor = _descriptor(value)
                count += len(descriptor['files'])
                if count > _MAX_REFERENCES:
                    _fail()
                for item in descriptor['files']:
                    remote = descriptor['download_dir'].rstrip('/') + '/' + item['name']
                    path = self._resolve(rows, descriptor['instance_id'], remote, ready)
                    self._content(path, missing=True)
            return True
        except (StorageError, OSError, TypeError, ValueError):
            return False
