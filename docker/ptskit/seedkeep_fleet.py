"""Instance-specific task management and shared, permanent hash deduplication."""
import copy
import hashlib
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse
import uuid
import seedkeep_configuration as configuration
import seedkeep_pull as pull
from seedkeep_downloaders import API, ManagementError, HASH, TR_FIELDS, count, hashes, inventory_rows, pts_url
from seedkeep_instances import Registry
from seedkeep_management import ACTIVE, DEFAULT_POLICY as LEGACY_POLICY, Manager, load, policy as legacy_policy
from torrent_meta import torrent_metadata
from seedkeep_transfer import Rules
from seedkeep_cleanup import Cleanup
import seedkeep_tagging as tagging

DEFAULT_POLICY = {**LEGACY_POLICY, 'unregistered_enabled': False, 'unregistered_wait_hours': 24,
                  'unregistered_interval_hours': 24, 'unregistered_max_per_run': 20,
                  'unregistered_scope': 'pts', 'unregistered_instances': []}
UNREGISTERED = re.compile(r'(?:\bunregistered\s+torrent\b|\btorrent\s+(?:is\s+)?not\s+(?:registered|found)\b|'
                          r'\btorrent\s+(?:(?:has\s+been|was|is)\s+)?deleted\b|\bunknown\s+torrent\b|'
                          r'种子(?:已被|已|被)?删除|种子不存在|种子未注册)', re.I)
GENERAL_FAILURE = re.compile(r'(?:unauthori[sz]ed|forbidden|authentication|passkey|invalid\s+(?:key|token)|'
                             r'timed?\s*out|timeout|connection|resolve|network|认证|权限|密钥|超时|网络)', re.I)


def policy(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULT_POLICY):
        raise ManagementError('清理设置字段无效')
    legacy_policy({key: values[key] for key in LEGACY_POLICY})
    if type(values['unregistered_enabled']) is not bool:
        raise ManagementError('未注册清理开关无效')
    for key in ('unregistered_wait_hours', 'unregistered_interval_hours'):
        number = values[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not 1 <= number <= 8760:
            raise ManagementError('未注册等待与检查间隔应为 1–8760 小时')
    if type(values['unregistered_max_per_run']) is not int or not 1 <= values['unregistered_max_per_run'] <= 50:
        raise ManagementError('未注册每轮清理上限应为 1–50 个')
    if values['unregistered_scope'] not in ('pts', 'all'):
        raise ManagementError('未注册清理范围无效')
    ids = values['unregistered_instances']
    if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(v, str) for v in ids) or len(ids) != len(set(ids)):
        raise ManagementError('未注册实例范围无效')
    return copy.deepcopy(values)


def unregistered_report(tracks, client, source, now, fresh_seconds, scope='pts'):
    """True only for explicit trusted, current failures; unknown/conflicts reset waiting."""
    reports = []
    for track in tracks:
        address = track.get('url' if client == 'qb' else 'announce', '')
        try:
            parsed = urllib.parse.urlsplit(address)
            trusted = bool(parsed.scheme in ('http', 'https') and parsed.hostname and parsed.username is None)
        except (ValueError, TypeError):
            trusted = False
        if not trusted or (scope == 'pts' and not pts_url(address, source)):
            continue
        if client == 'qb':
            status = track.get('status')
            message = str(track.get('msg', ''))
            success = status == 2
            failed = status == 4
            fresh = status in (2, 4)
        else:
            stamp = max((track.get(key, 0) for key in ('lastAnnounceTime', 'lastScrapeTime')
                         if type(track.get(key, 0)) in (int, float)), default=0)
            fresh = math.isfinite(stamp) and 0 < stamp <= now + 60 and now - stamp <= fresh_seconds
            success = bool(track.get('lastAnnounceSucceeded') or track.get('lastScrapeSucceeded'))
            message = ' '.join(str(track.get(key, '')) for key in ('lastAnnounceResult', 'lastScrapeResult'))
            failed = not success
        explicit = bool(UNREGISTERED.search(message))
        if not fresh or GENERAL_FAILURE.search(message) or (success and explicit):
            reports.append(None)
        elif failed and explicit:
            reports.append(True)
        elif success:
            reports.append(False)
        else:
            reports.append(None)
    if reports and all(value is True for value in reports):
        return True
    if reports and all(value is False for value in reports):
        return False
    return None


