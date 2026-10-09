#!/usr/bin/env python3
"""PTSkit container web UI, persistent scheduler and existing pull runner."""
import base64
import contextlib
import copy
import datetime as dt
import hashlib
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs
import seedkeep_pull as pull
import seedkeep_pts as pts
import seedkeep_management as management
from seedkeep_downloaders import API, ManagementError
import seedkeep_configuration as configuration
from seedkeep_instances import Registry
from seedkeep_fleet import Fleet
import seedkeep_logstore as logstore
from seedkeep_strategy import Strategy, StrategyError
import seedkeep_tagging as tagging

VERSION = 'docker-1'
PUBLIC_SETTINGS = ('target', 'max_per_run', 'max_bytes', 'min_seeders', 'max_seeders', 'interval_hours', 'cron_minute') + tuple(configuration.DEFAULTS)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def next_schedule(settings, now=None):
    now = time.time() if now is None else now
    moment = dt.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    for _ in range(24 * 60 + 1):
        if moment.timestamp() > now and moment.minute == settings['cron_minute'] and moment.hour % settings['interval_hours'] == 0:
            return moment.timestamp()
        moment += dt.timedelta(minutes=1)
    raise ApiError('无法计算下一次运行时间')


def next_refill_schedule(settings, now, *, immediate=False):
    if settings['refill_count_basis'] == 'site_effective':
        return now if immediate else now + settings['refill_check_minutes'] * 60
    return next_schedule(settings, now)


def load_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


