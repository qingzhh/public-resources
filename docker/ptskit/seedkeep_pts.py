"""Read PTS account statistics and expose only safe candidate metadata."""
import datetime as dt
import json
import hashlib
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.error
import urllib.request
import seedkeep_configuration as configuration
import seedkeep_rss as rss

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CACHE_SECONDS = 60
STATISTICS_FIELDS = ('task', 'has_task', 'has_record', 'current', 'target', 'missing', 'seeders_max', 'synced_at')


class PtsError(Exception):
    pass


def fetch_payload(source, *, timeout=15, limit=1000):
    if not source.get('token') or not source.get('api_base'):
        raise PtsError('PTS 连接凭证未配置')
    opener = configuration.opener(source)
    request = urllib.request.Request(source['api_base'].rstrip('/') + '/api/v1/seedkeep/refill?limit=' + str(limit),
                                     headers={'Authorization': 'Bearer ' + source['token']})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise PtsError('PTS 认证失败，请检查站点访问凭证或权限') from None
        raise PtsError('PTS 暂时无法访问（HTTP ' + str(error.code) + '）') from None
    except Exception:
        raise PtsError('PTS 连接失败，请稍后重试') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PtsError('PTS 响应超过读取上限')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise PtsError('PTS 返回了无法识别的数据') from None


def integer(value):
    return value if type(value) is int and value >= 0 else None


def text(value, limit=500):
    return value[:limit] if isinstance(value, str) else ''