class Fleet:
    def __init__(self, directory, source_callable, settings_callable, pts_snapshot, guard, legacy_manager,
                 api_factory=API, clock=time.time, runner_busy=lambda: False, refill_due=lambda: False, cleanup_storage=None):
        self.directory = Path(directory)
        self.source, self.settings, self.pts, self.guard = source_callable, settings_callable, pts_snapshot, guard
        self.legacy, self.api_factory, self.clock = legacy_manager, api_factory, clock
        self.runner_busy = runner_busy
        self.refill_due = refill_due
        self.registry = Registry(source_callable, settings_callable, api_factory)
        self.lock = threading.RLock()
        self.state_path, self.policy_path = self.directory / 'fleet_state.json', self.directory / 'fleet_settings.json'
        defaults = {**DEFAULT_POLICY, **(legacy_manager.config if legacy_manager else {})}
        self.config = policy(load(self.policy_path, defaults))
        self.redactions = None
        self.state = load(self.state_path, {'observations': {}, 'last_unregistered_cleanup': None})
        self.cache, self.cache_at, self.rows, self.raw, self.limit_cache = None, None, {}, {}, {}
        self.managers = {}
        self.last_monitor = None
        pairs = self.directory / 'fleet_pairs'
        if pairs.exists():
            for path in sorted(pairs.glob('*/pair.json')):
                pair = load(path, {})
                if set(pair) != {'source_instance_id', 'target_instance_id'}:
                    raise ManagementError('转种恢复配对资料无效', 409)
                self._manager(pair['source_instance_id'], pair['target_instance_id'], path.parent)
        self.transfer_rules = Rules(self)
        self.cleanup = Cleanup(self, storage=cleanup_storage)

    def _save(self):
        pull.save_json(self.state_path, self.state)

    def _log(self, event, reason=None, processed=0, errors=0, **counters):
        if self.legacy is not None:
            self.legacy.log(event, reason=reason, processed=processed, errors=errors, **counters)

    def _clear_instance_observations(self, instance_id):
        prefix = instance_id + ':'
        self.state['observations'] = {key: value for key, value in self.state.get('observations', {}).items()
                                      if not key.startswith(prefix)}
        self._save()

    def _safe_text(self, text):
        value = str(text)
        if self.redactions is None:
            settings = self.settings()
            resolved = configuration.resolve_source(self.source(), settings)
            secrets = [resolved.get('token', ''), resolved.get('password', ''), resolved.get('username', '')]
            try:
                login = configuration.tr_credentials(settings)
                secrets += [login.get('username', ''), login.get('password', '')]
            except configuration.ConfigurationError:
                pass
            for row in self.registry.items():
                secrets += [self.registry._password(row), row.get('username', ''), row.get('url', ''), row.get('proxy_url', '')]
            self.redactions = sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True)
        for secret in self.redactions:
            value = value.replace(secret, '[隐藏]')
        value = re.sub(r'https?://[^\s]+', '[地址隐藏]', value, flags=re.I)
        value = re.sub(r'(?i)(passkey|token|password|authorization)\s*[=:]\s*[^\s&,;]+', r'\1=[隐藏]', value)
        return value[:500]

    def _all_managers(self):
        return ([self.legacy] if self.legacy is not None else []) + list(self.managers.values())

    def busy(self):
        with self.lock:
            return (self.runner_busy() or self.cleanup.busy()
                    or any((manager.state.get('job') or {}).get('status') in ACTIVE for manager in self._all_managers()))

    def public_job(self):
        with self.lock:
            managers = self._all_managers()
            active = [m for m in managers if (m.state.get('job') or {}).get('status') in ACTIVE]
            candidates = active or [m for m in managers if m.state.get('job')]
            if not candidates:
                return None
            manager = max(candidates, key=lambda m: m.state['job'].get('started_at', 0))
            job = manager.public_job()
            private = manager.state['job']
            source_id = private.get('source_instance_id', 'qb' if manager is self.legacy else None)
            target_id = private.get('target_instance_id', 'tr' if manager is self.legacy else None)
            job.update(source_instance_id=source_id, target_instance_id=target_id)
            for item in job['items']:
                item['instance_id'] = source_id
                item['name'] = self._safe_text(item.get('name', ''))
                if item.get('error'):
                    item['error'] = self._safe_text(item['error'])
            if job.get('error'):
                job['error'] = self._safe_text(job['error'])
            return job

    def invalidate(self, reset_observations=False):
        with self.lock:
            self.cache, self.cache_at, self.rows, self.raw, self.limit_cache = None, None, {}, {}, {}
            self.last_monitor = None
            if reset_observations:
                self.state['observations'] = {}
                self.state['last_unregistered_cleanup'] = None
                self._save()
                self.cleanup.interrupt()
            for manager in self._all_managers():
                manager.invalidate(reset_observations)
            self.redactions = None

    def policy(self):
        with self.lock:
            return copy.deepcopy(self.config)

    def update_policy(self, body):
        value = policy(body)
        if value['unregistered_instances']:
            valid = {row['id'] for row in self.registry.items()}
            if not set(value['unregistered_instances']).issubset(valid):
                raise ManagementError('未注册清理范围包含不存在的实例')
        with self.lock:
            if self.busy():
                raise ManagementError('已有管理作业，请完成或取消后再修改清理设置', 409)
            with self.guard():
                pull.save_json(self.policy_path, value)
                self.config = value
                self.invalidate(True)
        self._log('fleet_cleanup_settings_saved')
        return {'ok': True, 'policy': self.policy()}

    def _threshold(self, site_snapshot=None):
        settings = self.settings()
        try:
            site = self.pts() if site_snapshot is None else site_snapshot
            site_limit = count(site.get('seeders_max')) if site.get('available') and not site.get('stale') else None
        except Exception:
            site_limit = None
        mode = settings.get('seeders_limit_mode', 'site')
        return settings.get('manual_seeders_max', 10) if mode == 'manual' else site_limit, site_limit, mode

    def _unregistered_scope(self, instance, row):
        return (self.config['unregistered_enabled'] and
                (not self.config['unregistered_instances'] or instance['id'] in self.config['unregistered_instances']) and
                (self.config['unregistered_scope'] == 'all' or row['pts']))

    def _make_rows(self, instance, torrents, api, connected, batch, threshold, now):
        kind, settings = instance['type'], self.settings()
        source = configuration.resolve_source(self.source(), settings)
        if connected:
            tagging.inventory_counts(batch, torrents if kind == 'qb' else [],
                torrents if kind == 'tr' else [], settings.get('managed_tag', tagging.DEFAULT_TAG))
        rows = inventory_rows(torrents if kind == 'qb' else [], torrents if kind == 'tr' else [],
                              source, batch, threshold, now, settings)
        copies_by_hash = {}
        if connected:
            for native in tagging.inventory_tasks(torrents):
                copies_by_hash.setdefault(native['hash' if kind == 'qb' else 'hashString'].lower(), []).append(native)
        for row in rows:
            task = row['_qb'] if kind == 'qb' else row['_tr']
            row.update(id=instance['id'] + ':' + row['hash'], instance_id=instance['id'],
                       instance_name=self._safe_text(instance['name']), client=kind, stale=not connected,
                       name=self._safe_text(row['name']), state=task.get('state' if kind == 'qb' else 'status'),
                       category=task.get('category', '') if kind == 'qb' else '',
                       tag=task.get('tags', '') if kind == 'qb' else ', '.join(task.get('labels', [])),
                       category_kind='qb_category' if kind == 'qb' else 'tr_labels',
                       unregistered=None, unregistered_since=None, unregistered_ready=False)
            try:
                row['managed'] = connected and tagging.has_tag(task, kind, settings.get('managed_tag', tagging.DEFAULT_TAG))
            except tagging.TaggingError:
                row['managed'] = False
            row['category'], row['tag'] = self._safe_text(row['category']), self._safe_text(row['tag'])
            tracks = None
            if connected:
                if kind == 'tr':
                    tracks = task.get('trackerStats', [])
                elif row['validity'] == 'invalid' or self._unregistered_scope(instance, row):
                    try:
                        tracks = api.qtrackers(row['hash'])
                        if not isinstance(tracks, list) or any(not isinstance(t, dict) for t in tracks):
                            raise ValueError()
                    except Exception:
                        tracks = None
                if kind == 'qb' and row['validity'] == 'invalid':
                    trusted = [t for t in tracks or [] if pts_url(t.get('url'), source)]
                    seeds = [t['num_seeds'] for t in trusted
                             if t.get('status') == 2 and count(t.get('num_seeds')) is not None]
                    if not seeds or len(seeds) != len(trusted) or min(seeds) <= threshold:
                        row.update(validity='unknown', validity_text='未知', reason='当前 Tracker 未确认持续超限')
                    else:
                        row['seeders'] = max(seeds)
                if tracks is not None and self._unregistered_scope(instance, row):
                    row['unregistered'] = unregistered_report(tracks, kind, source, now,
                                                              settings.get('tracker_fresh_seconds', 7200),
                                                              self.config['unregistered_scope'])
                registration = unregistered_report(tracks or [], kind, source, now,
                    settings.get('tracker_fresh_seconds', 7200), 'pts') if tracks is not None else None
                row['_seeding_validity'] = 'invalid' if registration is True else row['validity']
            if not connected:
                row.update(validity='unknown', validity_text='未知', reason='下载器读取失败，保留上次任务', can_transfer=False)
            row['_connected'] = connected
            if connected:
                copies = [copy for alias in row['_aliases'] for copy in copies_by_hash.get(alias, [])]
                if len(copies) > 1:
                    # Preserve every native copy for display; execution rows keep their existing merge.
                    row['_display_members'] = [self._make_rows(instance, [copy], api, True, batch, threshold, now)[0]
                                               for copy in copies]
        return rows

    def _observe(self, row, threshold, site_limit, mode, now, previous):
        observation = {}
        settings = self.settings()
        gap = settings.get('monitor_interval_seconds', 60) * 3
        # Site availability remains mandatory for the old destructive invalidity rule.
        if row['_connected'] and row['validity'] == 'invalid' and site_limit is not None:
            old = previous.get('invalid', {})
            continuous = (old.get('threshold') == threshold and old.get('mode') == mode and
                          0 <= now - old.get('last_seen', 0) <= gap)
            since = old.get('since', now) if continuous else now
            observation['invalid'] = {'since': since, 'last_seen': now, 'threshold': threshold, 'mode': mode}
            row.update(invalid_since=since, delete_after=since + self.config['cleanup_wait_hours'] * 3600)
            row['delete_ready'] = now >= row['delete_after']
        if row['_connected'] and row['unregistered'] is True:
            old = previous.get('unregistered', {})
            continuous = old.get('scope') == self.config['unregistered_scope'] and 0 <= now - old.get('last_seen', 0) <= gap
            since = old.get('since', now) if continuous else now
            observation['unregistered'] = {'since': since, 'last_seen': now, 'scope': self.config['unregistered_scope']}
            row.update(unregistered_since=since, unregistered_ready=now >= since + self.config['unregistered_wait_hours'] * 3600)
        return observation

    def snapshot(self, force=False, *, site_snapshot=None):
        with self.lock:
            now, settings = self.clock(), self.settings()
            if force or self.cache is None or self.cache_at is None or now - self.cache_at >= settings.get('management_cache_seconds', 30):
                self.redactions = None
                instances = self.registry.items()
                threshold, site_limit, mode = self._threshold(site_snapshot)
                batch = load(self.directory / 'batch.json', {})
                previous, observations, rows, summaries, raw = self.state.get('observations', {}), {}, {}, [], {}
                for instance in instances:
                    kind, iid = instance['type'], instance['id']
                    connected, error, torrents, api = False, None, [], None
                    if instance['enabled']:
                        try:
                            api = self.registry.api(iid)
                            torrents = api.inventory_one()
                            connected = True
                        except Exception:
                            torrents = copy.deepcopy(self.raw.get(iid, []))
                            error = '下载器读取失败，请检查连接和权限'
                    try:
                        selected = self._make_rows(instance, torrents, api, connected, batch, threshold, now)
                    except Exception:
                        connected = False
                        error = '下载器任务响应无效，保留上次任务'
                        torrents = copy.deepcopy(self.raw.get(iid, []))
                        selected = self._make_rows(instance, torrents, api, False, batch, threshold, now)
                    raw[iid] = torrents
                    for row in selected:
                        observation = self._observe(row, threshold, site_limit, mode, now, previous.get(row['id'], {}))
                        if observation:
                            observations[row['id']] = observation
                        rows[row['id']] = row
                    summaries.append({'id': iid, 'type': kind, 'name': self._safe_text(instance['name']),
                                      'enabled': instance['enabled'], 'connected': connected, 'error': error,
                                      'total': len(selected), 'upload_speed': sum(row['upload_speed'] for row in selected),
                                      'download_speed': sum(row['download_speed'] for row in selected)})
                self.state['observations'] = observations
                if observations != previous:
                    self._save()
                self.rows, self.raw = rows, raw
                self.cache = {'instances': summaries, 'seeders_max': threshold, 'site_seeders_max': site_limit,
                              'seeders_limit_mode': mode}
                self.cache['seedkeep'] = tagging.summarize_rows(rows.values(), summaries,
                    settings.get('managed_tag', tagging.DEFAULT_TAG), now, threshold, state=batch)
                self.cache['seedkeep_display'] = tagging.summarize_display(rows.values(), summaries,
                    settings.get('managed_tag', tagging.DEFAULT_TAG), now, threshold, state=batch)
                self.cache_at = now
            job = self.public_job()
            active = self.busy()
            items = []
            for row in self.rows.values():
                public = {key: copy.deepcopy(value) for key, value in row.items() if not key.startswith('_')}
                public.update(self.cleanup.task_annotation(row))
                if active:
                    public.update(delete_ready=False, unregistered_ready=False, can_transfer=False)
                items.append(public)
            items.sort(key=lambda row: (not row['pts'], row['name'].lower(), row['instance_id'], row['hash']))
            # Alias groups also deduplicate hybrid v1/v2 tasks across instances.
            groups = []
            for row in self.rows.values():
                matches = [group for group in groups if group['aliases'] & row['_aliases']]
                if matches:
                    group = matches[0]
                    for other in matches[1:]:
                        group['aliases'].update(other['aliases'])
                        group['managed'] |= other['managed']
                        group['pts'] |= other['pts']
                        groups.remove(other)
                    group['aliases'].update(row['_aliases'])
                    group['managed'] |= row['managed']
                    group['pts'] |= row['pts']
                else:
                    groups.append({'aliases': set(row['_aliases']), 'managed': row['managed'], 'pts': row['pts']})
            totals = {'total': len(items), 'unique': len(groups), 'unique_total': len(groups),
                      'managed_active': sum(g['managed'] for g in groups), 'managed_unique': sum(g['managed'] for g in groups),
                      'pts': sum(row['pts'] for row in items), 'pts_unique': sum(g['pts'] for g in groups),
                      'completed': sum(row['completed'] for row in items),
                      'qb_completed': sum(row['client'] == 'qb' and row['completed'] for row in items),
                      **{key: sum(row['validity'] == key for row in items) for key in ('valid', 'invalid', 'unknown', 'inactive', 'downloading')},
                      'ready': sum(row['delete_ready'] or row['unregistered_ready'] for row in items),
                      'transferable': sum(row['can_transfer'] for row in items)}
            return {**copy.deepcopy(self.cache), 'items': items, 'totals': totals, 'policy': self.policy(),
                    'checked_at': self.cache_at, 'job': job, 'cleanup': self.cleanup.summary()}

    def limits(self, instance_id, force=False):
        with self.lock:
            instance = self.registry.get(instance_id)
            cached = self.limit_cache.get(instance_id)
            if force or not cached or self.clock() - cached['checked_at'] >= self.settings().get('management_cache_seconds', 30):
                try:
                    values = self.registry.api(instance_id).limits()[instance['type']]
                except Exception:
                    values = {'connected': False, 'error': '限速读取失败，请检查连接和权限'}
                cached = {'instance_id': instance_id, 'type': instance['type'], 'name': self._safe_text(instance['name']),
                          'checked_at': self.clock(), 'values': values}
                self.limit_cache[instance_id] = cached
            return copy.deepcopy(cached)

    def set_limits(self, body):
        if not isinstance(body, dict) or set(body) - {'instance_id', 'upload_kib', 'download_kib', 'disable_alternative'}:
            raise ManagementError('限速参数无效')
        with self.lock:
            if self.busy():
                raise ManagementError('已有管理作业，请等待完成', 409)
            instance = self.registry.get(body.get('instance_id'))
            values = {key: value for key, value in body.items() if key != 'instance_id'}
            values['client'] = instance['type']
            with self.guard():
                self.registry.api(instance['id']).set_limits(values)
                self.limit_cache.pop(instance['id'], None)
                result = self.limits(instance['id'], True)
            return {'ok': result['values']['connected'], **result}

    def _tasks(self, body, maximum=None):
        tasks = body.get('tasks') if isinstance(body, dict) else None
        if not isinstance(tasks, list) or not tasks or (maximum is not None and len(tasks) > maximum):
            raise ManagementError('请选择至少 1 个任务' if maximum is None else '请选择 1–' + str(maximum) + ' 个任务')
        result, seen = [], set()
        for task in tasks:
            if not isinstance(task, dict) or set(task) != {'instance_id', 'hash'} or not isinstance(task['instance_id'], str):
                raise ManagementError('任务须明确指定实例及哈希')
            value = hashes([task['hash']])[0]
            key = (task['instance_id'], value)
            if key not in seen:
                self.registry.get(key[0])
                result.append({'instance_id': key[0], 'hash': key[1]})
                seen.add(key)
        return result

    def _current_row(self, instance, api, value):
        torrents = api.inventory_one()
        matching = [t for t in torrents if str(t.get('hash' if instance['type'] == 'qb' else 'hashString', '')).lower() == value]
        if len(matching) != 1:
            raise ManagementError('任务身份无法确认，请刷新实例列表', 409)
        threshold, site_limit, mode = self._threshold()
        rows = self._make_rows(instance, matching, api, True, load(self.directory / 'batch.json', {}), threshold, self.clock())
        if len(rows) != 1 or rows[0]['hash'] != value:
            raise ManagementError('任务身份无法确认', 409)
        return rows[0], threshold, site_limit, mode

    def _remove(self, instance, api, value, expired, operation):
        row, threshold, site_limit, mode = self._current_row(instance, api, value)
        key, now = row['id'], self.clock()
        old = self.state.get('observations', {}).get(key, {})
        observation = self._observe(row, threshold, site_limit, mode, now, old)
        if observation:
            self.state['observations'][key] = observation
        else:
            self.state['observations'].pop(key, None)
        self._save()
        if expired and not (row['delete_ready'] or row['unregistered_ready']):
            raise ManagementError('当前报告未满足连续等待条件，已跳过删除', 409)
        task = row['_qb'] or row['_tr']
        recovery = {'instance_id': instance['id'], 'type': instance['type'], 'hash': value,
                    'size': row['size'], 'at': now, 'reason': 'expired' if expired else 'manual'}
        if instance['type'] == 'qb':
            data = api.qexport(value)
            meta = torrent_metadata(data)
            if not set(meta['hashes']) & row['_aliases'] or meta['size'] != row['size']:
                raise ManagementError('种子身份或体积核验失败，未删除任务', 409)
            backup = operation / (instance['id'] + '-' + value + '.torrent')
            backup.write_bytes(data)
            os.chmod(backup, 0o600)
            recovery['download_dir'] = task['save_path']
        else:
            recovery['download_dir'] = task['downloadDir']
            recovery['magnet'] = 'magnet:?xt=urn:btih:' + value if len(value) == 40 else 'magnet:?xt=urn:btmh:1220' + value
        pull.save_json(operation / (instance['id'] + '-' + value + '.json'), recovery)
        # A second current read closes the export/backup window and rechecks destructive conditions.
        checked, threshold, site_limit, mode = self._current_row(instance, api, value)
        current_task = checked['_qb'] or checked['_tr']
        directory_field = 'save_path' if instance['type'] == 'qb' else 'downloadDir'
        if checked['size'] != row['size'] or current_task.get(directory_field) != task.get(directory_field):
            raise ManagementError('任务目录或体积已变化，未删除任务', 409)
        if expired:
            observation = self._observe(checked, threshold, site_limit, mode, self.clock(), observation)
            if observation:
                self.state['observations'][key] = observation
            else:
                self.state['observations'].pop(key, None)
            self._save()
            if not (checked['delete_ready'] or checked['unregistered_ready']):
                raise ManagementError('删除前报告已恢复或未知，未删除任务', 409)
        if checked['pts'] or checked['managed']:
            path = self.directory / 'batch.json'
            batch = load(path, {})
            batch['seen_hashes'] = sorted(set(batch.get('seen_hashes', [])) | checked['_aliases'])
            pull.save_json(path, batch)
        if instance['type'] == 'qb':
            api.qremove(value)
        else:
            api.rpc('torrent-remove', {'ids': [value], 'delete-local-data': False})
        remaining = api.inventory_one()
        if any(str(t.get('hash' if instance['type'] == 'qb' else 'hashString', '')).lower() == value for t in remaining):
            raise ManagementError('任务移除结果未确认，请刷新检查', 502)
        self.state['observations'].pop(key, None)
        self._save()

    def action(self, body):
        if not isinstance(body, dict) or set(body) - {'action', 'tasks', 'confirm'}:
            raise ManagementError('任务操作参数无效')
        action = body.get('action')
        if action not in ('start', 'stop', 'verify', 'remove', 'delete_expired'):
            raise ManagementError('任务操作无效')
        deleting = action in ('remove', 'delete_expired')
        if deleting and body.get('confirm') != 'REMOVE_TASKS_KEEP_DATA':
            raise ManagementError('请确认只移除任务并保留数据')
        tasks = self._tasks(body, self.settings().get('delete_max_per_job', 50))
        with self.lock:
            if self.busy():
                raise ManagementError('已有管理作业，请等待完成或取消', 409)
            with self.guard():
                results, groups = [], {}
                operation = None
                if deleting:
                    operation = self.directory / 'operations' / uuid.uuid4().hex
                    operation.mkdir(parents=True, mode=0o700)
                    os.chmod(operation.parent, 0o700)
                    os.chmod(operation, 0o700)
                for task in tasks:
                    groups.setdefault(task['instance_id'], []).append(task)
                for iid, group in groups.items():
                    try:
                        instance, api = self.registry.get(iid), self.registry.api(iid)
                        current = api.inventory_one()
                        present = {str(t.get('hash' if instance['type'] == 'qb' else 'hashString', '')).lower() for t in current}
                        if not all(task['hash'] in present for task in group):
                            raise ManagementError('所选任务已变化，请刷新列表', 409)
                        if not deleting:
                            values = [task['hash'] for task in group]
                            if instance['type'] == 'qb':
                                operation_name = {'start': 'start', 'stop': 'stop', 'verify': 'recheck'}[action]
                                api.qpost('torrents/' + operation_name, {'hashes': '|'.join(values)})
                            else:
                                api.rpc('torrent-' + {'start': 'start', 'stop': 'stop', 'verify': 'verify'}[action], {'ids': values})
                    except Exception:
                        if deleting:
                            self._clear_instance_observations(iid)
                        results.extend({**task, 'ok': False, 'error': '实例连接或任务状态未确认，操作未完成'} for task in group)
                        continue
                    for task in group:
                        try:
                            if deleting:
                                self._remove(instance, api, task['hash'], action == 'delete_expired', operation)
                            results.append({**task, 'ok': True, 'error': None})
                        except Exception:
                            if deleting:
                                self._clear_instance_observations(iid)
                            results.append({**task, 'ok': False, 'error': '任务操作未确认；文件保留，请刷新检查条件和恢复资料'})
                self.cache_at = None
                self._log('fleet_tasks_processed', reason=action, processed=sum(result['ok'] for result in results),
                          errors=sum(not result['ok'] for result in results))
                return {'ok': all(result['ok'] for result in results), 'results': results}

    def _manager(self, source_id, target_id, state_directory=None):
        key = (source_id, target_id)
        if key not in self.managers:
            if not isinstance(source_id, str) or not isinstance(target_id, str):
                raise ManagementError('转种恢复实例标识无效', 409)
            digest = hashlib.sha256((source_id + '\0' + target_id).encode()).hexdigest()
            path = Path(state_directory) if state_directory else self.directory / 'fleet_pairs' / digest
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o700)
            metadata = {'source_instance_id': source_id, 'target_instance_id': target_id}
            if not (path / 'pair.json').exists():
                pull.save_json(path / 'pair.json', metadata)
            manager = Manager(self.directory, lambda: self.registry.pair(source_id, target_id)[0],
                              lambda: self.registry.pair(source_id, target_id)[1], self.pts, self.guard,
                              client_factory=self.api_factory, clock=self.clock, state_directory=path,
                              transfer_mode=getattr(self.legacy, 'transfer_mode', 'native'))
            if manager.state.get('job'):
                manager.state['job'].update(metadata)
                manager.save()
            self.managers[key] = manager
        return self.managers[key]

    def transfer(self, body):
        if not isinstance(body, dict) or set(body) != {'tasks', 'target_instance_id', 'confirm'} or body.get('confirm') != 'MOVE_QB_TO_TR_KEEP_DATA':
            raise ManagementError('请确认 qB 转至指定 TR 并保留数据')
        tasks = self._tasks(body)
        sources = {task['instance_id'] for task in tasks}
        if len(sources) != 1:
            raise ManagementError('同一转种批次只能选择一个 qB 来源')
        source_id, target_id = next(iter(sources)), body['target_instance_id']
        self.registry.pair(source_id, target_id)
        with self.lock:
            if self.busy():
                raise ManagementError('已有管理作业，请等待完成或取消', 409)
            self.transfer_rules.observe()
            manager = self._manager(source_id, target_id)
            manager.begin('transfer', {'hashes': [task['hash'] for task in tasks], 'confirm': body['confirm']})
            manager.state['job'].update(source_instance_id=source_id, target_instance_id=target_id)
            manager.save()
            self.cache_at = None
            return {'ok': True, 'job': self.public_job()}

    def cancel(self, body):
        with self.lock:
            for manager in self._all_managers():
                job = manager.state.get('job')
                if isinstance(body, dict) and job and body.get('job_id') == job['id']:
                    manager.cancel(body)
                    self.cache_at = None
                    return {'ok': True, 'job': self.public_job()}
        raise ManagementError('没有可取消的当前转种', 409)

    def _tick(self):
        with self.lock:
            self.transfer_rules.observe()
            active = [m for m in self._all_managers() if (m.state.get('job') or {}).get('status') in ACTIVE]
            if active:
                # Persisted jobs are resumed before any new fleet operation.
                immediate = active[0].tick(allow_cleanup=False)
                self.cache_at = None
                self.transfer_rules.observe()
                return immediate
            if self.cleanup.busy():
                immediate = self.cleanup.step()
                self.cache_at = None
                return immediate
            if self.runner_busy():
                self.cleanup.defer()
                return
            allow_cleanup = not self.refill_due() or self.state.get('automation_turn', 'cleanup') == 'cleanup'
            if self.cleanup.tick(allow_execute=allow_cleanup):
                self.cache_at = None
                return True
            self.transfer_rules.tick()
            if self.busy():
                return
            if self.last_monitor is not None and self.clock() - self.last_monitor < self.settings().get('monitor_interval_seconds', 60):
                return
            value = self.snapshot(True)
            self.last_monitor = self.clock()
            summary = value['seedkeep']
            self._log('inventory_summary' if summary['connected'] else 'inventory_unavailable',
                      reason=None if summary['connected'] else '下载器库存或标签读取失败，本次数量未知',
                      **tagging.log_counters(summary))
            selected = []
            if self.config['cleanup_enabled']:
                selected += [row for row in value['items'] if row['delete_ready']][:self.config['cleanup_max_per_run']]
            last = self.state.get('last_unregistered_cleanup')
            due = self.config['unregistered_enabled'] and (last is None or self.clock() - last >= self.config['unregistered_interval_hours'] * 3600)
            completed = True
            unregistered = []
            if due:
                scope = set(self.config['unregistered_instances'])
                checked = [item for item in value['instances'] if item['enabled'] and (not scope or item['id'] in scope)]
                completed = all(item['connected'] for item in checked)
                unregistered = [row for row in value['items'] if row['unregistered_ready']][:self.config['unregistered_max_per_run']]
                selected += unregistered
            if selected:
                unique = {row['id']: {'instance_id': row['instance_id'], 'hash': row['hash']} for row in selected}
                result = self.action({'action': 'delete_expired', 'tasks': list(unique.values())[:self.settings().get('delete_max_per_job', 50)],
                                      'confirm': 'REMOVE_TASKS_KEEP_DATA'})
                completed = completed and result['ok']
                processed = {(item['instance_id'], item['hash']) for item in result['results'] if item['ok']}
                completed = completed and all((row['instance_id'], row['hash']) in processed for row in unregistered)
            if due and completed:
                self.state['last_unregistered_cleanup'] = self.clock()
                self._save()

    def monitor(self, stop):
        delay = 5
        while not stop.wait(delay):
            delay = 5
            try:
                if self._tick():
                    delay = 0
            except Exception:
                # Monitoring interruption never counts as a continuous destructive observation.
                with self.lock:
                    self.state['observations'] = {}
                    self.cache_at = None
                    self._save()
                    self.cleanup.interrupt()
                self._log('fleet_monitor_failed', errors=1)