class Controller:
    def __init__(self, config, spawn=subprocess.Popen, clock=time.time):
        self.config = Path(config).resolve()
        self.directory = self.config.parent
        self.spawn, self.clock = spawn, clock
        self.lock = threading.RLock()
        self._file_lock_local = threading.local()
        self.process = None
        self.cache = None
        self.cache_at = 0
        self.cache_lock = threading.Lock()
        self.registry = Registry(self.source, self.settings)
        self.pts = pts.Client(self.source, self.settings,
                              rss_fetcher=lambda source, values: pts.rss.fetch(source, values, self.registry), clock=clock,
                              cache_path=self.directory / 'pts_statistics_cache.json')
        self.runtime_path = self.directory / 'web_runtime.json'
        self.runtime = load_json(self.runtime_path, {'automatic_enabled': False, 'next_run_at': None})
        self.secret_path = self.directory / 'web_session_key'
        if not self.secret_path.exists():
            self.secret_path.write_bytes(secrets.token_bytes(32))
            os.chmod(self.secret_path, 0o600)
        self.secret = self.secret_path.read_bytes()
        self.stop = threading.Event()
        self.login_attempts = {}
        self.settings()
        if self.runtime['automatic_enabled'] and not self.runtime.get('next_run_at'):
            self.runtime['next_run_at'] = next_refill_schedule(self.settings(), self.clock(), immediate=True)
            self.save_runtime()
        threshold = lambda: self.pts.snapshot(include_rss=False)
        self.management = management.Manager(self.directory, self.source, self.settings, threshold, self.operation_guard, clock=clock)
        self.fleet = Fleet(self.directory, self.source, self.settings, threshold,
                           self.operation_guard, self.management, clock=clock, runner_busy=self.running,
                           refill_due=lambda: self.runtime['automatic_enabled'] and self.clock() >= (self.runtime.get('next_run_at') or 0))
        self.logstore = logstore.Store(self.directory, clock=clock)
        self.refill_strategy = Strategy(self.directory, clock=clock)
        self.pending_path = self.directory / 'settings_pending.json'
        self.pending = load_json(self.pending_path)
        if self.pending is not None and (not isinstance(self.pending, dict)
                or self.pending.get('status') not in ('pending', 'failed', 'applied')
                or not isinstance(self.pending.get('entries'), list)
                or not isinstance(self.pending.get('baseline'), dict)):
            raise ApiError('暂存设置恢复资料无效；请检查备份，未清空暂存', 409)

    def settings(self):
        return pull.validate_settings(load_json(self.config))

    def source(self):
        settings = self.settings()
        return configuration.resolve_source(load_json(Path(settings['source_config'])), settings)

    def credentials(self, settings=None, source=None):
        return configuration.tr_credentials(settings or self.settings(),
            load_json(self.directory / 'web_auth.json') or source or self.source())

    def session_key(self):
        auth = self.credentials()
        identity = json.dumps([auth.get('username', ''), auth.get('password', '')], ensure_ascii=False).encode()
        return hmac.new(self.secret, identity, hashlib.sha256).digest()

    def configuration(self):
        with self.lock:
            settings, source = self.settings(), self.source()
            auth = self.credentials(settings, source)
            document = {'values': configuration.connection_values(source, settings, auth),
                        'secrets': {'token': bool(source.get('token')), 'qb_password': bool(source.get('password')),
                                    'tr_password': bool(auth.get('password'))},
                        'deployment': {'web_port': int(os.environ.get('WEB_PORT', '8786')),
                                       'timezone': os.environ.get('TZ') or time.tzname[0]}}
            content = json.dumps([settings, source, auth], sort_keys=True, ensure_ascii=False).encode()
            document['revision'] = hashlib.sha256(content).hexdigest()
            return document

    def prepared_configuration(self, body):
        if not isinstance(body, dict) or set(body) - {'values', 'revision'}:
            raise ApiError('连接配置格式无效')
        settings, source = self.settings(), self.source()
        auth = self.credentials(settings, source)
        try:
            candidate = configuration.prepare_connections(body.get('values'), source, settings, auth)
        except configuration.ConfigurationError as error:
            raise ApiError(str(error)) from None
        return settings, source, auth, candidate

    def update_configuration(self, body):
        with self.mutation_locks():
            self.ensure_settings_idle()
            with self.file_lock():
                settings, source, auth, candidate = self.prepared_configuration(body)
                revision = body.get('revision')
                if (not isinstance(revision, str) or len(revision) != 64 or any(c not in '0123456789abcdef' for c in revision)
                        or not hmac.compare_digest(revision, self.configuration()['revision'])):
                    raise ApiError('配置已被其他页面修改，请刷新配置后重新保存；未保存输入仍保留', 409)
                previous = configuration.prepare_connections({}, source, settings, auth)
                identity_fields = set(configuration.CONNECTION_FIELDS + configuration.SECRET_FIELDS) - {'download_path', 'category', 'tag', 'keep_torrent'}
                changed = any(candidate[key] != previous[key] for key in identity_fields)
                reauth = any(candidate[key] != previous[key] for key in ('tr_username', 'tr_password'))
                # Pause first: a failed configuration write leaves automation safely paused.
                if changed:
                    self.pause_automation()
                settings['connections'] = candidate
                pull.save_json(self.config, settings)
                self.invalidate_configuration(reset_observations=changed)
                return {'ok': True, 'configuration': self.configuration(),
                        'automation_paused': changed, 'reauth_required': reauth}

    def check_configuration(self, body):
        with self.lock:
            settings, source, _, candidate = self.prepared_configuration(body)
            settings = {**settings, 'connections': candidate}
            source = configuration.resolve_source(source, settings)
        result = {}
        try:
            pts.sanitize(pts.fetch_payload(source, timeout=settings['site_timeout_seconds'], limit=settings['candidate_limit']))
            result['site'] = {'connected': True, 'error': None}
        except Exception:
            result['site'] = {'connected': False, 'error': '站点查询失败，请检查地址、Token 和代理'}
        try:
            _, _, errors = API(source, settings).inventory()
        except Exception:
            errors = {'qb': '下载器连接失败', 'tr': '下载器连接失败'}
        for name in ('qb', 'tr'):
            result[name] = {'connected': name not in errors, 'error': errors.get(name)}
        return result

    def ensure_settings_idle(self):
        if self.running() or self.fleet.busy():
            raise ApiError('正在补量或管理任务，请等待完成后再保存设置', 409)

    def invalidate_configuration(self, reset_observations=False, invalidate_pts=True):
        with self.cache_lock:
            self.cache, self.cache_at = None, 0
        if invalidate_pts:
            self.pts.invalidate()
        self.fleet.invalidate(reset_observations=reset_observations)

    @contextlib.contextmanager
    def mutation_locks(self):
        # Fleet -> manager -> controller is also the monitor's acquisition order.
        with self.fleet.lock, self.management.lock, self.lock:
            yield

    def pause_automation(self):
        self.runtime.update(automatic_enabled=False, next_run_at=None)
        self.save_runtime()
        self.management.config['cleanup_enabled'] = False
        pull.save_json(self.management.policy_path, self.management.config)
        self.fleet.config.update(cleanup_enabled=False, unregistered_enabled=False)
        pull.save_json(self.fleet.policy_path, self.fleet.config)
        self.fleet.transfer_rules.pause()
        self.fleet.cleanup.pause()
        self.refill_strategy.pause()

    def revealed_token(self, body):
        if body != {}:
            raise ApiError('Token 显示请求格式无效')
        with self.lock:
            return {'token': self.source().get('token', '')}

    def validate_revision(self, revision, current):
        if (not isinstance(revision, str) or len(revision) != 64
                or any(c not in '0123456789abcdef' for c in revision)
                or not hmac.compare_digest(revision, current)):
            raise ApiError('配置已被其他页面修改，请刷新后重新保存；未保存输入仍保留', 409)

    def instances_identity(self, rows):
        fields = ('id', 'type', 'url', 'username', 'enabled', 'default', 'use_proxy', 'proxy_url')
        return sorted([tuple(row[key] for key in fields) + (self.registry._password(row),)
                       for row in rows])

    def save_instance(self, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'instance'}:
            raise ApiError('下载器保存请求格式无效')
        with self.mutation_locks():
            self.ensure_settings_idle()
            with self.file_lock():
                self.validate_revision(body['revision'], self.registry.public()['revision'])
                previous = self.registry.items()
                rows = self.registry.updated(body['instance'])
                changed = self.instances_identity(previous) != self.instances_identity(rows)
                if changed:
                    self.pause_automation()
                settings = self.settings()
                settings['downloaders'] = rows
                pull.save_json(self.config, settings)
                self.invalidate_configuration(reset_observations=changed, invalidate_pts=False)
                return {'ok': True, **self.registry.public(), 'automation_paused': changed}

    def delete_instance(self, body):
        if (not isinstance(body, dict) or set(body) != {'revision', 'instance_id', 'confirm'}
                or body['confirm'] != 'REMOVE_INSTANCE_CONFIGURATION'):
            raise ApiError('请确认只移除下载器配置并保留任务和文件')
        with self.mutation_locks():
            self.ensure_settings_idle()
            with self.file_lock():
                self.validate_revision(body['revision'], self.registry.public()['revision'])
                rows = self.registry.removed(body['instance_id'])
                self.pause_automation()
                settings = self.settings()
                settings['downloaders'] = rows
                pull.save_json(self.config, settings)
                selected = self.fleet.config['unregistered_instances']
                self.fleet.config['unregistered_instances'] = [item for item in selected if item != body['instance_id']]
                pull.save_json(self.fleet.policy_path, self.fleet.config)
                self.invalidate_configuration(reset_observations=True, invalidate_pts=False)
                return {'ok': True, **self.registry.public(), 'automation_paused': True}

    def check_instance(self, body):
        if not isinstance(body, dict) or set(body) != {'instance'}:
            raise ApiError('下载器检查请求格式无效')
        with self.lock:
            source, settings = self.source(), self.settings()
        return Registry(lambda: source, lambda: settings).check(body['instance'])

    def update_log_settings(self, body):
        with self.mutation_locks():
            self.ensure_settings_idle()
            with self.file_lock():
                return {'ok': True, 'policy': self.logstore.update_policy(body)}

    def cleanup_logs(self, body):
        if (not isinstance(body, dict) or set(body) != {'mode', 'confirm'}
                or body['confirm'] != 'CLEAR_MANAGED_LOGS' or body['mode'] not in ('errors', 'expired', 'all')):
            raise ApiError('请确认清理受管日志')
        with self.mutation_locks():
            self.ensure_settings_idle()
            with self.file_lock():
                return {'ok': True, **self.logstore.cleanup(body['mode'])}

    def update_transfer_rules(self, body):
        with self.mutation_locks():
            self.ensure_settings_idle()
            return self.fleet.transfer_rules.update(body)

    def run_transfer_rules(self, body):
        with self.mutation_locks():
            self.ensure_settings_idle()
            return self.fleet.transfer_rules.run(body)

    def legacy_operation(self, method, body):
        with self.fleet.lock:
            if self.fleet.cleanup.busy() or any((manager.state.get('job') or {}).get('status') in management.ACTIVE
                                               for manager in self.fleet.managers.values()):
                raise ApiError('已有管理作业，请完成后再操作', 409)
            self.fleet.transfer_rules.observe()
            return method(body)

    def pending_busy(self):
        job = self.fleet.cleanup.state.get('job') or {}
        return self.running() or self.fleet.busy() or job.get('status') == 'needs_review'

    def settings_baseline(self):
        # Only saved configuration, never observations, job progress or schedules.
        settings = self.settings()
        documents = {'config': settings, 'source': load_json(Path(settings['source_config'])),
                     'auth': load_json(self.directory / 'web_auth.json'),
                     'instances': self.registry.public()['revision'],
                     'storage': self.fleet.cleanup.storage.public()['values'],
                     'policy': load_json(self.fleet.policy_path, self.fleet.config),
                     'legacy_policy': load_json(self.management.policy_path, self.management.config),
                     'transfer': load_json(self.fleet.transfer_rules.path, self.fleet.transfer_rules.values),
                     'category': load_json(self.fleet.cleanup.path, self.fleet.cleanup.values),
                     'logs': self.logstore._policy()}
        return {key: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                              allow_nan=False).encode()).hexdigest()
                for key, value in documents.items()}

    def pending_public(self):
        def public_value(value):
            if isinstance(value, dict):
                return {k: public_value(v) for k, v in value.items()
                        if not any(part in k.lower() for part in ('password', 'token', 'secret', 'authorization', 'cookie'))}
            if isinstance(value, list):
                return [public_value(v) for v in value]
            return value
        with self.lock:
            doc = self.pending or {}
            entries = public_value(doc.get('entries', []))
            return {'status': doc.get('status', 'none'), 'id': doc.get('id'),
                    'queued_at': doc.get('queued_at'), 'applied_at': doc.get('applied_at'),
                    'error': doc.get('error'), 'entries': entries, 'count': len(entries),
                    'results': copy.deepcopy(doc.get('results', {'applied': [], 'failed': []})),
                    'automation_paused': doc.get('automation_paused', False),
                    'reauth_required': doc.get('reauth_required', False)}

    def persist_pending(self, document):
        # Create privately before writing any secret; replacement is atomic.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                             prefix='.settings_pending.', delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(document, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.pending_path)
            self.pending = document
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def pending_revision(self, key):
        if key == 'configuration':
            return self.configuration()['revision']
        if key.startswith('labels:'):
            return self.registry.public()['revision']
        if key == 'storage':
            return self.fleet.cleanup.storage.public()['revision']
        if key == 'transfer':
            return self.fleet.transfer_rules.revision()
        if key == 'category':
            return self.fleet.cleanup._revision()

    def validate_pending_entry(self, entry):
        from seedkeep_fleet import policy
        from seedkeep_storage import validate as validate_storage
        from seedkeep_transfer import validate as validate_transfer
        from seedkeep_cleanup import validate as validate_category
        if (not isinstance(entry, dict) or set(entry) - {'key', 'values', 'document', 'confirm'}
                or not isinstance(entry.get('key'), str) or not isinstance(entry.get('values'), dict)):
            raise ApiError('暂存设置分组格式无效')
        key, values = entry['key'], copy.deepcopy(entry['values'])
        if key == 'configuration' and self.pending and self.pending.get('entries'):
            previous = next((row['values']['values'] for row in self.pending['entries']
                             if row['key'] == key), {})
            incoming = values.get('values')
            if isinstance(incoming, dict):
                # Empty secret fields retain the private saved draft, also after failure.
                for name in configuration.SECRET_FIELDS:
                    if (incoming.get(name, '') == '' and name in previous
                            and not (name == 'qb_password' and incoming.get('clear_qb_password'))):
                        incoming[name] = previous[name]
                if ('clear_qb_password' not in incoming and not incoming.get('qb_password')
                        and previous.get('clear_qb_password')):
                    incoming['clear_qb_password'] = True
        if key not in ('runtime', 'configuration', 'storage', 'policy', 'transfer', 'category', 'logs') and not key.startswith('labels:'):
            raise ApiError('暂存设置包含不支持的分组')
        document = entry.get('document', {})
        if not isinstance(document, dict):
            raise ApiError('暂存设置版本格式无效')
        revision = self.pending_revision(key)
        supplied = values.get('revision') if key == 'configuration' else document.get('revision')
        if revision is not None:
            self.validate_revision(supplied, revision)
        if 'instances_revision' in document:
            self.validate_revision(document['instances_revision'], self.registry.public()['revision'])
        if key == 'runtime':
            self.prepared_settings(values)
        elif key == 'configuration':
            self.prepared_configuration(values)
        elif key.startswith('labels:'):
            if set(values) != {'id', 'category', 'tag'} or values['id'] != key[7:]:
                raise ApiError('下载器分类或标签格式无效')
            self.registry.get(values['id'], enabled=False)
            self.registry.updated(values)
        elif key == 'storage':
            validate_storage(values, allowed_roots=self.fleet.cleanup.storage.allowed_roots)
            registered = {row['id'] for row in self.registry.items()}
            if any(row['instance_id'] not in registered for row in values['mappings']):
                raise ApiError('存储映射包含不存在的下载器')
        elif key == 'policy':
            policy(values)
            if not set(values['unregistered_instances']).issubset({row['id'] for row in self.registry.items()}):
                raise ApiError('未注册清理范围包含不存在的实例')
        elif key == 'transfer':
            values = validate_transfer(values)
            if values['source_instance_id'] or values['target_instance_id'] or values['enabled']:
                self.fleet.transfer_rules.pair(values)
            if values['enabled']:
                if entry.get('confirm') != 'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA':
                    raise ApiError('请确认启用定时 qB→TR 转种；文件始终保留')
                if not values['path_mappings']:
                    raise ApiError('启用定时转种前请填写路径映射')
        elif key == 'category':
            values = validate_category(values)
            self.fleet.cleanup._bind(values)
            old = {row['id']: row for row in self.fleet.cleanup.values['rules']}
            if any(row['enabled'] and (row['id'] not in old or not old[row['id']]['enabled']) for row in values['rules']):
                raise ApiError('请先保存规则，再对已保存版本明确启用自动删除')
        elif key == 'logs':
            try:
                logstore._validate_policy(values)
            except ValueError as error:
                raise ApiError(str(error)) from None
        result = {'key': key, 'values': values}
        if revision is not None and key != 'configuration':
            result['document'] = {'revision': revision}
            if 'instances_revision' in document:
                result['document']['instances_revision'] = document['instances_revision']
        if key == 'transfer' and 'confirm' in entry:
            result['confirm'] = entry['confirm']
        return result

    def defer_settings(self, body):
        if (not isinstance(body, dict) or set(body) != {'entries'} or not isinstance(body['entries'], list)
                or not 1 <= len(body['entries']) <= 107):
            raise ApiError('暂存设置请求格式无效')
        with self.mutation_locks():
            entries = [self.validate_pending_entry(entry) for entry in body['entries']]
            keys = [entry['key'] for entry in entries]
            if len(keys) != len(set(keys)):
                raise ApiError('暂存设置包含重复分组')
            baseline = self.settings_baseline()
            # Detect external edits that have not reached the in-memory rule objects.
            for path, current in ((self.fleet.policy_path, self.fleet.config),
                                  (self.fleet.transfer_rules.path, self.fleet.transfer_rules.values),
                                  (self.fleet.cleanup.path, self.fleet.cleanup.values)):
                if load_json(path, current) != current:
                    raise ApiError('已保存规则基线发生变化，请重新加载服务并核对设置', 409)
            old = self.pending if self.pending and self.pending['entries'] else None
            if old and old['baseline'] != baseline:
                # Explicitly replacing every failed entry is the only rebase operation.
                if old['status'] != 'failed' or not {e['key'] for e in old['entries']}.issubset(keys):
                    raise ApiError('暂存设置的配置基线已变化，请刷新后核对；旧暂存仍保留', 409)
            busy = self.pending_busy()
            if not busy and old is None:
                try:
                    with self.file_lock():
                        pass
                except ApiError as error:
                    if error.status != 409:
                        raise
                    busy = True
                if not busy:
                    return {'ok': True, 'deferred': False}
            merged = {entry['key']: copy.deepcopy(entry) for entry in (old or {}).get('entries', [])}
            merged.update({entry['key']: entry for entry in entries})
            ordered = list(merged.values())
            if old and old['status'] == 'pending' and ordered == old['entries']:
                return {'ok': True, 'deferred': True, 'pending': self.pending_public()}
            document = {**(copy.deepcopy(old) if old else {}), 'status': 'pending',
                        'id': (old or {}).get('id') or secrets.token_hex(16),
                        'queued_at': (old or {}).get('queued_at', self.clock()), 'applied_at': None,
                        'error': None, 'entries': ordered, 'baseline': baseline,
                        'results': {'applied': (old or {}).get('results', {}).get('applied', []), 'failed': []}}
            self.persist_pending(document)
            return {'ok': True, 'deferred': True, 'pending': self.pending_public()}

    def apply_pending_settings(self):
        if not self.pending or self.pending['status'] != 'pending' or self.pending_busy():
            return
        try:
            with self.file_lock():
                self._apply_pending_locked()
        except ApiError as error:
            if error.status != 409:
                raise
            # A detached refill process may still hold run.lock after a restart.

    def _apply_pending_locked(self):
        document = copy.deepcopy(self.pending)
        key = None
        try:
            if document['baseline'] != self.settings_baseline():
                document.update(status='failed', error='配置基线发生变化，暂存未应用；请刷新后核对并明确重新保存')
                document['results']['failed'] = [entry['key'] for entry in document['entries']]
                self.persist_pending(document)
                return
            # Revalidate the whole batch before any execution configuration is written.
            document['entries'] = [self.validate_pending_entry(entry) for entry in document['entries']]
            order = {name: i for i, name in enumerate(('configuration', 'runtime', 'labels', 'storage', 'policy', 'transfer', 'category', 'logs'))}
            for entry in sorted(list(document['entries']), key=lambda item: order[item['key'].split(':')[0]]):
                key, values = entry['key'], copy.deepcopy(entry['values'])
                paused = document.get('automation_paused', False)
                if key == 'configuration':
                    values['revision'] = self.configuration()['revision']
                    result = self.update_configuration(values)
                    document['automation_paused'] = paused or result['automation_paused']
                    document['reauth_required'] = result['reauth_required']
                elif key == 'runtime':
                    self.update_settings(values)
                elif key.startswith('labels:'):
                    self.save_instance({'revision': self.registry.public()['revision'], 'instance': values})
                elif key == 'storage':
                    self.fleet.cleanup.update_storage({'revision': self.pending_revision(key), 'values': values})
                elif key == 'policy':
                    if paused:
                        values.update(cleanup_enabled=False, unregistered_enabled=False)
                    self.fleet.update_policy(values)
                elif key == 'transfer':
                    if paused:
                        values['enabled'] = False
                    self.update_transfer_rules({'revision': self.pending_revision(key), 'values': values, 'confirm': entry.get('confirm')})
                elif key == 'category':
                    # Earlier sensitive/storage saves may have paused saved modes.
                    current = {r['id']: r for r in self.fleet.cleanup.values['rules']}
                    for rule in values['rules']:
                        if rule['enabled'] and not current.get(rule['id'], {}).get('enabled'):
                            rule.update(enabled=False, observe_only=True)
                    self.fleet.cleanup.update({'revision': self.pending_revision(key), 'values': values})
                elif key == 'logs':
                    self.update_log_settings(values)
                document['entries'].remove(entry)
                document['results']['applied'].append(key)
                document['baseline'] = self.settings_baseline()
                # Checkpoint after each group: recovery never repeats a recorded save.
                for remaining in document['entries']:
                    revision = self.pending_revision(remaining['key'])
                    if remaining['key'] == 'configuration':
                        remaining['values']['revision'] = revision
                    elif revision:
                        remaining['document'] = {'revision': revision}
                    if document.get('automation_paused'):
                        if remaining['key'] == 'policy':
                            remaining['values'].update(cleanup_enabled=False, unregistered_enabled=False)
                        elif remaining['key'] == 'transfer':
                            remaining['values']['enabled'] = False
                    if remaining['key'] == 'category':
                        modes = {r['id']: r for r in self.fleet.cleanup.values['rules']}
                        for rule in remaining['values']['rules']:
                            if rule['enabled'] and not modes.get(rule['id'], {}).get('enabled'):
                                rule.update(enabled=False, observe_only=True)
                if not document['entries']:
                    document.update(status='applied', applied_at=self.clock(), error=None)
                self.persist_pending(copy.deepcopy(document))
        except Exception:
            document.update(status='failed', error=('暂存设置分组保存失败：' + key if key else '暂存设置验证失败')
                            + '；成功分组已生效，剩余暂存保留，请核对后重新保存')
            document['results']['failed'] = [key] if key else [entry['key'] for entry in document['entries']]
            self.persist_pending(document)

    def save_runtime(self):
        pull.save_json(self.runtime_path, self.runtime)

    def running(self):
        return self.process is not None and self.process.poll() is None

    @contextlib.contextmanager
    def file_lock(self):
        if getattr(self._file_lock_local, 'held', False):
            yield
            return
        import fcntl
        with (self.directory / 'run.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ApiError('正在拉取，请等待本轮结束后再保存设置', 409) from None
            self._file_lock_local.held = True
            try:
                yield
            finally:
                self._file_lock_local.held = False

    @contextlib.contextmanager
    def operation_guard(self):
        with self.lock:
            if self.running():
                raise ApiError('正在补量，请等待本轮完成后再管理任务或限速', 409)
            guard = self.file_lock()
            guard.__enter__()
        try:
            yield
        finally:
            guard.__exit__(None, None, None)

    def monitor_step(self):
        with self.mutation_locks():
            self.apply_pending_settings()
            if self.pending and self.pending['status'] == 'pending':
                # Continue existing jobs, but give queued settings priority over new ones.
                active = any((manager.state.get('job') or {}).get('status') in management.ACTIVE
                             for manager in self.fleet._all_managers()) or self.fleet.cleanup.busy()
                if not active:
                    return
            return self.fleet._tick()

    def monitor(self):
        delay = 5
        while not self.stop.wait(delay):
            delay = 5
            try:
                if self.monitor_step():
                    delay = 0
            except Exception:
                with self.fleet.lock:
                    self.fleet.state['observations'] = {}
                    self.fleet.cache_at = None
                    self.fleet._save()
                    self.fleet.cleanup.interrupt()
                self.fleet._log('fleet_monitor_failed', errors=1)

    def refresh_site_statistics(self):
        self.pts.refresh_api(self.settings(), False)

    def site_monitor(self):
        while not self.stop.wait(5):
            try:
                self.refresh_site_statistics()
            except Exception as error:
                pull.event('pts_statistics_monitor_failed', reason=type(error).__name__)

    def prepared_settings(self, values):
        daily = {'target', 'max_per_run', 'min_seeders', 'max_seeders', 'max_size_mib', 'interval_hours', 'cron_minute'}
        allowed = daily | set(configuration.DEFAULTS) | set(configuration.LEGACY_NUMBERS)
        if not isinstance(values, dict) or set(values) - allowed:
            raise ApiError('设置中包含不支持的字段')
        settings = self.settings()
        original = dict(settings)
        for key, value in values.items():
            if key == 'max_size_mib':
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1048576 or int(value * 1024 * 1024) < 1:
                    raise ApiError('最大体积应大于 0，单位为 MiB')
                settings['max_bytes'] = int(value * 1024 * 1024)
            else:
                if key in daily and type(value) is not int:
                    raise ApiError('人数、数量和运行周期必须是整数')
                settings[key] = value
        try:
            settings = pull.validate_settings(configuration.validate_runtime(settings))
        except configuration.ConfigurationError as error:
            raise ApiError(str(error)) from None
        except pull.PullError:
            raise ApiError('请检查人数范围、数量上限和运行周期') from None
        if settings['target'] > 100000 or settings['max_per_run'] > 1000 or settings['max_seeders'] > 100000:
            raise ApiError('目标最多 100000 个，每轮最多 1000 个')
        settings.update(mode='maintain', allow_filter_changes=True)
        return original, settings

    def update_settings(self, values):
        allowed = {'target', 'max_per_run', 'min_seeders', 'max_seeders', 'max_size_mib', 'interval_hours', 'cron_minute'} | set(configuration.DEFAULTS) | set(configuration.LEGACY_NUMBERS)
        if not isinstance(values, dict) or set(values) - allowed:
            raise ApiError('设置中包含不支持的字段')
        with self.mutation_locks():
            self.ensure_settings_idle()
            with self.file_lock():
                original, settings = self.prepared_settings(values)
                strategy_keys = ('target', 'refill_count_basis', 'refill_trigger', 'refill_floor', 'refill_check_minutes',
                                 'refill_max_inflight', 'refill_retry_seconds', 'refill_site_max_age_minutes', 'refill_reservation_hours')
                if any(original[key] != settings[key] for key in strategy_keys):
                    self.refill_strategy.reset(preserve_active=original['refill_count_basis'] == settings['refill_count_basis'])
                pull.save_json(self.config, settings)
                schedule_keys = strategy_keys + ('interval_hours', 'cron_minute')
                if self.runtime['automatic_enabled'] and any(original[key] != settings[key] for key in schedule_keys):
                    self.runtime['next_run_at'] = next_refill_schedule(settings, self.clock(), immediate=True)
                self.save_runtime()
                reset = any(key in values and original[key] != settings[key] for key in ('seeders_limit_mode', 'manual_seeders_max', 'tracker_fresh_seconds', 'monitor_interval_seconds'))
                self.invalidate_configuration(reset_observations=reset,
                    invalidate_pts=any(key in values and original[key] != settings[key] for key in ('pts_cache_seconds', 'site_timeout_seconds', 'candidate_limit')))
        return {'ok': True}

    def automation(self, enabled):
        if type(enabled) is not bool:
            raise ApiError('自动运行开关必须是布尔值')
        with self.lock:
            if enabled and 'downloaders' in self.settings() and self.registry.primary() is None:
                raise ApiError('请先配置启用的默认 qB 下载器')
            self.runtime.update(automatic_enabled=enabled,
                                next_run_at=next_refill_schedule(self.settings(), self.clock(), immediate=True) if enabled else None)
            self.save_runtime()
            if not enabled:
                self.refill_strategy.pause()
        return {'ok': True}

    def refill_inventory(self):
        if 'downloaders' not in self.settings():
            return self.snapshot(force=True)
        qb, tr = [], []
        try:
            for instance in self.registry.items():
                if instance['enabled']:
                    rows = self.registry.api(instance['id']).inventory_one()
                    (qb if instance['type'] == 'qb' else tr).extend(rows)
        except Exception:
            return [], [], {'error': '下载器读取失败'}
        return qb, tr, {'error': None}

    def check_effective_refill(self, *, force=False):
        with self.file_lock():
            site = self.pts.snapshot(force=True, include_rss=False)
            qb, tr, connection = self.refill_inventory()
            if connection['error'] is not None:
                self.refill_strategy.defer('unknown')
                raise ApiError('下载器读取失败，无法确认在途数量；已暂停本轮补量', 502)
            try:
                result = self.refill_strategy.evaluate(self.settings(), site, qb, tr,
                    load_json(self.directory / 'batch.json', {}), source=self.source(), force=force)
            except StrategyError:
                raise ApiError('保种策略恢复资料无效，请检查备份；未清空资料', 409) from None
            if result['status'] == 'unknown':
                raise ApiError('站端有效数量未知或同步记录已过期；已暂停本轮补量', 502)
            return result

    def log_refill_check(self, decision=None, reason=None):
        values = {}
        if decision is not None:
            if type(decision.get('site_current')) is int:
                values['site_current'] = decision['site_current']
            if type(decision.get('allowance')) is int:
                values['refill_allowance'] = decision['allowance']
            reason = decision.get('status')
        summary = (self.fleet.cache or {}).get('seedkeep')
        if summary and self.fleet.cache_at is not None and self.clock() - self.fleet.cache_at <= self.settings()['management_cache_seconds']:
            values.update(tagging.log_counters(summary))
        with pull.private_logging(self.directory):
            pull.event('refill_check_deferred' if decision is None else 'refill_check', reason=reason, **values)

    def start_run(self, *, refill_allowance=None):
        with self.mutation_locks():
            if self.running():
                raise ApiError('本轮正在运行，无需重复启动', 409)
            if self.fleet.busy():
                raise ApiError('正在管理任务，请等待完成后再补量', 409)
            settings = self.settings()
            if 'downloaders' in settings and self.registry.primary() is None:
                raise ApiError('请先配置启用的默认 qB 下载器')
            with self.file_lock():
                pass
            if settings['refill_count_basis'] == 'site_effective' and refill_allowance is None:
                decision = self.check_effective_refill(force=True)
                refill_allowance = decision['allowance']
                if not refill_allowance:
                    return {'ok': True, 'started': False, 'reason': decision['status']}
            args = [sys.executable, str(Path(__file__).parent / 'seedkeep_pull.py'), '--config', str(self.config), '--log']
            if refill_allowance is not None:
                if type(refill_allowance) is not int or not 1 <= refill_allowance <= settings['max_per_run']:
                    raise ApiError('本轮补量预算无效')
                args += ['--refill-allowance', str(refill_allowance)]
            self.process = self.spawn(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.cache_at = 0
            self.fleet.state['automation_turn'] = 'cleanup'
            self.fleet._save()
            if settings['refill_count_basis'] == 'site_effective':
                self.refill_strategy.defer('running')
        return {'ok': True, 'started': True}

    def tick(self):
        with self.mutation_locks():
            if self.process is not None and self.process.poll() is not None:
                self.process = None
                self.cache_at = 0
            self.apply_pending_settings()
            if self.runtime['automatic_enabled'] and self.clock() >= (self.runtime.get('next_run_at') or 0):
                settings, now = self.settings(), self.clock()
                effective = settings['refill_count_basis'] == 'site_effective'
                if self.running():
                    if effective:
                        self.refill_strategy.defer('running')
                    self.runtime['next_run_at'] = now + settings['refill_retry_seconds']
                    self.save_runtime()
                    return
                try:
                    if self.fleet.busy():
                        raise ApiError('正在管理任务，请等待完成后再补量', 409)
                    if (self.fleet.state.get('automation_turn', 'cleanup') == 'cleanup'
                            and self.fleet.cleanup.wants_turn()):
                        raise ApiError('本轮由分类清理先执行，稍后重试补量', 409)
                    if effective:
                        decision = None
                        decision = self.check_effective_refill()
                        self.log_refill_check(decision)
                        if decision['allowance']:
                            self.start_run(refill_allowance=decision['allowance'])
                    else:
                        self.start_run()
                    self.runtime['next_run_at'] = next_refill_schedule(settings, now)
                except (ApiError, OSError) as error:
                    if effective:
                        self.refill_strategy.defer('busy' if getattr(error, 'status', None) == 409 else 'unknown')
                    self.runtime['next_run_at'] = now + settings['refill_retry_seconds']
                    self.log_refill_check(reason='busy' if getattr(error, 'status', None) == 409 else 'unknown')
                self.save_runtime()
        if self.logstore.policy()['auto_cleanup_enabled']:
            try:
                with self.mutation_locks():
                    self.ensure_settings_idle()
                    with self.file_lock():
                        self.logstore.tick()
            except ApiError:
                pass

    def scheduler(self):
        while not self.stop.wait(1):
            try:
                self.tick()
            except Exception as error:
                pull.event('scheduler_error', reason=type(error).__name__)

    def snapshot(self, force=False):
        with self.cache_lock:
            settings = self.settings()
            if not force and self.cache is not None and self.clock() - self.cache_at < settings['page_refresh_seconds']:
                return self.cache
            try:
                if 'downloaders' in settings:
                    client = pull.FleetClients(self.source(), settings, require_default=False)
                    qb, tr = client.snapshot()
                    connection = {kind: any(row['type'] == kind for row in client.instances) for kind in ('qb', 'tr')}
                    connection['error'] = None
                else:
                    qb, tr = pull.Clients(self.source(), settings).snapshot()
                    connection = {'qb': True, 'tr': True, 'error': None}
            except Exception as error:
                message = '请检查下载器连接和权限'
                if self.cache:
                    qb, tr, _ = self.cache
                else:
                    qb, tr = [], {}
                connection = {'qb': False, 'tr': False, 'error': '下载器连接失败：' + message}
            self.cache = (qb, tr, connection)
            self.cache_at = self.clock()
            return self.cache

    def status(self, force=False):
        qb, tr, connection = self.snapshot(force=force)
        state = load_json(self.directory / 'batch.json', {})
        settings = self.settings()
        unknown_counts = dict.fromkeys(('managed_active', 'managed_qb', 'managed_tr', 'managed_both', 'pending_reserved'))
        fleet_snapshot = None
        try:
            counts = pull.managed_counts(state, qb, tr, settings['managed_tag']) if not connection.get('error') else unknown_counts
            if counts['managed_active'] is None:
                seedkeep = tagging.unknown_summary(settings['managed_tag'], self.cache_at)
            elif counts['managed_active'] == 0:
                seedkeep = tagging.summarize_rows([], [], settings['managed_tag'], self.cache_at)
            elif force:
                fleet_snapshot = self.fleet.snapshot(force=True,
                    site_snapshot=self.pts.snapshot(include_rss=False, refresh=False))
                seedkeep = fleet_snapshot['seedkeep']
            else:
                fleet_snapshot = self.fleet.snapshot(site_snapshot=self.pts.snapshot(include_rss=False, refresh=False))
                seedkeep = fleet_snapshot['seedkeep']
        except (pull.PullError, tagging.TaggingError, ManagementError, ValueError, KeyError, TypeError):
            counts, seedkeep = unknown_counts, tagging.unknown_summary(settings['managed_tag'], self.cache_at)
        if not seedkeep['connected']:
            counts = unknown_counts
        else:
            counts.update(managed_active=seedkeep['total'], managed_qb=seedkeep['qb'],
                          managed_tr=seedkeep['tr'], managed_both=seedkeep['both'])
        seedkeep_display = tagging.unknown_display(settings['managed_tag'], self.cache_at)
        try:
            if fleet_snapshot is None:
                fleet_snapshot = self.fleet.snapshot(force=force,
                    site_snapshot=self.pts.snapshot(include_rss=False, refresh=False))
            seedkeep_display = fleet_snapshot['seedkeep_display']
        except (tagging.TaggingError, ManagementError, ValueError, KeyError, TypeError):
            pass
        source = self.source()
        last = load_json(self.directory / 'status.json', {})
        allowed = ('run_outcome', 'stop_reason', 'last_error', 'added_this_run', 'errors', 'updated_at')
        with self.fleet.lock:
            cleanup = self.fleet.cleanup.summary()
        with self.lock:
            runtime, running = dict(self.runtime), self.running()
            strategy = self.refill_strategy.public(settings, runtime['automatic_enabled'], runtime.get('next_run_at'))
        return {'version': VERSION, 'timezone': os.environ.get('TZ') or time.tzname[0],
                'settings': {key: settings[key] for key in PUBLIC_SETTINGS},
                **runtime, 'running': running, **counts,
                'refill_strategy': strategy,
                'seedkeep': seedkeep,
                'seedkeep_display': seedkeep_display,
                'cleanup': cleanup,
                'downloaders': [{key: row[key] for key in ('id', 'name', 'type', 'enabled', 'default')} for row in self.registry.items()],
                'accepted_total': len(state.get('accepted', {})),
                'pending_total': len(state.get('pending', {})),
                'unconfirmed_total': len(state.get('unconfirmed', {})),
                'qb_total': None if connection.get('error') else len(qb),
                'tr_total': None if connection.get('error') else len(tr),
                'remaining': None if counts['managed_active'] is None else max(0, settings['target'] - counts['managed_active'] - counts['pending_reserved']),
                'connection': connection, 'last_run': {key: last[key] for key in allowed if key in last},
                'destination': {key: source.get(key, '') for key in ('category', 'tag')}}

    def tasks(self):
        qb, tr, _ = self.snapshot()
        qmap = {t['hash'].lower(): t for t in qb}
        state = load_json(self.directory / 'batch.json', {})
        items = []
        for bucket in ('accepted', 'pending', 'unconfirmed'):
            for tid, record in state.get(bucket, {}).items():
                aliases = pull.record_hashes(record)
                q = next((qmap[h] for h in aliases if h in qmap), None)
                t = next((tr[h] for h in aliases if h in tr), None)
                location = 'qB + TR' if q and t else 'qB' if q else 'TR' if t else '已移除' if bucket == 'accepted' else '待确认'
                items.append({'id': tid, 'name': (q or t or {}).get('name', '保种任务 #' + tid),
                              'size': record['size'], 'seeders_at_pull': record['seeders_at_pull'],
                              'location': location, 'progress': q.get('progress', 0) if q else t.get('percentDone', 0) if t else 0,
                              'added_at': record['added_at']})
        items.sort(key=lambda item: item['added_at'], reverse=True)
        return {'items': items, 'total': len(items)}

    def logs(self, kind='all'):
        if kind not in ('all', 'error'):
            raise ApiError('日志筛选无效')
        return self.logstore.read(limit=self.settings()['log_limit'], kind=kind)

    def login(self, values, address):
        if not isinstance(values, dict):
            raise ApiError('请输入用户名和密码')
        with self.lock:
            recent = [stamp for stamp in self.login_attempts.get(address, []) if self.clock() - stamp < 60]
            if len(recent) >= 10:
                raise ApiError('尝试过于频繁，请稍后重试', 429)
            self.login_attempts[address] = recent + [self.clock()]
            auth = self.credentials()
            user, password = auth.get('username', ''), auth.get('password', '')
            valid = (bool(user and password)
                     and hmac.compare_digest(str(values.get('username', '')).encode(), user.encode())
                     and hmac.compare_digest(str(values.get('password', '')).encode(), password.encode()))
            if not valid:
                raise ApiError('用户名或密码错误，请使用已有 Transmission 账号', 401)
            self.login_attempts.pop(address, None)
            payload = str(int(self.clock() + 12 * 3600)) + '.' + secrets.token_hex(12)
            signature = hmac.new(self.session_key(), payload.encode(), hashlib.sha256).hexdigest()
            return payload + '.' + signature

    def authenticated(self, token):
        try:
            expires, nonce, signature = token.split('.')
            expected = hmac.new(self.session_key(), (expires + '.' + nonce).encode(), hashlib.sha256).hexdigest()
            return self.clock() < int(expires) <= self.clock() + 12 * 3600 + 5 and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError, configuration.ConfigurationError):
            return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'PTSkit'
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def send(self, code, body, content_type='application/json; charset=utf-8', cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def auth(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
            token = jar['seedkeep_session'].value if 'seedkeep_session' in jar else ''
            if not self.server.controller.authenticated(token):
                raise ApiError('请先登录', 401)
        except cookies.CookieError:
            raise ApiError('请先登录', 401) from None

    def do_GET(self):
        try:
            address = urlsplit(self.path)
            path = address.path
            if path == '/healthz':
                return self.send(200, {'ok': True, 'version': VERSION})
            files = {'/': ('index.html', 'text/html; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
            if path in files:
                name, mime = files[path]
                return self.send(200, (Path(__file__).parent / 'web' / name).read_bytes(), mime)
            self.auth()
            controller = self.server.controller
            query = parse_qs(address.query, keep_blank_values=True, max_num_fields=10)
            routes = {'/api/status': controller.status, '/api/tasks': controller.tasks,
                      '/api/logs': lambda: controller.logs(query.get('kind', ['all'])[0]),
                      '/api/logs/settings': controller.logstore.policy, '/api/pts': controller.pts.snapshot,
                      '/api/pts/statistics': controller.pts.statistics,
                      '/api/downloaders': controller.management.snapshot, '/api/limits': controller.management.limits,
                      '/api/configuration': controller.configuration, '/api/instances': controller.registry.public,
                      '/api/settings/pending': controller.pending_public,
                      '/api/fleet/tasks': controller.fleet.snapshot, '/api/fleet/policy': controller.fleet.policy,
                      '/api/fleet/transfer/settings': controller.fleet.transfer_rules.public,
                      '/api/fleet/cleanup/settings': controller.fleet.cleanup.public,
                      '/api/fleet/cleanup/storage': controller.fleet.cleanup.storage.public,
                      '/api/fleet/limits': lambda: controller.fleet.limits(query.get('instance_id', [''])[0])}
            if path not in routes:
                raise ApiError('页面不存在', 404)
            self.send(200, routes[path]())
        except (ApiError, ManagementError) as error:
            self.send(error.status, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            pull.event('web_error', reason=type(error).__name__)
            self.send(500, {'error': '读取失败，请稍后重试'})

    def do_POST(self):
        try:
            if self.headers.get('X-Seedkeep-Request') != '1':
                raise ApiError('请求校验失败', 403)
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                raise ApiError('请求来源不匹配', 403)
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ApiError('请求需要 JSON 内容', 415)
            path = urlsplit(self.path).path
            length = int(self.headers.get('Content-Length', '0'))
            maximum = 16 * 1024 * 1024 if path in ('/api/transfer', '/api/fleet/transfer', '/api/fleet/transfer/run') else 131072
            if not 0 < length <= maximum:
                raise ApiError('请求内容大小无效')
            values = json.loads(self.rfile.read(length))
            controller = self.server.controller
            if path == '/api/login':
                token = controller.login(values, self.client_address[0])
                return self.send(200, {'ok': True}, cookie='seedkeep_session=' + token + '; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict')
            self.auth()
            if not isinstance(values, dict):
                raise ApiError('请求内容必须是对象')
            if path == '/api/logout':
                return self.send(200, {'ok': True}, cookie='seedkeep_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict')
            if path == '/api/status/refresh':
                if values:
                    raise ApiError('统计刷新不接受参数')
                result = controller.status(force=True)
            elif path == '/api/settings':
                result = controller.update_settings(values)
            elif path == '/api/settings/defer':
                result = controller.defer_settings(values)
            elif path == '/api/configuration':
                result = controller.update_configuration(values)
            elif path == '/api/configuration/check':
                result = controller.check_configuration(values)
            elif path == '/api/configuration/token':
                result = controller.revealed_token(values)
            elif path == '/api/instances/save':
                result = controller.save_instance(values)
            elif path == '/api/instances/delete':
                result = controller.delete_instance(values)
            elif path == '/api/instances/check':
                result = controller.check_instance(values)
            elif path == '/api/fleet/tasks/refresh':
                result = controller.fleet.snapshot(force=True)
            elif path == '/api/fleet/policy':
                result = controller.fleet.update_policy(values)
            elif path == '/api/fleet/actions':
                result = controller.fleet.action(values)
            elif path == '/api/fleet/transfer':
                result = controller.fleet.transfer(values)
            elif path == '/api/fleet/transfer/settings':
                result = controller.update_transfer_rules(values)
            elif path == '/api/fleet/transfer/preview':
                result = controller.fleet.transfer_rules.preview(values)
            elif path == '/api/fleet/transfer/run':
                result = controller.run_transfer_rules(values)
            elif path == '/api/fleet/cancel':
                result = controller.fleet.cancel(values)
            elif path == '/api/fleet/cleanup/settings':
                result = controller.fleet.cleanup.update(values)
            elif path == '/api/fleet/cleanup/preview':
                result = controller.fleet.cleanup.preview(values)
            elif path == '/api/fleet/cleanup/mode':
                result = controller.fleet.cleanup.mode(values)
            elif path == '/api/fleet/cleanup/run':
                result = controller.fleet.cleanup.run(values)
            elif path == '/api/fleet/cleanup/cancel':
                result = controller.fleet.cleanup.cancel(values)
            elif path == '/api/fleet/cleanup/recheck':
                result = controller.fleet.cleanup.recheck(values)
            elif path == '/api/fleet/cleanup/storage':
                result = controller.fleet.cleanup.update_storage(values)
            elif path == '/api/fleet/limits':
                result = controller.fleet.set_limits(values)
            elif path == '/api/logs/settings':
                result = controller.update_log_settings(values)
            elif path == '/api/logs/cleanup':
                result = controller.cleanup_logs(values)
            elif path == '/api/automation':
                result = controller.automation(values.get('enabled'))
            elif path == '/api/run':
                result = controller.start_run()
            elif path == '/api/pts/statistics/refresh':
                if values:
                    raise ApiError('统计刷新不接受参数')
                result = controller.pts.snapshot(force=True, include_rss=False)
            elif path == '/api/pts/refresh':
                result = controller.pts.snapshot(force=True)
            elif path == '/api/downloaders/refresh':
                result = controller.management.snapshot(force=True)
            elif path == '/api/limits/refresh':
                result = controller.management.limits(force=True)
            elif path == '/api/limits':
                result = controller.legacy_operation(controller.management.set_limits, values)
            elif path == '/api/cleanup/settings':
                management.policy(values)
                result = controller.fleet.update_policy({**controller.fleet.policy(), **values})
                controller.management.config.update(values)
                pull.save_json(controller.management.policy_path, controller.management.config)
            elif path == '/api/cleanup/delete':
                result = controller.legacy_operation(lambda body: controller.management.begin('delete', body), values)
            elif path == '/api/transfer':
                result = controller.legacy_operation(lambda body: controller.management.begin('transfer', body), values)
            elif path == '/api/transfer/cancel':
                result = controller.management.cancel(values)
            elif path == '/api/check':
                _, _, connection = controller.snapshot(force=True)
                if connection['error']:
                    raise ApiError(connection['error'], 502)
                result = {'ok': True}
            else:
                raise ApiError('操作不存在', 404)
            self.send(200, result)
        except (ApiError, ManagementError) as error:
            self.send(error.status, {'error': str(error)})
        except (ValueError, json.JSONDecodeError):
            self.send(400, {'error': '请求格式无效'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            pull.event('web_error', reason=type(error).__name__)
            self.send(500, {'error': '操作失败，请稍后重试'})


def main():
    os.umask(0o077)
    controller = Controller(os.environ.get('SEEDKEEP_CONFIG', '/data/docker_settings.json'))
    server = ThreadingHTTPServer((os.environ.get('WEB_HOST', '0.0.0.0'), int(os.environ.get('WEB_PORT', '8786'))), Handler)
    server.controller = controller
    scheduler = threading.Thread(target=controller.scheduler, daemon=True)
    scheduler.start()
    monitor = threading.Thread(target=controller.monitor, daemon=True)
    monitor.start()
    site_monitor = threading.Thread(target=controller.site_monitor, daemon=True)
    site_monitor.start()
    def shutdown(*_):
        controller.stop.set()
        if controller.running():
            controller.process.terminate()
            try:
                controller.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                controller.process.kill()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    pull.event('web_started', version=VERSION)
    server.serve_forever()
    server.server_close()


if __name__ == '__main__':
    main()
