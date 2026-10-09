"""Persistent invalidity observations and recoverable task-only cleanup / qB-to-TR transfer."""
import base64
import contextlib
import copy
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
import seedkeep_pull as pull
from seedkeep_downloaders import API, ManagementError, TR_FIELDS, completed_qb, count, hashes, inventory_rows, pts_url, transfer_path
from torrent_meta import torrent_metadata
import seedkeep_configuration as configuration
import seedkeep_logstore as logstore

import seedkeep_tagging as tagging
import seedkeep_transfer as transfer_rules
DEFAULT_POLICY = {'cleanup_enabled': False, 'cleanup_wait_hours': 24, 'cleanup_max_per_run': 20}
ACTIVE = ('waiting', 'running')
FINISHED_PHASES = ('completed', 'failed', 'cancelled', 'skipped')
VERIFY_TIMEOUT = 6 * 3600


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else copy.deepcopy(default)


def policy(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULT_POLICY):
        raise ManagementError('清理设置字段无效')
    if type(values['cleanup_enabled']) is not bool:
        raise ManagementError('自动清理开关无效')
    wait = values['cleanup_wait_hours']
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 1 <= wait <= 8760:
        raise ManagementError('失效等待时间应为 1–8760 小时')
    maximum = values['cleanup_max_per_run']
    if type(maximum) is not int or not 1 <= maximum <= 50:
        raise ManagementError('每轮清理上限应为 1–50 个')
    return dict(values)