def sanitize(payload):
    if not isinstance(payload, dict) or type(payload.get('ret')) is not int:
        raise PtsError('PTS 响应缺少有效业务状态')
    if payload['ret'] != 0:
        # External error messages can echo credentials; never pass them to the UI.
        raise PtsError('PTS 返回业务错误，请检查账号保种任务及访问权限')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise PtsError('PTS 响应缺少保种统计')
    counts = {key: integer(data.get(key)) for key in ('target', 'current', 'missing')}
    if any(value is None for value in counts.values()):
        raise PtsError('PTS 响应缺少有效数量，无法确认站端统计')
    for key in ('has_task', 'has_record'):
        if type(data.get(key)) is not bool:
            raise PtsError('PTS 响应缺少有效任务状态')
    raw = data.get('candidates')
    if not isinstance(raw, list) or len(raw) > 10000:
        raise PtsError('PTS 候选列表格式无效')
    rows, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = item.get('id')
        if type(tid) is int and tid > 0:
            tid = str(tid)
        if not isinstance(tid, str) or not tid.isascii() or not tid.isdigit() or len(tid) > 20 or tid in seen:
            continue
        tid = str(int(tid))
        if tid == '0' or tid in seen:
            continue
        size, seeders = integer(item.get('size')), integer(item.get('seeders'))
        if size is None or size == 0 or seeders is None:
            continue
        seen.add(tid)
        rows.append({'id': tid, 'name': text(item.get('name')) or '种子 #' + tid,
                     'small_descr': text(item.get('small_descr')), 'category': text(item.get('category'), 100),
                     'size': size, 'seeders': seeders, 'leechers': integer(item.get('leechers'))})
    rows.sort(key=lambda row: (row['seeders'], row['size'], int(row['id'])))
    synced = text(data.get('synced_at'), 30)
    try:
        dt.datetime.strptime(synced, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        synced = None
    return {'task': text(data.get('task'), 100), 'has_task': data['has_task'], 'has_record': data['has_record'],
            **counts, 'seeders_max': integer(data.get('seeders_max')), 'synced_at': synced,
            'candidates_total': integer(data.get('candidates_total')),
            'candidates_truncated': data.get('candidates_truncated') if type(data.get('candidates_truncated')) is bool else None,
            'returned_candidates': len(raw), 'valid_candidates': len(rows), 'items': rows}


class Client:
    def __init__(self, source, settings, *, fetcher=fetch_payload, rss_fetcher=None, clock=time.time, cache_path=None):
        self.source, self.settings = source, settings
        self.fetcher, self.rss_fetcher, self.clock = fetcher, rss_fetcher, clock
        self.lock = threading.Lock()
        self.api_refresh_lock, self.rss_refresh_lock = threading.Lock(), threading.Lock()
        self.generation = 0
        self.cached = self.fetched_at = self.attempted_at = self.error = None
        self.rss_cached = self.rss_fetched_at = self.rss_attempted_at = self.rss_error = None
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.restored_cache = self.cache_error = None
        self._restore_statistics()

    def _cache_identity(self):
        source = self.source()
        identity = json.dumps([source.get('api_base', ''), source.get('token', '')], ensure_ascii=False).encode('utf-8')
        return hashlib.sha256(identity).hexdigest()

    def _restore_statistics(self):
        if self.cache_path is None:
            return
        try:
            with self.cache_path.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                return
            saved = json.loads(raw.decode('utf-8'))
            if (not isinstance(saved, dict) or saved.get('version') != 1
                    or saved.get('identity') != self._cache_identity()
                    or type(saved.get('last_query_failed')) is not bool):
                return
            for key in ('fetched_at', 'attempted_at'):
                stamp = saved.get(key)
                if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp <= self.clock():
                    return
            value = sanitize({'ret': 0, 'data': {**saved['statistics'], 'candidates': []}})
            self.restored_cache = {**saved, 'statistics': {key: value[key] for key in STATISTICS_FIELDS},
                                   'error': '上次站端查询失败，等待后台更新' if saved['last_query_failed'] else None}
        except (OSError, ValueError, TypeError, KeyError, PtsError):
            return

    def _persist_statistics(self):
        if self.cache_path is None:
            return
        restored = self.restored_cache or {}
        value = self.cached if self.cached is not None else restored.get('statistics')
        if value is None:
            return
        temporary = None
        try:
            saved = {'version': 1, 'identity': self._cache_identity(),
                     'statistics': {key: value[key] for key in STATISTICS_FIELDS},
                     'fetched_at': self.fetched_at if self.cached is not None else restored['fetched_at'],
                     'attempted_at': self.attempted_at,
                     'last_query_failed': self.error is not None}
            descriptor, temporary = tempfile.mkstemp(prefix='.pts-statistics-', suffix='.tmp', dir=self.cache_path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(saved, stream, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.cache_path)
            temporary = None
            self.cache_error = None
        except Exception:
            self.cache_error = '站端缓存保存失败，重启后可能需要重新查询'
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def statistics(self):
        """Read a small display cache without network, candidate merging or downloader work."""
        settings = self.settings()
        with self.lock:
            restored = self.restored_cache or {}
            value = self.cached if self.cached is not None else restored.get('statistics')
            fetched = self.fetched_at if self.cached is not None else restored.get('fetched_at')
            attempted = self.attempted_at if self.attempted_at is not None else restored.get('attempted_at')
            error = self.error if self.attempted_at is not None else restored.get('error')
            expired = value is not None and (self.clock() < fetched
                or self.clock() - fetched >= settings.get('pts_cache_seconds', CACHE_SECONDS))
            return {**{key: value[key] for key in STATISTICS_FIELDS if value is not None},
                    'available': value is not None and error is None and not expired,
                    'stale': value is not None and (error is not None or expired),
                    'cache_expired': expired, 'restored': self.cached is None and value is not None,
                    'error': error, 'cache_error': self.cache_error, 'fetched_at': fetched, 'attempted_at': attempted,
                    'local_target': settings.get('target'), 'scope': 'statistics_cache'}

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.cached = self.fetched_at = self.attempted_at = self.error = None
            self.rss_cached = self.rss_fetched_at = self.rss_attempted_at = self.rss_error = None
            self.restored_cache = self.cache_error = None
            if self.cache_path is not None:
                try:
                    self.cache_path.unlink(missing_ok=True)
                except OSError:
                    self.cache_error = '旧站端缓存未能清除，请检查数据目录权限'

    def refresh_api(self, settings, force):
        with self.api_refresh_lock:
            with self.lock:
                generation = self.generation
                due = (force or self.attempted_at is None
                       or self.clock() - self.attempted_at >= settings.get('pts_cache_seconds', CACHE_SECONDS))
            if not due:
                return
            value, error = None, None
            try:
                source = self.source()
                payload = (fetch_payload(source, timeout=settings.get('site_timeout_seconds', 15), limit=settings.get('candidate_limit', 1000))
                           if self.fetcher is fetch_payload else self.fetcher(source))
                value = sanitize(payload)
            except PtsError as failure:
                error = str(failure)
            except Exception:
                error = 'PTS 查询失败，请稍后重试'
            with self.lock:
                if generation != self.generation:
                    return
                self.attempted_at, self.error = self.clock(), error
                if value is not None:
                    self.cached, self.fetched_at = value, self.clock()
                self._persist_statistics()

    def refresh_rss(self, settings, force):
        if self.rss_fetcher is None:
            return
        with self.rss_refresh_lock:
            with self.lock:
                generation = self.generation
                due = (force or self.rss_attempted_at is None
                       or self.clock() - self.rss_attempted_at >= max(rss.CACHE_SECONDS, settings.get('pts_cache_seconds', CACHE_SECONDS)))
            if not due:
                return
            value, error = None, None
            try:
                value = rss.sanitize(self.rss_fetcher(self.source(), settings))
            except rss.RssError as failure:
                error = str(failure)
            except Exception:
                error = 'RSS 查询失败，请稍后重试'
            with self.lock:
                if generation != self.generation:
                    return
                self.rss_attempted_at, self.rss_error = self.clock(), error
                if value is not None:
                    self.rss_cached, self.rss_fetched_at = value, self.clock()

    def snapshot(self, force=False, *, include_rss=True, refresh=True):
        settings = self.settings()
        if refresh:
            self.refresh_api(settings, force)
            if include_rss:
                self.refresh_rss(settings, force)
        with self.lock:
            value = dict(self.cached or {})
            api_rows = value.pop('items', [])
            rss_value = self.rss_cached or {} if include_rss else {}
            rss_rows = rss_value.get('items', [])
            api_available = self.error is None and self.cached is not None
            rss_available = include_rss and self.rss_error is None and self.rss_cached is not None
            status = {'available': api_available, 'stale': self.error is not None and self.cached is not None,
                      'error': self.error, 'fetched_at': self.fetched_at, 'attempted_at': self.attempted_at,
                      'rss_available': rss_available,
                      'rss_stale': include_rss and self.rss_error is not None and self.rss_cached is not None,
                      'rss_error': self.rss_error if include_rss else None,
                      'rss_fetched_at': self.rss_fetched_at if include_rss else None,
                      'rss_attempted_at': self.rss_attempted_at if include_rss else None}
        # A fresh API row wins a duplicate; a fresh RSS row wins over stale API data.
        merged = {row['id']: {**row, 'source': 'rss'} for row in rss_rows}
        for row in api_rows:
            prior = merged.get(row['id'])
            chosen = prior if prior is not None and not api_available and rss_available else row
            merged[row['id']] = {**chosen, 'source': 'both' if prior is not None else 'api'}
        settings = self.settings()
        items = [{**row, 'matches_filter': (0 < row['size'] < settings['max_bytes']
                                           and settings['min_seeders'] <= row['seeders'] <= settings['max_seeders'])}
                 for row in sorted(merged.values(), key=lambda row: (row['seeders'], row['size'], int(row['id'])))]
        return {**value, **status, 'items': items, 'filter_matches': sum(row['matches_filter'] for row in items),
                'api_valid_candidates': len(api_rows), 'rss_total': rss_value.get('total'),
                'rss_returned_candidates': len(rss_rows), 'rss_truncated': rss_value.get('truncated', False),
                'merged_candidates': len(items),
                'filter': {key: settings[key] for key in ('min_seeders', 'max_seeders', 'max_bytes')},
                'scope': 'api_and_rss_returned_candidates' if include_rss else 'api_returned_candidates'}