class Manager:
    def __init__(self, directory, source, settings, pts, guard, *, client_factory=API, clock=time.time, state_directory=None, transfer_mode='native'):
        if transfer_mode not in ('native', 'full'):
            raise ValueError('transfer_mode must be native or full')
        self.transfer_mode = transfer_mode
        self.directory = Path(directory)
        self.state_directory = Path(state_directory) if state_directory is not None else self.directory
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_directory, 0o700)
        self.source, self.settings, self.pts, self.guard = source, settings, pts, guard
        self.client_factory, self.clock = client_factory, clock
        self.lock = threading.RLock()
        self.state_path = self.state_directory / 'downloader_state.json'
        self.policy_path = self.state_directory / 'downloader_settings.json'
        self.state = load(self.state_path, {'observations': {}, 'job': None})
        self.config = policy(load(self.policy_path, DEFAULT_POLICY))
        self.cache, self.cache_at, self.raw = None, None, ([], [])
        self.limits_cache, self.limits_at = None, None
        self.last_monitor = None
        self.last_transfer_log = None
        job = self.state.get('job')
        if job and job['status'] in ACTIVE:
            if all(item['phase'] in FINISHED_PHASES for item in job['items']):
                self.finish_job(job)
            else:
                if job['kind'] == 'transfer':
                    job['status'] = 'waiting'
                else:
                    job.update(status='interrupted', finished_at=self.clock(), error='服务重启，清理已中断；请刷新后重新选择')
                self.save()

    def save(self):
        pull.save_json(self.state_path, self.state)

    def api(self):
        return self.client_factory(self.source(), self.settings())

    def invalidate(self, reset_observations=False):
        with self.lock:
            self.cache, self.cache_at, self.raw = None, None, ([], [])
            self.limits_cache, self.limits_at, self.last_monitor = None, None, None
            if reset_observations and self.state.get('observations'):
                self.state['observations'] = {}
                self.save()

    @staticmethod
    def target_directory(item):
        source = item['source']
        return source.get('tr_download_dir', source['download_dir'])

    def log(self, event, reason=None, processed=0, errors=0, **counters):
        job = self.state.get('job') or {}
        if job.get('kind') == 'transfer' and event.startswith(('transfer_', 'management_task_', 'management_job_')):
            counters = {**self.transfer_counters(job), **counters}
        logstore.append(self.directory, 'management.log',
                        {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event,
                         'reason': reason, 'processed': processed, 'errors': errors, **counters})

    @staticmethod
    def transfer_counters(job):
        items = job.get('items', [])
        return {'transfer_total': len(items),
                **{'transfer_' + phase: sum(item['phase'] == phase for item in items) for phase in FINISHED_PHASES},
                'transfer_waiting': sum(item['phase'] not in FINISHED_PHASES for item in items)}

    def log_transfer_progress(self, job):
        if job.get('kind') != 'transfer':
            return
        counters = self.transfer_counters(job)
        signature = (job.get('id'), job.get('status'), tuple(counters.values()))
        if signature != self.last_transfer_log:
            self.last_transfer_log = signature
            self.log('transfer_progress', **counters)

    def public_job(self):
        job = self.state.get('job')
        if not job:
            return None
        return {**{key: job.get(key) for key in ('id', 'kind', 'status', 'started_at', 'finished_at', 'error', 'rule_run', 'verification_mode')},
                'items': [{key: item.get(key) for key in ('hash', 'name', 'phase', 'error', 'source_kept')} for item in job['items']]}

    def update_policy(self, values):
        value = policy(values)
        with self.lock:
            with self.guard():
                pull.save_json(self.policy_path, value)
                self.config = value
                self.cache_at = None
            self.log('cleanup_settings_saved')
        return {'ok': True, 'policy': dict(value)}

    def execution_settings(self, options=None):
        settings = copy.deepcopy(self.settings())
        if options is not None:
            settings['transfer_path_mappings'] = copy.deepcopy(options['path_mappings'])
        return settings

    def snapshot(self, force=False, *, transfer_options=None):
        with self.lock:
            now = self.clock()
            job = self.state.get('job') or {}
            if transfer_options is None and job.get('status') in ACTIVE:
                transfer_options = job.get('transfer_options')
            settings = self.execution_settings(transfer_options)
            if force or transfer_options is not None or self.cache is None or self.cache_at is None or now - self.cache_at >= settings.get('management_cache_seconds', 30):
                api = self.api()
                qb, tr, errors = api.inventory()
                if 'qb' in errors:
                    qb = self.raw[0]
                if 'tr' in errors:
                    tr = self.raw[1]
                self.raw = (qb, tr)
                mode = settings.get('seeders_limit_mode', 'site')
                try:
                    site = self.pts()
                    site_limit = count(site.get('seeders_max')) if site.get('available') and not site.get('stale') else None
                except Exception:
                    site_limit = None
                limit = settings.get('manual_seeders_max', 10) if mode == 'manual' else site_limit
                source = configuration.resolve_source(self.source(), settings)
                rows = inventory_rows(qb, tr, source, load(self.directory / 'batch.json', {}), limit, now, settings)
                old_observations = self.state.get('observations', {})
                observations = {}
                for row in rows:
                    if row['pts'] and any(client in errors for client in row['locations']):
                        row.update(validity='unknown', validity_text='未知', reason='下载器读取失败，保留上次已知任务', can_transfer=False)
                        for client in row['locations']:
                            if client in errors:
                                row['client_states'][client].update(validity='unknown', validity_text='未知', reason=row['reason'])
                    if row['validity'] == 'invalid':
                        # qB's global count alone is insufficient for destructive actions.
                        try:
                            q = row['_qb']
                            if q:
                                tracks = api.qtrackers(q['hash'])
                                values = [track['num_seeds'] for track in tracks
                                          if pts_url(track.get('url'), source) and track.get('status') == 2 and count(track.get('num_seeds')) is not None]
                                if not values or min(values) <= limit:
                                    raise ValueError()
                                row['seeders'] = max(values + [row['seeders']])
                            if errors:
                                raise ValueError()
                        except Exception:
                            row.update(validity='unknown', validity_text='未知', reason='超限报告尚未通过当前 Tracker 校验')
                            for value in row['client_states'].values():
                                value.update(validity='unknown', validity_text='未知', reason=row['reason'])
                    if row['validity'] == 'invalid' and site_limit is not None:
                        previous = old_observations.get(row['hash'], {})
                        continuous = (previous.get('threshold') == limit and previous.get('mode', 'site') == mode
                                      and 0 <= now - previous.get('last_seen', 0) <= settings.get('monitor_interval_seconds', 60) * 3)
                        since = previous.get('since', now) if continuous else now
                        observations[row['hash']] = {'since': since, 'last_seen': now, 'threshold': limit, 'mode': mode, 'seeders': row['seeders']}
                        row['invalid_since'] = since
                        row['delete_after'] = since + self.config['cleanup_wait_hours'] * 3600
                        row['delete_ready'] = now >= row['delete_after']
                    if errors:
                        row['can_transfer'] = False
                self.state['observations'] = observations
                if observations != old_observations:
                    self.save()
                self.cache = {'rows': rows, 'errors': errors, 'seeders_max': limit,
                              'seeders_limit_mode': mode, 'site_seeders_max': site_limit}
                self.cache_at = now
            job = self.public_job()
            phases = {item['hash']: item['phase'] for item in (job or {}).get('items', [])}
            items = []
            for raw in self.cache['rows']:
                row = {key: value for key, value in raw.items() if not key.startswith('_')}
                if row['delete_after'] is not None:
                    row['delete_ready'] = now >= row['delete_after']
                row['transfer_phase'] = phases.get(row['hash']) if job and job['kind'] == 'transfer' else None
                if job and job['status'] in ACTIVE:
                    row['can_transfer'] = False
                    row['delete_ready'] = False
                items.append(row)
            totals = {'total': len(items), 'pts': sum(row['pts'] for row in items),
                      **{key: sum(row['validity'] == key for row in items) for key in ('valid', 'invalid', 'unknown', 'inactive', 'downloading')},
                      'ready': sum(row['delete_ready'] for row in items), 'transferable': sum(row['can_transfer'] for row in items)}
            clients = {}
            for name in ('qb', 'tr'):
                selected = [row for row in items if name in row['locations']]
                raw = self.raw[0 if name == 'qb' else 1]
                clients[name] = {'connected': name not in self.cache['errors'], 'error': self.cache['errors'].get(name),
                                 'total': len(raw), 'pts_total': sum(row['pts'] for row in selected),
                                 **{key: sum(row['client_states'][name]['validity'] == key for row in selected) for key in ('valid', 'invalid', 'unknown', 'inactive', 'downloading')},
                                 'upload_speed': sum(t.get('upspeed' if name == 'qb' else 'rateUpload', 0) for t in raw),
                                 'download_speed': sum(t.get('dlspeed' if name == 'qb' else 'rateDownload', 0) for t in raw)}
            return {'items': items, 'totals': totals, 'clients': clients, 'seeders_max': self.cache['seeders_max'],
                    'seeders_limit_mode': self.cache['seeders_limit_mode'], 'site_seeders_max': self.cache['site_seeders_max'],
                    'checked_at': self.cache_at, 'policy': dict(self.config), 'job': job}

    def limits(self, force=False):
        with self.lock:
            if force or self.limits_cache is None or self.limits_at is None or self.clock() - self.limits_at >= self.settings().get('management_cache_seconds', 30):
                self.limits_cache = self.api().limits()
                self.limits_at = self.clock()
            return {**copy.deepcopy(self.limits_cache), 'checked_at': self.limits_at}

    def set_limits(self, values):
        with self.lock:
            with self.guard():
                result = self.api().set_limits(values)
                self.limits_cache, self.limits_at = result, self.clock()
            self.log('limits_saved')
        return {'ok': True, 'limits': {**result, 'checked_at': self.limits_at}}

    def begin(self, kind, values, *, transfer_options=None):
        if kind not in ('transfer', 'delete'):
            raise ManagementError('管理操作类型无效')
        expected = 'MOVE_QB_TO_TR_KEEP_DATA' if kind == 'transfer' else 'REMOVE_TASKS_KEEP_DATA'
        if not isinstance(values, dict) or set(values) != {'hashes', 'confirm'} or values.get('confirm') != expected:
            raise ManagementError('请确认所选操作及保留文件')
        selected = hashes(values['hashes'], None if kind == 'transfer' else self.settings().get('delete_max_per_job', 50))
        if transfer_options is not None:
            if kind != 'transfer':
                raise ManagementError('规则选项仅支持转种')
            transfer_options = transfer_rules.validate(transfer_options)
            if not transfer_options['path_mappings']:
                raise ManagementError('执行转种前请填写规则路径映射')
        with self.lock:
            existing = self.state.get('job')
            if existing and existing['status'] in ACTIVE:
                raise ManagementError('已有管理操作正在进行，请等待完成或取消转种', 409)
            snapshot = self.snapshot(force=True, transfer_options=transfer_options)
            if not all(value['connected'] for value in snapshot['clients'].values()):
                raise ManagementError('请先恢复两个下载器连接', 502)
            rows = {row['hash']: row for row in snapshot['items']}
            for value in selected:
                row = rows.get(value)
                if not row or not row['can_transfer' if kind == 'transfer' else 'delete_ready']:
                    raise ManagementError('所选任务尚未满足转种或到期清理条件', 409)
                if transfer_options is not None:
                    raw = self.row(value)
                    if transfer_rules.filter_reason(raw['_qb'], transfer_options) or (raw['_tr'] and not transfer_options['delete_duplicate_source']):
                        raise ManagementError('最新来源或目的任务已不满足保存规则，请重新预览；未执行', 409)
            # Check the native runner lock before recording a new operation.
            with self.guard():
                job = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'waiting', 'started_at': self.clock(),
                       'finished_at': None, 'error': None, 'items': [{'hash': value, 'name': rows[value]['name'], 'phase': 'queued', 'error': None} for value in selected]}
                if kind == 'transfer':
                    job['verification_mode'] = self.transfer_mode
                if transfer_options is not None:
                    job.update(transfer_options=copy.deepcopy(transfer_options), rule_run=True,
                               source_instance_id=transfer_options['source_instance_id'],
                               target_instance_id=transfer_options['target_instance_id'])
                self.state['job'] = job
                self.save()
            self.log('transfer_started' if kind == 'transfer' else 'cleanup_started')
            return {'ok': True, 'job': self.public_job()}

    def cancel(self, values):
        with self.lock:
            job = self.state.get('job')
            if not isinstance(values, dict) or set(values) != {'job_id'} or not job or values['job_id'] != job['id'] or job['kind'] != 'transfer' or job['status'] not in ACTIVE:
                raise ManagementError('没有可取消的当前转种', 409)
            job['cancel_requested'] = True
            self.save()
            return {'ok': True, 'job': self.public_job()}

    def recovery_directory(self, job):
        path = self.directory / 'operations' / job['id']
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o700)
        return path

    def exclude(self, row):
        # Keep permanent dedup even for older PTS jobs that were not accepted by this batch.
        path = self.directory / 'batch.json'
        state = load(path, {})
        existing = set(state.get('seen_hashes', []))
        if row['_aliases'] - existing:
            state['seen_hashes'] = sorted(existing | row['_aliases'])
            pull.save_json(path, state)

    def row(self, value):
        return next((row for row in self.cache['rows'] if value == row['hash'] or value in row['_aliases']), None)

    def recover_source(self, api, item):
        source = item.get('source')
        if not source or item.get('source_removed'):
            return True
        recovered = True
        try:
            if not item.get('tr_hash'):
                targets = api.rpc('torrent-get', {'ids': source['hashes'], 'fields': TR_FIELDS})['torrents']
                if targets:
                    item['tr_hash'] = targets[0]['hashString'].lower()
                    self.save()
            if item.get('tr_hash'):
                should_start = not item.get('created_tr') and source.get('tr_was_active', False)
                target = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
                expected_dir = source.get('tr_original_dir') or self.target_directory(item)
                expected_size = source.get('tr_original_size', source['size'])
                if target and (len(target) != 1 or target[0]['downloadDir'].rstrip('/') != expected_dir
                               or target[0]['totalSize'] != expected_size or target[0]['hashString'].lower() not in source['hashes']):
                    recovered = False
                elif target:
                    api.rpc('torrent-start' if should_start else 'torrent-stop', {'ids': [item['tr_hash']]})
                    target = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
                    if target and (target[0]['status'] not in (3, 4, 5, 6) if should_start else target[0]['status'] != 0):
                        recovered = False
        except Exception:
            recovered = False
        try:
            current = api.qget('torrents/info', {'hashes': item['hash']})
            if len(current) != 1 or current[0]['save_path'].rstrip('/') != source['download_dir'] or current[0]['total_size'] != source['size']:
                return False
            (api.qstop if source['paused'] else api.qstart)(item['hash'])
            current = api.qget('torrents/info', {'hashes': item['hash']})
            if len(current) != 1 or current[0].get('state', '').startswith(('stopped', 'paused')) != source['paused']:
                recovered = False
        except Exception:
            recovered = False
        return recovered

    def delete_step(self, api, job, item):
        snapshot = self.snapshot(force=True)
        row = self.row(item['hash'])
        if not row:
            item['phase'] = 'completed'
            self.save()
            return
        observation = self.state['observations'].get(row['hash'])
        ready = observation and self.clock() >= observation['since'] + self.config['cleanup_wait_hours'] * 3600
        if not ready or row['validity'] != 'invalid' or not row['pts'] or not all(value['connected'] for value in snapshot['clients'].values()):
            raise ManagementError('最新核查未满足失效等待条件，已跳过删除')
        directory = self.recovery_directory(job)
        q, t = row['_qb'], row['_tr']
        recovery = {'hash': row['hash'], 'locations': row['locations'], 'size': row['size'], 'seeders': row['seeders'], 'at': self.clock()}
        if q:
            data = api.qexport(q['hash'])
            meta = torrent_metadata(data)
            if not set(meta['hashes']) & row['_aliases']:
                raise ManagementError('种子身份核验失败，未删除任务')
            path = directory / (item['hash'] + '.torrent')
            path.write_bytes(data)
            os.chmod(path, 0o600)
            recovery['qb_save_path'] = q['save_path']
        if t:
            detail = api.rpc('torrent-get', {'ids': [t['hashString']], 'fields': ['hashString', 'downloadDir', 'magnetLink']})['torrents']
            if len(detail) != 1 or detail[0]['hashString'].lower() not in row['_aliases']:
                raise ManagementError('Transmission 任务身份核验失败，未删除任务')
            recovery['tr'] = detail[0]
        pull.save_json(directory / (item['hash'] + '.json'), recovery)
        self.exclude(row)
        item['phase'] = 'removing'
        self.save()
        if q:
            api.qremove(q['hash'])
        if t:
            api.rpc('torrent-remove', {'ids': [t['hashString']], 'delete-local-data': False})
        qb, tr, errors = api.inventory()
        if errors or any(t.get('hash', t.get('hashString', '')).lower() in row['_aliases'] for t in qb + tr):
            raise ManagementError('移除结果尚未确认，请刷新下载器列表')
        item['phase'] = 'completed'
        self.cache_at = None
        self.save()
        self.log('cleanup_task_removed', processed=1)

    def request_verification(self, api, item):
        if self.clock() - item['verify_started_at'] > self.settings().get('verify_timeout_seconds', VERIFY_TIMEOUT):
            raise ManagementError('TR 校验请求超过设定时限，来源保留')
        targets = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
        if len(targets) != 1:
            raise ManagementError('TR 校验任务已消失，来源保留')
        target = targets[0]
        if (target['downloadDir'].rstrip('/') != self.target_directory(item) or target['totalSize'] != item['source']['size']
                or target['hashString'].lower() not in item['source']['hashes']):
            raise ManagementError('TR 校验目标身份或目录已变化，来源保留')
        if target['status'] not in (1, 2):
            api.rpc('torrent-stop', {'ids': [item['tr_hash']]})
            target = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
            if len(target) != 1 or target[0]['status'] != 0:
                return
            # A completed check with a lost response is checked again, rather than inferred.
            api.rpc('torrent-verify', {'ids': [item['tr_hash']]})
            targets = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
            target = targets[0] if len(targets) == 1 else {}
        if target.get('status') in (1, 2):
            item['verify_observed'] = True
        item.update(phase='verifying')
        self.save()

    @staticmethod
    def target_complete(target, size, *, native=False):
        progress = target.get('percentDone')
        valid, unchecked = count(target.get('haveValid')), count(target.get('haveUnchecked'))
        return (type(progress) in (int, float) and math.isfinite(progress) and progress == 1
                and count(target.get('leftUntilDone')) == 0 and count(target.get('error')) == 0
                and valid is not None and unchecked is not None
                and (valid + unchecked == size if native else valid >= size and unchecked == 0))

    @staticmethod
    def target_should_start(job, item):
        options = job.get('transfer_options')
        if options is not None:
            return options['start_after_verify']
        return (True if job.get('verification_mode') == 'native'
                else not item['source']['paused'] or item['source'].get('tr_was_active'))

    def confirm_native_target(self, api, job, item):
        """Read native completion and actual seeding; never request an extra full verify."""
        elapsed = self.clock() - item['verify_started_at']
        if elapsed > self.settings().get('verify_timeout_seconds', VERIFY_TIMEOUT) and not item.get('source_removed'):
            raise ManagementError('TR 接管超过设定时限，qB 来源保留')
        def read_target():
            targets = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
            if len(targets) != 1:
                raise ManagementError('TR 接管任务尚未确认，qB 来源保留', 502)
            target = targets[0]
            if (target['downloadDir'].rstrip('/') != self.target_directory(item)
                    or target['totalSize'] != item['source']['size']
                    or target['hashString'].lower() not in item['source']['hashes']
                    or type(target.get('status')) is not int):
                raise ManagementError('TR 接管身份、目录或体积已变化，qB 来源保留')
            return target
        target = read_target()
        if target['status'] in (1, 2):
            return
        if target.get('error'):
            raise ManagementError('TR 接管报告错误，qB 来源保留')
        if not self.target_complete(target, item['source']['size'], native=True):
            if target['status'] in (3, 4) or elapsed >= 10:
                raise ManagementError('TR 未识别完整下载数据，qB 来源保留')
            return
        should_start = self.target_should_start(job, item)
        if should_start:
            if target['status'] == 0:
                item['native_start_requested'] = True
                self.save()
                api.rpc('torrent-start', {'ids': [item['tr_hash']]})
                target = read_target()
            if target['status'] != 6:
                return
        elif target['status'] != 0:
            api.rpc('torrent-stop', {'ids': [item['tr_hash']]})
            target = read_target()
            if target['status'] != 0:
                return
        if not self.target_complete(target, item['source']['size'], native=True):
            raise ManagementError('TR 接管完整性回读已变化，qB 来源保留')
        item.update(phase='verified', native_ready=True, error=None)
        self.save()

    def merge_target_labels(self, api, item, target, labels):
        existing = target.get('labels', [])
        if not isinstance(existing, list) or any(not isinstance(value, str) for value in existing):
            raise ManagementError('TR 标签读取无效，来源保留', 502)
        merged = list(dict.fromkeys(existing + labels))
        if merged != existing:
            api.rpc('torrent-set', {'ids': [item['tr_hash']], 'labels': merged})
            checked = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
            if len(checked) != 1 or not set(merged).issubset(checked[0].get('labels', [])):
                raise ManagementError('TR 标签写入尚未确认，来源保留', 502)

    def transfer_step(self, api, job, item):
        options = job.get('transfer_options')
        if item.get('removing_source') and not item.get('source_removed'):
            current = api.qget('torrents/info', {'hashes': item['hash']})
            if current:
                raise ManagementError('等待确认 qB 来源移除结果，TR 接管与文件保留', 502)
            item.update(source_removed=True, phase='source_removed')
            self.save()
        if job.get('cancel_requested') and not item.get('source_removed'):
            recovered = self.recover_source(api, item)
            item.update(phase='cancelled' if recovered else 'recovering', recover_phase='cancelled',
                        error=None if recovered else '等待确认 qB 与原有 TR 副本恢复；新建 TR 副本暂停保留')
            self.save()
            return
        if item['phase'] == 'recovering':
            if not self.recover_source(api, item):
                raise ManagementError('等待下载器恢复后确认来源状态', 502)
            item['phase'] = item.pop('recover_phase', 'failed')
            return
        if item['phase'] == 'waiting_target':
            self.confirm_native_target(api, job, item)
            return
        if item['phase'] == 'verify_requested':
            self.request_verification(api, item)
            return
        if item['phase'] in ('queued', 'prepared', 'adding', 'added'):
            snapshot = self.snapshot(force=True, transfer_options=options)
            row = self.row(item['hash'])
            if not row or not row['_qb'] or not all(value['connected'] for value in snapshot['clients'].values()):
                raise ManagementError('来源任务或下载器连接无法确认，未交接', 502)
            q, t = row['_qb'], row['_tr']
            if options is not None and not item.get('source'):
                reason = transfer_rules.filter_reason(q, options)
                if reason or (t and not options['delete_duplicate_source']):
                    item.update(phase='skipped', error=transfer_rules.REASONS[reason or 'target_exists'])
                    self.save()
                    return
            if not row['can_transfer']:
                raise ManagementError('来源任务未完整下载或目录不支持转种')
            directory = self.recovery_directory(job)
            backup = directory / (item['hash'] + '.torrent')
            if not item.get('source'):
                data = api.qexport(q['hash'])
                meta = torrent_metadata(data)
                if not set(meta['hashes']) & row['_aliases'] or meta['size'] != q['total_size']:
                    raise ManagementError('来源种子哈希或总体积核验失败')
                backup.write_bytes(data)
                os.chmod(backup, 0o600)
                mapped = configuration.mapped_path(q['save_path'], self.execution_settings(options))
                if not mapped:
                    raise ManagementError('来源目录不在转种映射中')
                item.update(source={'paused': q['state'].startswith(('stopped', 'paused')), 'download_dir': mapped[0],
                                    'tr_download_dir': mapped[1], 'size': meta['size'], 'hashes': meta['hashes'],
                                    'tr_original_dir': t['downloadDir'].rstrip('/') if t else None,
                                    'tr_original_size': t['totalSize'] if t else meta['size'],
                                    'tr_was_active': bool(t and t['status'] in (3, 4, 5, 6)), 'existing_target': bool(t),
                                    'target_labels': tagging.merge_labels(q.get('tags', ''),
                                        (options['target_labels'] if options is not None else self.settings().get('tr_labels', []))
                                        + [self.settings().get('managed_tag', tagging.DEFAULT_TAG)])},
                            created_tr=not bool(t), phase='prepared', pause_started_at=self.clock())
                self.save()
            if q['save_path'].rstrip('/') != item['source']['download_dir'] or q['total_size'] != item['source']['size']:
                raise ManagementError('来源目录或体积已变化，未交接')
            api.qstop(q['hash'])
            current = api.qget('torrents/info', {'hashes': q['hash']})
            if len(current) != 1 or not current[0].get('state', '').startswith(('stopped', 'paused')):
                if self.clock() - item['pause_started_at'] > self.settings().get('pause_timeout_seconds', 120):
                    raise ManagementError('来源暂停未确认，未添加 TR 任务')
                return
            paused_source = current[0]
            if (not completed_qb(paused_source)
                    or str(paused_source.get('hash', '')).lower() not in item['source']['hashes']
                    or paused_source['save_path'].rstrip('/') != item['source']['download_dir']
                    or paused_source['total_size'] != item['source']['size']):
                raise ManagementError('来源暂停后不再完整下载或身份已变化，未添加 TR 任务')
            if t:
                if t['downloadDir'].rstrip('/') != self.target_directory(item) or t['totalSize'] != item['source']['size']:
                    raise ManagementError('TR 已有同哈希任务，但目录或体积不一致')
                item['tr_hash'] = t['hashString'].lower()
            else:
                item['phase'] = 'adding'
                self.save()
                arguments = {'metainfo': base64.b64encode(backup.read_bytes()).decode(),
                             'download-dir': self.target_directory(item),
                             'paused': not self.target_should_start(job, item) if job.get('verification_mode') == 'native' else True}
                arguments['labels'] = item['source']['target_labels']
                answer = api.rpc('torrent-add', arguments)
                added = answer.get('torrent-added') or answer.get('torrent-duplicate') or {}
                target_hash = str(added.get('hashString', '')).lower()
                if target_hash not in item['source']['hashes']:
                    raise ManagementError('TR 添加身份未确认，保留来源任务')
                item['tr_hash'] = target_hash
            item['phase'] = 'added'
            self.save()
            if job.get('verification_mode') == 'native':
                item.update(phase='waiting_target', verify_started_at=self.clock())
                self.save()
                self.confirm_native_target(api, job, item)
                return
            api.rpc('torrent-stop', {'ids': [item['tr_hash']]})
            target = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
            if len(target) != 1 or target[0]['status'] != 0:
                return
            item.update(phase='verify_requested', verify_started_at=self.clock())
            self.save()
            self.request_verification(api, item)
            return
        if item['phase'] in ('verifying', 'verified', 'restoring_source', 'source_kept', 'source_removed'):
            target = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
            if len(target) != 1:
                raise ManagementError('TR 校验任务已消失，来源保留')
            t = target[0]
            if t['downloadDir'].rstrip('/') != self.target_directory(item) or t['totalSize'] != item['source']['size'] or t['hashString'].lower() not in item['source']['hashes']:
                raise ManagementError('TR 目录、总体积或哈希已变化，来源保留')
            native = job.get('verification_mode') == 'native'
            if self.clock() - item['verify_started_at'] > self.settings().get('verify_timeout_seconds', VERIFY_TIMEOUT) and not item.get('source_removed'):
                raise ManagementError('TR 接管超过设定时限，未移除来源' if native else 'TR 校验超过设定时限，未移除来源')
            if native and t['status'] != (6 if self.target_should_start(job, item) else 0):
                item['phase'] = 'waiting_target'
                self.save()
                return
            if t['status'] in (1, 2):
                if not item.get('verify_observed'):
                    item['verify_observed'] = True
                    self.save()
                return
            if not native and not item.get('verify_observed') and self.clock() - item['verify_started_at'] < 10:
                return
            if not self.target_complete(t, item['source']['size'], native=native):
                raise ManagementError('TR 数据完整性未确认，qB 来源保留' if native else 'TR 校验未完整通过，qB 来源保留')
            labels = item['source'].get('target_labels', tagging.merge_labels(
                options['target_labels'] if options is not None else self.settings().get('tr_labels', []),
                [self.settings().get('managed_tag', tagging.DEFAULT_TAG)]))
            self.merge_target_labels(api, item, t, labels)
            item['phase'] = 'verified' if not item.get('source_removed') else 'source_removed'
            self.save()
            remove_source = options is None or options['delete_duplicate_source' if item['source'].get('existing_target') else 'delete_source']
            current = [] if remove_source and item.get('source_removed') else api.qget('torrents/info', {'hashes': item['hash']})
            if current:
                if len(current) != 1:
                    raise ManagementError('qB 来源身份无法确认，未移除')
                q = current[0]
                paused = q.get('state', '').startswith(('stopped', 'paused'))
                if (str(q.get('hash', '')).lower() not in item['source']['hashes']
                        or q['save_path'].rstrip('/') != item['source']['download_dir'] or q['total_size'] != item['source']['size']
                        or not completed_qb(q)
                        or (not paused and (remove_source or not item.get('restoring_source')))):
                    raise ManagementError('qB 来源状态已变化，未移除')
            elif not remove_source:
                raise ManagementError('qB 来源已消失，保留来源结果无法确认')
            if remove_source:
                if current:
                    item.update(removing_source=True, phase='removing_source')
                    self.save()
                    batch = load(self.directory / 'batch.json', {})
                    permanent = set(batch.get('seen_hashes', []))
                    for bucket in ('accepted', 'pending', 'unconfirmed'):
                        for record in batch.get(bucket, {}).values():
                            permanent.update(pull.record_hashes(record))
                    unrecorded = set(item['source']['hashes']) - permanent
                    if unrecorded:
                        self.exclude({'_aliases': unrecorded})
                    api.qremove(item['hash'])
                    if api.qget('torrents/info', {'hashes': item['hash']}):
                        raise ManagementError('等待确认 qB 来源移除结果，TR 接管与文件保留', 502)
                item.update(source_removed=True, phase='source_removed')
                self.save()
            else:
                item.update(restoring_source=True, phase='restoring_source')
                self.save()
                if paused != item['source']['paused']:
                    (api.qstop if item['source']['paused'] else api.qstart)(item['hash'])
                checked = api.qget('torrents/info', {'hashes': item['hash']})
                if (len(checked) != 1 or checked[0].get('state', '').startswith(('stopped', 'paused')) != item['source']['paused']
                        or checked[0]['save_path'].rstrip('/') != item['source']['download_dir'] or checked[0]['total_size'] != item['source']['size']):
                    raise ManagementError('qB 来源恢复尚未确认，任务和文件保留', 502)
                item.update(source_kept=True, phase='source_kept')
                self.save()
            should_start = self.target_should_start(job, item)
            if should_start or options is not None:
                if not native:
                    api.rpc('torrent-start' if should_start else 'torrent-stop', {'ids': [item['tr_hash']]})
                target = api.rpc('torrent-get', {'ids': [item['tr_hash']], 'fields': TR_FIELDS})['torrents']
                if (len(target) != 1 or target[0].get('error')
                        or not self.target_complete(target[0], item['source']['size'], native=native)
                        or target[0]['downloadDir'].rstrip('/') != self.target_directory(item)
                        or target[0]['hashString'].lower() not in item['source']['hashes']
                        or (target[0]['status'] != 6 if native and should_start else
                            target[0]['status'] not in (3, 4, 5, 6) if should_start else target[0]['status'] != 0)):
                    raise ManagementError('等待确认 TR 完整数据与保种状态', 502)
            item.update(phase='completed', error=None)
            self.cache_at = None
            self.save()
            self.log('transfer_task_completed', processed=1)

    def finish_job(self, job):
        result = ('failed' if any(item['phase'] == 'failed' for item in job['items'])
                  else 'cancelled' if job.get('cancel_requested') else 'completed')
        job.update(status=result, finished_at=self.clock(), error='部分任务未完成，请查看明细' if result == 'failed' else None)
        self.save()
        self.cache_at = None
        self.log('management_job_finished', processed=sum(item['phase'] == 'completed' for item in job['items']),
                 errors=sum(item['phase'] == 'failed' for item in job['items']))

    def tick(self, allow_cleanup=True):
        with self.lock:
            job = self.state.get('job')
            if job and job['status'] in ACTIVE:
                deadline = time.monotonic() + 1
                while True:
                    item = next((item for item in job['items'] if item['phase'] not in FINISHED_PHASES), None)
                    if item is None:
                        self.finish_job(job)
                        return False
                    api = self.api()
                    try:
                        with self.guard():
                            if 'started_at' not in item:
                                # Queued legacy items start now; already touched items keep their recovery timer.
                                item['started_at'] = (self.clock() if item['phase'] == 'queued' else
                                                      item.get('pause_started_at', item.get('verify_started_at', job['started_at'])))
                            job['status'] = 'running'
                            self.save()
                            if job['kind'] == 'transfer':
                                self.transfer_step(api, job, item)
                            else:
                                self.delete_step(api, job, item)
                    except Exception as error:
                        if getattr(error, 'status', None) == 409 and not isinstance(error, ManagementError):
                            job['status'] = 'waiting'
                            self.log_transfer_progress(job)
                            self.save()
                            return False
                        elapsed = self.clock() - item.get('verify_started_at', item.get('started_at', self.clock()))
                        if job['kind'] == 'transfer' and (item.get('source_removed') or item.get('removing_source') or item['phase'] == 'recovering' or (getattr(error, 'status', None) == 502 and elapsed < self.settings().get('verify_timeout_seconds', VERIFY_TIMEOUT))):
                            item['error'] = '下载器操作暂未确认，保留当前任务；稍后重新查询实际状态'
                            job['status'] = 'waiting'
                            self.save()
                            self.log_transfer_progress(job)
                            return False
                        message = str(error) if isinstance(error, ManagementError) else '管理操作失败，请刷新检查；凭证详情已隐藏'
                        try:
                            with self.guard():
                                recovered = self.recover_source(api, item) if job['kind'] == 'transfer' else True
                        except Exception:
                            job['status'] = 'waiting'
                            item['error'] = '等待释放运行锁后恢复来源状态'
                            self.save()
                            self.log_transfer_progress(job)
                            return False
                        item.update(phase='failed' if recovered else 'recovering', recover_phase='failed',
                                    error=message + ('' if recovered else '；等待下载器恢复后确认来源状态'))
                        self.log('management_task_failed', errors=1)
                    self.cache_at = None
                    if all(current['phase'] in FINISHED_PHASES for current in job['items']):
                        self.finish_job(job)
                        return False
                    job['status'] = 'waiting'
                    self.save()
                    self.log_transfer_progress(job)
                    if job['kind'] != 'transfer' or item['phase'] not in FINISHED_PHASES:
                        return False
                    # Yield locks for cancellation on long skip/cancel queues, then resume without a polling delay.
                    if time.monotonic() >= deadline:
                        return True
            if self.last_monitor is None or self.clock() - self.last_monitor >= self.settings().get('monitor_interval_seconds', 60):
                value = self.snapshot(force=True)
                self.last_monitor = self.clock()
                if allow_cleanup and self.config['cleanup_enabled']:
                    selected = [row['hash'] for row in value['items'] if row['delete_ready']][:min(self.config['cleanup_max_per_run'], self.settings().get('delete_max_per_job', 50))]
                    if selected:
                        self.begin('delete', {'hashes': selected, 'confirm': 'REMOVE_TASKS_KEEP_DATA'})

    def monitor(self, stop):
        delay = 5
        while not stop.wait(delay):
            delay = 5
            try:
                if self.tick():
                    delay = 0
            except Exception:
                # Do not turn failed observations into elapsed invalidity.
                with self.lock:
                    if self.state.get('observations'):
                        self.state['observations'] = {}
                        self.cache_at = None
                        self.save()
                self.log('management_monitor_failed', errors=1)
