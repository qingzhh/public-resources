"""Category-scoped cleanup with independent evidence and recoverable native data deletion.

Preview never writes. Content is inspected through Storage and is never unlinked here.
All mutating entry points use Fleet -> root Manager -> Controller/run.lock.
"""
import contextlib
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import urllib.parse
import uuid
import seedkeep_configuration as configuration
import seedkeep_pull as pull
from seedkeep_downloaders import HASH, ManagementError, count, pts_url, task_state
from seedkeep_storage import validate as validate_storage
from seedkeep_storage import Storage, StorageError
from torrent_meta import torrent_metadata

ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
HEX = re.compile(r'^[0-9a-f]{64}$')
UNREGISTERED = re.compile(r'(?:\bunregistered\s+torrent\b|\btorrent\s+(?:is\s+)?not\s+(?:registered|found)\b|'
                          r'\btorrent\s+(?:(?:has\s+been|was|is)\s+)?deleted\b|\bunknown\s+torrent\b|'
                          r'种子(?:已被|已|被)?删除|种子不存在|种子未注册)', re.I)
FAILURE = re.compile(r'(?:unauthori[sz]ed|forbidden|authentication|passkey|invalid\s+(?:key|token)|'
                     r'timed?\s*out|timeout|connection|resolve|network|认证|权限|密钥|超时|网络)', re.I)
DEFAULT_RULE = {'id': 'cleanup', 'name': '按分类清理', 'enabled': False, 'observe_only': True, 'scopes': [],
                'target_mode': 'follow', 'target': 1200, 'start_margin': 200, 'stop_margin': 100,
                'check_minutes': 5, 'seeders_enabled': True, 'unregistered_enabled': True,
                'seeders_mode': 'site', 'seeders_max': 10, 'seeders_wait_hours': 24,
                'unregistered_wait_hours': 24, 'max_per_run': 20, 'retry_seconds': 60,
                'sort': 'earliest', 'protected_categories': [], 'protected_tags': []}
COUNTS = ('mature', 'waiting', 'seeders', 'unregistered', 'shared', 'unknown', 'conflicts', 'protected')
STATUSES = ('idle', 'observing', 'cleaning', 'waiting', 'unknown', 'no_candidates', 'disabled', 'busy')
PHASES = ('queued', 'preparing', 'verified', 'waiting_result', 'completed', 'needs_review',
          'failed', 'cancelled', 'skipped', 'restoring')
TERMINAL = ('completed', 'needs_review', 'failed', 'cancelled', 'skipped')
REASONS = {'ready': '连续失效确认已满，等待文件归属复核', 'waiting': '连续失效确认尚未满',
           'unknown': '当前失效信息未知，已重置相应等待', 'protected': '完成状态、保护范围或恢复状态不允许删除',
           'shared': '存在其它副本、共享文件或文件归属无法确认，整项保留',
           'conflict': '同时匹配多条执行规则，整项保留', 'valid': '没有可信的持续失效证据，保留任务'}
BAD_STATE = '分类清理恢复资料无效，请检查备份；未清空资料'
MAX_TASKS, MAX_FILES, MAX_SCAN = 100000, 250000, 100


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _number(value, minimum, maximum, *, integer=True):
    return (type(value) is int if integer else type(value) in (int, float)) and math.isfinite(value) and minimum <= value <= maximum


def _text(value, limit=256, *, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def _strings(values, maximum=100):
    return (isinstance(values, list) and len(values) <= maximum and all(_text(v) for v in values)
            and len(set(values)) == len(values))


def validate(values):
    if not isinstance(values, dict) or set(values) != {'rules'} or not isinstance(values['rules'], list) or len(values['rules']) > 100:
        raise ManagementError('分类清理规则格式无效')
    ids = set()
    for rule in values['rules']:
        if not isinstance(rule, dict) or set(rule) != set(DEFAULT_RULE):
            raise ManagementError('分类清理规则字段无效')
        if not isinstance(rule['id'], str) or not ID.fullmatch(rule['id']) or rule['id'] in ids or not _text(rule['name'], 80):
            raise ManagementError('分类清理规则标识或名称无效')
        ids.add(rule['id'])
        for key in ('enabled', 'observe_only', 'seeders_enabled', 'unregistered_enabled'):
            if type(rule[key]) is not bool:
                raise ManagementError('分类清理开关无效')
        if rule['enabled'] and rule['observe_only']:
            raise ManagementError('观察模式不能同时执行自动删除')
        if not (rule['seeders_enabled'] or rule['unregistered_enabled']):
            raise ManagementError('请至少选择一种明确失效条件')
        if rule['target_mode'] not in ('follow', 'independent') or rule['seeders_mode'] not in ('site', 'manual') or rule['sort'] not in ('earliest', 'largest'):
            raise ManagementError('分类清理目标、人数口径或排序无效')
        bounds = {'target': (1, 100000), 'start_margin': (1, 100000), 'stop_margin': (0, 99999),
                  'check_minutes': (1, 1440), 'seeders_max': (1, 100000), 'max_per_run': (1, 50), 'retry_seconds': (10, 3600)}
        if any(not _number(rule[k], *bound) for k, bound in bounds.items()) or rule['stop_margin'] >= rule['start_margin']:
            raise ManagementError('请检查目标、开始/停止余量、周期与每轮上限；停止余量必须小于开始余量')
        if any(not _number(rule[k], 1, 8760, integer=False) for k in ('seeders_wait_hours', 'unregistered_wait_hours')):
            raise ManagementError('两类连续确认时间应为 1–8760 小时')
        if any(not _strings(rule[k]) for k in ('protected_categories', 'protected_tags')):
            raise ManagementError('保护分类或标签格式无效')
        scopes = rule['scopes']
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 100:
            raise ManagementError('请配置至少一个下载器与分类/标签范围')
        seen = set()
        for scope in scopes:
            if (not isinstance(scope, dict) or set(scope) != {'instance_id', 'values', 'include_empty'}
                    or not isinstance(scope['instance_id'], str) or not ID.fullmatch(scope['instance_id'])
                    or scope['instance_id'] in seen or not _strings(scope['values'])
                    or type(scope['include_empty']) is not bool or not (scope['values'] or scope['include_empty'])):
                raise ManagementError('分类/标签选择无效；空选择不会表示全部任务')
            seen.add(scope['instance_id'])
    return copy.deepcopy(values)


def rule_version(rule):
    return digest({k: v for k, v in rule.items() if k not in ('name', 'enabled', 'observe_only')})


def labels(record):
    raw = record['raw']
    return ([value.strip() for value in raw.get('tags', '').split(',') if value.strip()]
            if record['type'] == 'qb' else raw.get('labels', []))


def scope_match(rule, record):
    for scope in rule['scopes']:
        if scope['instance_id'] != record['instance_id']:
            continue
        selected = [record['raw'].get('category', '')] if record['type'] == 'qb' else labels(record)
        return (any(value in scope['values'] for value in selected if value)
                or (scope['include_empty'] and not any(selected)))
    return False


def protected(rule, record):
    return ((record['type'] == 'qb' and record['raw'].get('category', '') in rule['protected_categories'])
            or bool(set(labels(record)) & set(rule['protected_tags'])))


def unique(records):
    """Union aliases transitively, including v1/v2 and all instance copies, in linear space."""
    parents = list(range(len(records)))
    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    owners = {}
    for index, record in enumerate(records):
        for alias in set(record['aliases']) | {record['hash']}:
            if alias in owners:
                parents[root(index)] = root(owners[alias])
            else:
                owners[alias] = index
    groups = {}
    for index, record in enumerate(records):
        groups.setdefault(root(index), []).append(record)
    return list(groups.values())


def evidence(tracks, client, source, now, fresh_seconds, threshold):
    reports = {'seeders': [], 'unregistered': []}
    if not isinstance(tracks, list) or len(tracks) > 1000 or any(not isinstance(t, dict) for t in tracks):
        return dict.fromkeys(reports)
    for track in tracks:
        address = track.get('url' if client == 'qb' else 'announce')
        try:
            parsed = urllib.parse.urlsplit(address)
            trusted = parsed.scheme in ('http', 'https') and parsed.username is None and pts_url(address, source)
        except (ValueError, TypeError, AttributeError):
            trusted = False
        if not trusted:
            continue
        if client == 'qb':
            status = track.get('status')
            message = str(track.get('msg', ''))
            # qB's ordinary tracker response has no receipt time. A read must
            # never renew stale evidence; adapters may supply a verified receipt.
            stamp = track.get('_verified_receipt_at')
            fresh = (type(stamp) in (int, float) and math.isfinite(stamp)
                     and 0 < stamp <= now + 60 and now - stamp <= fresh_seconds)
            success, failed = status == 2, status == 4
            seeds = count(track.get('num_seeds'))
        else:
            events = [(track.get('last' + kind + 'Time'), kind) for kind in ('Announce', 'Scrape')
                      if type(track.get('last' + kind + 'Time')) in (int, float)
                      and math.isfinite(track['last' + kind + 'Time']) and track['last' + kind + 'Time'] > 0]
            stamp = max((t for t, _ in events), default=0)
            newest = [kind for t, kind in events if t == stamp]
            fresh = 0 < stamp <= now + 60 and now - stamp <= fresh_seconds
            success = bool(newest) and all(track.get('last' + kind + 'Succeeded') is True for kind in newest)
            failed = bool(newest) and all(track.get('last' + kind + 'Succeeded') is False for kind in newest)
            message = ' '.join(str(track.get('last' + kind + 'Result', '')) for kind in newest)
            if any(track.get('last' + kind + 'TimedOut') is True for kind in newest):
                fresh = False
            seeds = count(track.get('seederCount'))
        explicit = bool(UNREGISTERED.search(message))
        uncertain = not fresh or FAILURE.search(message) or (success and explicit)
        reports['unregistered'].append(None if uncertain else True if failed and explicit else False if success else None)
        reports['seeders'].append((seeds > threshold) if (not uncertain and success and seeds is not None and threshold is not None) else None)
    return {key: (True if values and all(v is True for v in values) else
                  False if values and all(v is False for v in values) else None) for key, values in reports.items()}


def _summary(rule, target):
    effective = target if rule['target_mode'] == 'follow' else rule['target']
    return {'id': rule['id'], 'inventory': None, 'target': effective, 'start_line': effective + rule['start_margin'],
            'stop_line': effective + rule['stop_margin'], 'active': False, 'status': 'unknown', 'budget': 0,
            **dict.fromkeys(COUNTS, 0), 'last_checked_at': None, 'next_check_at': None, 'last_result': None}


def evaluate(rule, records, previous, now, target, *, threshold_version='', conflicts=frozenset(), unknown=False):
    """Pure observation and inventory logic. The caller decides when to persist it."""
    version = rule_version(rule)
    previous = previous if previous.get('version') == version else {}
    report = _summary(rule, target)
    report.update(last_checked_at=now, next_check_at=now + rule['check_minutes'] * 60,
                  last_result=previous.get('summary', {}).get('last_result'))
    state = {'version': version, 'active': False, 'observations': {}, 'summary': report,
             'scan_cursor': previous.get('scan_cursor', 0)}
    if unknown:
        return report, state, []
    selected = [r for r in records if r['pts'] and scope_match(rule, r)]
    groups = unique(records)
    copy_counts = {r['id']: len(group) for group in groups for r in group}
    selected_ids = {r['id'] for r in selected}
    report['inventory'] = sum(any(r['id'] in selected_ids for r in group) for group in groups)
    # Followed targets change only threshold/budget, never the evidence version.
    active = bool(previous.get('active')) or report['inventory'] >= report['start_line']
    active = active and report['inventory'] > report['stop_line']
    items = []
    for record in selected:
        key, current, old = record['id'], {}, previous.get('observations', {}).get(record['id'], {})
        if key in conflicts:
            status = 'conflict'
        elif protected(rule, record) or not record['safe']:
            status = 'protected'
        elif copy_counts[key] > 1:
            status = 'shared'
        else:
            reasons = {}
            for reason in ('seeders', 'unregistered'):
                if rule[reason + '_enabled'] and record['evidence'].get(reason) is True:
                    old_reason = old.get('reasons', {}).get(reason, {})
                    reason_version = digest([version, threshold_version if reason == 'seeders' else 'unregistered'])
                    continuous = (old.get('signature') == record['signature'] and old_reason.get('version') == reason_version
                                  and 0 <= now - old_reason.get('last', -1e20) <= rule['check_minutes'] * 180)
                    reasons[reason] = {'first': old_reason['first'] if continuous else now, 'last': now, 'version': reason_version}
            if reasons:
                current = {'signature': record['signature'], 'reasons': reasons}
                state['observations'][key] = current
                mature = [r for r, observation in reasons.items() if now - observation['first'] >= rule[r + '_wait_hours'] * 3600]
                status = 'ready' if mature else 'waiting'
                for reason in reasons:
                    report[reason] += 1
            else:
                status = ('unknown' if any(rule[r + '_enabled'] and record['evidence'].get(r) is None
                                         for r in ('seeders', 'unregistered')) else 'valid')
        since = min((r['first'] for r in current.get('reasons', {}).values()), default=None)
        reason_names = [r for r in current.get('reasons', {})]
        reason_text = ' / '.join('人数超限' if r == 'seeders' else '明确未注册/已删除' for r in reason_names)
        items.append({'instance_id': record['instance_id'], 'hash': record['hash'], 'name': record['name'],
                      'rule_id': rule['id'], 'status': status, 'reason': (reason_text + '；' if reason_text else '') + REASONS[status],
                      'since': since, 'elapsed_seconds': max(0, now - since) if since is not None else 0, 'size': record['size']})
        if (status == 'unknown' and record.get('type') == 'qb'
                and record.get('tracks') and not any('_verified_receipt_at' in t for t in record['tracks'])):
            items[-1]['reason'] = 'qB Tracker 回执未提供可核验时间，无法确认连续失效；保留任务与原文件'
        counter = {'ready': 'mature', 'conflict': 'conflicts'}.get(status, status)
        if counter in COUNTS:
            report[counter] += 1
    report.update(active=active, budget=min(rule['max_per_run'], max(0, report['inventory'] - report['stop_line'])) if active else 0)
    report['status'] = ('cleaning' if active and report['mature'] else 'waiting' if active and report['waiting']
                        else 'no_candidates' if active else 'idle')
    if not rule['enabled']:
        report['status'] = 'observing' if rule['observe_only'] else 'disabled'
    state['active'] = active
    items.sort(key=(lambda item: (-item['size'], item['since'] or now, item['hash'])) if rule['sort'] == 'largest'
               else (lambda item: (item['since'] if item['since'] is not None else now, item['hash'])))
    return report, state, items


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _read(path, default, maximum=64 * 1024 * 1024):
    try:
        if _linked(path):
            raise ValueError()
        if not path.exists():
            return copy.deepcopy(default)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError()
        with path.open('rb') as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError()
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique_json)
    except (ValueError, OSError, UnicodeError, RecursionError):
        raise ManagementError(BAD_STATE, 409) from None


def _save(path, value):
    if _linked(path) or path.with_suffix(path.suffix + '.tmp').exists() or _linked(path.with_suffix(path.suffix + '.tmp')):
        raise ManagementError(BAD_STATE, 409)
    pull.save_json(path, value)


def _safe_task(kind, task):
    size = task.get('total_size' if kind == 'qb' else 'totalSize')
    progress = task.get('progress' if kind == 'qb' else 'percentDone')
    if not _number(size, 1, 2 ** 63 - 1) or not _number(progress, 1, 1, integer=False):
        return False
    if kind == 'qb':
        return (task.get('state') in ('uploading', 'stalledUP', 'forcedUP', 'queuedUP', 'stoppedUP', 'pausedUP')
                and task.get('amount_left') == 0 and _number(task.get('completed'), size, 2 ** 63 - 1))
    return (type(task.get('status')) is int and task['status'] in (0, 5, 6) and task.get('leftUntilDone') == 0
            and task.get('haveValid') == size and task.get('haveUnchecked') == 0
            and type(task.get('error', 0)) is int and task.get('error', 0) in (0, 1, 2))


def _paused(kind, task):
    return task.get('state') in ('stoppedUP', 'pausedUP') if kind == 'qb' else type(task.get('status')) is int and task['status'] == 0


def _signature(kind, task, aliases):
    return digest([kind, sorted(aliases), task.get('total_size' if kind == 'qb' else 'totalSize'),
                   task.get('save_path' if kind == 'qb' else 'downloadDir'),
                   task.get('category', '') if kind == 'qb' else sorted(task.get('labels', [])),
                   sorted(v.strip() for v in task.get('tags', '').split(',') if v.strip()) if kind == 'qb' else []])


class Cleanup:
    def __init__(self, fleet, *, storage=None):
        self.fleet, self.directory = fleet, fleet.directory
        self.path = self.directory / 'cleanup_rules.json'
        self.state_path = self.directory / 'cleanup_rules_state.json'
        self.storage = storage if storage is not None else Storage(self.directory)
        self.values = validate(_read(self.path, {'rules': []}, 1024 * 1024))
        self.state = _read(self.state_path, {'version': 1, 'rules': {}, 'job': None})
        self.annotations = {}
        self._validate_state()
        self.review_due = bool(self.state['job'] and self.state['job']['status'] == 'needs_review')

    def _validate_state(self):
        def bad():
            raise ManagementError(BAD_STATE, 409)
        state = self.state
        if not isinstance(state, dict) or set(state) != {'version', 'rules', 'job'} or type(state['version']) is not int or state['version'] != 1:
            bad()
        if not isinstance(state['rules'], dict) or len(state['rules']) > 100:
            bad()
        for identity, value in state['rules'].items():
            if (not ID.fullmatch(identity) or not isinstance(value, dict)
                    or set(value) != {'version', 'active', 'observations', 'summary', 'scan_cursor'}
                    or not isinstance(value['version'], str) or not HEX.fullmatch(value['version'])
                    or type(value['active']) is not bool or not _number(value['scan_cursor'], 0, MAX_TASKS)
                    or not isinstance(value['observations'], dict) or len(value['observations']) > MAX_TASKS):
                bad()
            summary = value['summary']
            if (not isinstance(summary, dict) or set(summary) != set(_summary(DEFAULT_RULE, 1200)) or summary['id'] != identity
                    or summary['status'] not in STATUSES or type(summary['active']) is not bool
                    or (summary['inventory'] is not None and not _number(summary['inventory'], 0, MAX_TASKS))
                    or any(not _number(summary[k], 0, 300000) for k in ('target', 'start_line', 'stop_line', 'budget', *COUNTS))
                    or any(summary[k] is not None and not _number(summary[k], 0, 1e13, integer=False) for k in ('last_checked_at', 'next_check_at'))
                    or summary['last_result'] not in (None, 'completed', 'failed', 'cancelled', 'needs_review')):
                bad()
            for key, observation in value['observations'].items():
                parts = key.split(':')
                if (len(parts) != 2 or not ID.fullmatch(parts[0]) or not HASH.fullmatch(parts[1]) or not isinstance(observation, dict)
                        or set(observation) != {'signature', 'reasons'} or not isinstance(observation['signature'], str)
                        or not HEX.fullmatch(observation['signature']) or not isinstance(observation['reasons'], dict)
                        or not 1 <= len(observation['reasons']) <= 2 or set(observation['reasons']) - {'seeders', 'unregistered'}):
                    bad()
                for reason in observation['reasons'].values():
                    if (not isinstance(reason, dict) or set(reason) != {'first', 'last', 'version'}
                            or not _number(reason['first'], 0, 1e13, integer=False) or not _number(reason['last'], reason['first'], 1e13, integer=False)
                            or not isinstance(reason['version'], str) or not HEX.fullmatch(reason['version'])):
                        bad()
        job = state['job']
        if job is None:
            return
        keys = {'id', 'kind', 'status', 'rule_id', 'rule', 'started_at', 'finished_at', 'cancel_requested', 'items'}
        if (not isinstance(job, dict) or set(job) != keys or not isinstance(job['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', job['id'])
                or job['kind'] != 'cleanup_data' or job['status'] not in ('running', 'completed', 'cancelled', 'needs_review', 'failed')
                or type(job['cancel_requested']) is not bool or not _number(job['started_at'], 0, 1e13, integer=False)
                or (job['finished_at'] is not None and not _number(job['finished_at'], job['started_at'], 1e13, integer=False))
                or not isinstance(job['items'], list) or not 1 <= len(job['items']) <= 50):
            bad()
        try:
            validate({'rules': [job['rule']]})
            if job['rule_id'] != job['rule']['id']:
                bad()
            identities = set()
            for item in job['items']:
                required = {'instance_id', 'type', 'hash', 'aliases', 'name', 'size', 'phase', 'reason', 'snapshot', 'descriptor',
                            'original', 'started_at', 'intent_at', 'signature', 'resume_requested', 'outcome'}
                if (not isinstance(item, dict) or set(item) != required or not isinstance(item['instance_id'], str) or not ID.fullmatch(item['instance_id'])
                        or item['type'] not in ('qb', 'tr') or not isinstance(item['hash'], str) or not HASH.fullmatch(item['hash'])
                        or not isinstance(item['aliases'], list) or not 1 <= len(item['aliases']) <= 64
                        or any(not isinstance(v, str) or not HASH.fullmatch(v) for v in item['aliases'])
                        or item['hash'] not in item['aliases'] or not _text(item['name'], 500, empty=True)
                        or not _number(item['size'], 1, 2 ** 63 - 1) or item['phase'] not in PHASES
                        or type(item['resume_requested']) is not bool or item['outcome'] not in ('skipped', 'failed', 'cancelled')
                        or (item['reason'] is not None and not _text(item['reason'], 256))
                        or any(item[k] is not None and not _number(item[k], job['started_at'], 1e13, integer=False) for k in ('started_at', 'intent_at'))):
                    bad()
                identity = (item['instance_id'], item['hash'])
                if identity in identities:
                    bad()
                identities.add(identity)
                if item['original'] is not None and (not isinstance(item['original'], dict) or set(item['original']) != {'paused', 'force'}
                                                    or any(type(v) is not bool for v in item['original'].values())):
                    bad()
                if item['signature'] is not None and (not isinstance(item['signature'], str) or not HEX.fullmatch(item['signature'])):
                    bad()
                if item['snapshot'] is not None:
                    self.storage.recheck(item['snapshot'])  # Validates even when files have disappeared.
                if item['descriptor'] is not None:
                    descriptor = item['descriptor']
                    if (not isinstance(descriptor, dict) or set(descriptor) != {'instance_id', 'type', 'hash', 'aliases', 'download_dir', 'files'}
                            or any(descriptor[k] != item[k] for k in ('instance_id', 'type', 'hash', 'aliases'))
                            or not isinstance(descriptor['download_dir'], str) or not descriptor['download_dir'].startswith('/')
                            or not isinstance(descriptor['files'], list) or not 1 <= len(descriptor['files']) <= 20000
                            or any(not isinstance(f, dict) or set(f) != {'name', 'size'} or not _text(f['name'], 4096)
                                   or not _number(f['size'], 0, 2 ** 63 - 1) for f in descriptor['files'])
                            or sum(f['size'] for f in descriptor['files']) != item['size']):
                        bad()
                if item['phase'] in ('preparing', 'verified', 'waiting_result', 'restoring') and any(item[k] is None for k in ('snapshot', 'descriptor', 'original', 'signature', 'started_at')):
                    bad()
                if item['phase'] == 'waiting_result' and item['intent_at'] is None:
                    bad()
        except (StorageError, KeyError, ValueError, TypeError, RecursionError):
            bad()

    def _save(self):
        self._validate_state()
        _save(self.state_path, self.state)

    @contextlib.contextmanager
    def _guard(self):
        with self.fleet.lock:
            manager = getattr(self.fleet, 'legacy', None)
            with (manager.lock if manager is not None else contextlib.nullcontext()):
                with self.fleet.guard():
                    yield

    def _busy_other(self):
        return self.fleet.runner_busy() or any((manager.state.get('job') or {}).get('status') in ('waiting', 'running') for manager in self.fleet._all_managers())

    def busy(self):
        return bool(self.state['job'] and self.state['job']['status'] == 'running')

    def _require_idle(self):
        if self.busy() or self._busy_other():
            raise ManagementError('已有补量、转种、恢复或清理作业，请等待完成或取消', 409)

    def _revision(self):
        return digest([self.values, self.fleet.registry.public()['revision'], self.storage.public()['revision']])

    def _require_revision(self, value):
        if not isinstance(value, str) or not HEX.fullmatch(value) or not hmac.compare_digest(value, self._revision()):
            raise ManagementError('分类清理规则、下载器或存储版本已变化；未保存草稿保留，请刷新后核对', 409)

    def _rule(self, identity):
        value = next((r for r in self.values['rules'] if r['id'] == identity), None)
        if value is None:
            raise ManagementError('分类清理规则不存在', 404)
        return value

    def _bind(self, values):
        ids = {r['id'] for r in self.fleet.registry.items()}
        if any(s['instance_id'] not in ids for r in values['rules'] for s in r['scopes']):
            raise ManagementError('分类清理范围包含不存在的下载器')

    def public_job(self):
        job = self.state['job']
        if not job:
            return None
        public = {k: copy.deepcopy(job[k]) for k in ('id', 'kind', 'status', 'rule_id', 'started_at', 'finished_at', 'cancel_requested')}
        public['items'] = [{k: copy.deepcopy(item[k]) for k in ('instance_id', 'hash', 'name', 'size', 'phase', 'reason')} for item in job['items']]
        public['completed'] = sum(item['phase'] == 'completed' for item in job['items'])
        public['logical_bytes'] = sum(item['size'] for item in job['items'] if item['phase'] == 'completed')
        return public

    def summary(self):
        target = self.fleet.settings().get('target', 1100)
        reports = []
        for rule in self.values['rules']:
            value = self.state['rules'].get(rule['id'], {})
            report = copy.deepcopy(value.get('summary', _summary(rule, target)))
            current = _summary(rule, target)
            for key in ('target', 'start_line', 'stop_line'):
                report[key] = current[key]
            if rule['target_mode'] == 'follow' and report['inventory'] is not None:
                report['budget'] = min(rule['max_per_run'], max(0, report['inventory'] - report['stop_line'])) if report['active'] else 0
            if not rule['enabled'] and not rule['observe_only']:
                report['status'] = 'disabled'
            reports.append(report)
        return {'rules': reports, 'job': self.public_job()}

    def public(self):
        with self.fleet.lock:
            return {'values': copy.deepcopy(self.values), 'revision': self._revision(), 'saved': self.path.exists(),
                    'instances_revision': self.fleet.registry.public()['revision'], 'target': self.fleet.settings().get('target', 1100),
                    'runtime': self.summary(), 'capabilities': [{'instance_id': r['id'], 'can_inspect': self.storage.capability(r['id'])}
                                                              for r in self.fleet.registry.items()]}

    def update(self, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'values'}:
            raise ManagementError('分类清理保存请求无效')
        with self._guard():
            self._require_idle()
            self._require_revision(body['revision'])
            values = validate(body['values'])
            self._bind(values)
            old = {r['id']: r for r in self.values['rules']}
            states = {}
            for rule in values['rules']:
                prior = old.get(rule['id'])
                if rule['enabled'] and (not prior or not prior['enabled']):
                    raise ManagementError('请先保存规则，再对已保存版本明确启用自动删除')
                changed = prior is None or rule_version(prior) != rule_version(rule)
                if changed:
                    rule.update(enabled=False, observe_only=True)
                elif rule['id'] in self.state['rules']:
                    states[rule['id']] = self.state['rules'][rule['id']]
            _save(self.path, values)
            self.values, self.state['rules'] = values, states
            self.annotations = {}
            self._save()
            self.fleet._log('category_cleanup_rules_saved')
            return {'ok': True, **self.public()}

    def pause(self, *, reset=True):
        changed = False
        for rule in self.values['rules']:
            if rule['enabled']:
                rule.update(enabled=False, observe_only=True)
                changed = True
        if changed:
            _save(self.path, self.values)
        if reset and self.state['rules']:
            self.state['rules'] = {}
            self.annotations = {}
            self._save()

    def update_storage(self, body):
        with self._guard():
            self._require_idle()
            # Verify before pausing, then fail safely if any subsequent save fails.
            if not isinstance(body, dict) or set(body) != {'revision', 'values'}:
                raise ManagementError('存储设置请求无效')
            validate_storage(body['values'], allowed_roots=self.storage.allowed_roots)
            supplied = body['revision']
            if (not isinstance(supplied, str) or not HEX.fullmatch(supplied)
                    or not hmac.compare_digest(supplied, self.storage.public()['revision'])):
                raise ManagementError('存储设置已变化，请刷新后重试', 409)
            registered = {r['id'] for r in self.fleet.registry.items()}
            if any(m['instance_id'] not in registered for m in body['values']['mappings']):
                raise ManagementError('存储映射包含不存在的下载器')
            self.pause()
            result = self.storage.update(body)
            self.fleet._log('cleanup_storage_saved')
            return result

    def mode(self, body):
        if not isinstance(body, dict) or set(body) - {'revision', 'rule_id', 'action', 'confirm'} or body.get('action') not in ('enable', 'pause', 'observe'):
            raise ManagementError('分类清理模式请求无效')
        with self._guard():
            self._require_idle()
            self._require_revision(body.get('revision'))
            rule = self._rule(body.get('rule_id'))
            if not self.path.exists():
                raise ManagementError('请先保存分类清理规则', 409)
            if body['action'] == 'enable':
                if body.get('confirm') != 'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA':
                    raise ManagementError('请明确确认自动删除任务及原文件；元数据备份无法恢复已删除内容')
                if self.state['job'] and self.state['job']['status'] == 'needs_review':
                    raise ManagementError('已有删除结果待核验，请先核对任务和文件', 409)
                if not all(self.storage.capability(r['id']) for r in self.fleet.registry.items()):
                    raise ManagementError('只读文件检查映射尚未覆盖全部已登记下载器，不能启用原文件删除', 409)
                # A real inventory/reference read proves coverage; no task/file writes.
                read = self._read_inventory(self.values)
                if not self.storage.covers(self._references(read)):
                    raise ManagementError('实际文件路径未完整覆盖受控只读映射，不能启用原文件删除', 409)
                for scope in rule['scopes']:
                    self.fleet.registry.get(scope['instance_id'])
                rule.update(enabled=True, observe_only=False)
            else:
                rule.update(enabled=False, observe_only=body['action'] == 'observe')
            _save(self.path, self.values)
            self.fleet._log('category_cleanup_mode_changed', reason=body['action'])
            return {'ok': True, **self.public()}

    def _read_inventory(self, values):
        records, apis, now = [], {}, self.fleet.clock()
        settings = self.fleet.settings()
        source = configuration.resolve_source(self.fleet.source(), settings)
        batch = _read(self.directory / 'batch.json', {})
        aliases = {}
        for bucket in ('accepted', 'pending', 'unconfirmed'):
            for item in batch.get(bucket, {}).values():
                known = pull.record_hashes(item)
                for alias in known:
                    aliases.setdefault(alias, set()).update(known)
        try:
            for instance in self.fleet.registry.items():
                api = self.fleet.registry.api(instance['id'], enabled=False)
                apis[instance['id']] = api
                tasks = api.inventory_one()
                if not isinstance(tasks, list) or len(tasks) + len(records) > MAX_TASKS:
                    raise ValueError()
                seen = set()
                for task in tasks:
                    kind = instance['type']
                    value = task.get('hash' if kind == 'qb' else 'hashString', '').lower()
                    size = task.get('total_size' if kind == 'qb' else 'totalSize')
                    if not HASH.fullmatch(value) or value in seen or not _number(size, 0, 2 ** 63 - 1):
                        raise ValueError()
                    seen.add(value)
                    known = {value}
                    if kind == 'qb':
                        known.update(str(task.get(k, '')).lower() for k in ('infohash_v1', 'infohash_v2') if HASH.fullmatch(str(task.get(k, ''))))
                        if not _text(task.get('category', ''), 4096, empty=True) or not _text(task.get('tags', ''), 8192, empty=True):
                            raise ValueError()
                    elif not _strings(task.get('labels', []), 1000):
                        raise ValueError()
                    for alias in tuple(known):
                        known.update(aliases.get(alias, ()))
                    if len(known) > 64:
                        raise ValueError()
                    record = {'id': instance['id'] + ':' + value, 'instance_id': instance['id'], 'type': kind, 'hash': value,
                              'aliases': sorted(known), 'name': self.fleet._safe_text(task.get('name', '未命名任务')),
                              'size': size, 'raw': task, 'pts': bool(any(alias in aliases for alias in known) or
                              pts_url(task.get('tracker'), source) if kind == 'qb' else any(alias in aliases for alias in known) or
                              any(pts_url(t.get('announce'), source) for t in task.get('trackerStats', []))),
                              'safe': instance['enabled'] and _safe_task(kind, task), 'signature': _signature(kind, task, known),
                              'tracks': None, 'evidence': {'seeders': None, 'unregistered': None}}
                    # The all-instance list also protects aliases outside selected scopes.
                    if record['pts'] and any(scope_match(rule, record) for rule in values['rules']):
                        try:
                            record['tracks'] = api.qtrackers(value) if kind == 'qb' else task.get('trackerStats', [])
                        except Exception:
                            record['tracks'] = None
                    records.append(record)
            return {'records': records, 'apis': apis, 'source': source, 'settings': settings}
        except Exception:
            raise ManagementError('下载器库存或共享引用读取失败；数量未知，本轮未删除', 502) from None

    def _site_threshold(self):
        try:
            site = self.fleet.pts()
            now, settings = self.fleet.clock(), self.fleet.settings()
            stamp = time.mktime(time.strptime(site.get('synced_at', ''), '%Y-%m-%d %H:%M:%S'))
            if (not site.get('available') or site.get('stale') or not -60 <= now - stamp <= settings.get('refill_site_max_age_minutes', 120) * 60):
                return None
            return count(site.get('seeders_max'))
        except Exception:
            return None

    def _references(self, read):
        descriptors, total = [], 0
        try:
            for record in read['records']:
                files = read['apis'][record['instance_id']].files_one(record['type'], record['hash'])
                total += len(files)
                if total > MAX_FILES or sum(f['size'] for f in files) != record['size']:
                    raise ValueError()
                descriptors.append({'instance_id': record['instance_id'], 'type': record['type'], 'hash': record['hash'],
                                    'aliases': record['aliases'], 'download_dir': record['raw'].get('save_path' if record['type'] == 'qb' else 'downloadDir'),
                                    'files': sorted(files, key=lambda f: f['name'])})
            return descriptors
        except Exception:
            raise ManagementError('完整文件清单、体积或共享引用无法确认；整项保留', 502) from None

    def _plan(self, values, *, persist=False, files=False, rule_id=None, manual=False):
        self._bind(values)
        now, target = self.fleet.clock(), self.fleet.settings().get('target', 1100)
        try:
            read, unknown = self._read_inventory(values), False
        except ManagementError:
            read, unknown = {'records': []}, True
        site_limit = self._site_threshold() if any(r['seeders_enabled'] and r['seeders_mode'] == 'site' for r in values['rules']) and not unknown else None
        conflicts = set()
        for record in read['records']:
            matches = [r for r in values['rules'] if (r['enabled'] or manual and r['id'] == rule_id) and scope_match(r, record)]
            if len(matches) > 1:
                conflicts.add(record['id'])
        reports, all_items, states = [], [], {}
        for rule in values['rules']:
            if rule_id is not None and rule['id'] != rule_id:
                continue
            limit = rule['seeders_max'] if rule['seeders_mode'] == 'manual' else site_limit
            for record in read['records']:
                record['evidence'] = evidence(record.get('tracks'), record['type'], read['source'], now,
                                               read['settings'].get('tracker_fresh_seconds', 7200), limit)
            report, state, items = evaluate(rule, read['records'], self.state['rules'].get(rule['id'], {}), now, target,
                                             threshold_version=digest([rule['seeders_mode'], limit]), conflicts=conflicts, unknown=unknown)
            states[rule['id']], reports = state, reports + [report]
            all_items.extend(items)
        references, snapshots = None, {}
        if files:
            ready = [i for i in all_items if i['status'] == 'ready']
            if ready:
                try:
                    references = self._references(read)
                except ManagementError:
                    references = None
                for report in reports:
                    chosen = [i for i in ready if i['rule_id'] == report['id']]
                    cursor = states[report['id']]['scan_cursor'] % max(1, len(chosen))
                    window = (chosen[cursor:] + chosen[:cursor])[:MAX_SCAN]
                    for item in window:
                        candidate = next((d for d in references or [] if d['instance_id'] == item['instance_id'] and d['hash'] == item['hash']), None)
                        try:
                            snapshot = self.storage.inspect(candidate, references) if candidate else None
                        except StorageError:
                            snapshot = None
                        if not snapshot or not snapshot['ok']:
                            item.update(status='shared', reason=REASONS['shared'])
                            report['shared'] += 1
                            report['mature'] -= 1
                        else:
                            snapshots[(item['instance_id'], item['hash'])] = snapshot
                    # Do not promise file eligibility beyond the bounded inspected window.
                    for item in chosen:
                        if item not in window:
                            item.update(status='unknown', reason='本轮文件复核有界，后续轮次继续检查')
                            report['unknown'] += 1
                            report['mature'] -= 1
                    states[report['id']]['scan_cursor'] = (cursor + len(window)) % max(1, len(chosen))
        counts = {key: sum(r[key] for r in reports) for key in COUNTS}
        if persist:
            self.state['rules'].update(states)
            checked_ids = set(states)
            self.annotations = {key: retained for key, entries in self.annotations.items()
                                if (retained := [i for i in entries if i['rule_id'] not in checked_ids])}
            for item in all_items:
                self.annotations.setdefault(item['instance_id'] + ':' + item['hash'], []).append(item)
            self._save()
        return {'rules': reports, 'items': all_items[:100], 'counts': counts, 'checked_at': now,
                'truncated': len(all_items) > 100}, all_items, read, references, snapshots

    def preview(self, body):
        if not isinstance(body, dict) or set(body) - {'values', 'rule_id'} or 'values' not in body:
            raise ManagementError('分类清理预览请求无效')
        values = validate(body['values'])
        if 'rule_id' in body and not any(r['id'] == body['rule_id'] for r in values['rules']):
            raise ManagementError('预览规则不存在')
        with self._guard():
            # Guard rejects native run.lock contention before any network read.
            return self._plan(values, files=True, rule_id=body.get('rule_id'))[0]

    def run(self, body, *, scheduled=False):
        if not isinstance(body, dict) or set(body) != {'revision', 'rule_id', 'confirm'} or body['confirm'] != 'RUN_DELETE_TASKS_AND_DATA':
            raise ManagementError('请明确确认按已保存规则删除任务及原文件')
        with self._guard():
            return self._run_locked(body)

    def _run_locked(self, body):
        """Caller holds the Fleet -> Manager -> Controller -> run.lock chain."""
        self._require_idle()
        self._require_revision(body['revision'])
        rule = self._rule(body['rule_id'])
        if not self.path.exists():
            raise ManagementError('请先保存分类清理规则', 409)
        if self.state['job'] and self.state['job']['status'] == 'needs_review':
            raise ManagementError('已有删除结果待核验，请先重查任务和文件', 409)
        report, items, read, references, snapshots = self._plan(self.values, persist=True, files=True, rule_id=rule['id'], manual=True)
        summary = report['rules'][0]
        budget = min(summary['budget'], self.fleet.settings().get('delete_max_per_job', 50))
        chosen = [i for i in items if i['status'] == 'ready' and (i['instance_id'], i['hash']) in snapshots][:budget]
        if not chosen:
            return {'ok': True, 'job': None, 'message': '库存门槛、连续证据或文件保护条件未满足，本轮未删除', 'counts': report['counts']}
        records = {r['id']: r for r in read['records']}
        self.state['job'] = {'id': uuid.uuid4().hex, 'kind': 'cleanup_data', 'status': 'running', 'rule_id': rule['id'],
                             'rule': copy.deepcopy(rule), 'started_at': self.fleet.clock(), 'finished_at': None,
                             'cancel_requested': False, 'items': []}
        for item in chosen:
            record = records[item['instance_id'] + ':' + item['hash']]
            self.state['job']['items'].append({'instance_id': record['instance_id'], 'type': record['type'], 'hash': record['hash'],
                'aliases': record['aliases'], 'name': record['name'], 'size': record['size'], 'phase': 'queued', 'reason': None,
                'snapshot': None, 'descriptor': None, 'original': None, 'started_at': None, 'intent_at': None,
                'signature': None, 'resume_requested': False, 'outcome': 'skipped'})
        self._save()
        self.fleet._log('category_cleanup_started', processed=len(chosen))
        return {'ok': True, 'job': self.public_job(), 'counts': report['counts']}

    def cancel(self, body):
        with self.fleet.lock:
            job = self.state['job']
            if not isinstance(body, dict) or set(body) != {'job_id'} or not job or body['job_id'] != job['id'] or not self.busy():
                raise ManagementError('没有可取消的当前分类清理', 409)
            job['cancel_requested'] = True
            self._save()
            return {'ok': True, 'job': self.public_job()}

    def _fresh_candidate(self, item):
        job = self.state['job']
        rule = self._rule(job['rule_id'])
        if rule_version(rule) != rule_version(job['rule']):
            raise ManagementError('执行规则版本已变化，未删除', 409)
        # Avoid rotating the bounded scan away from the current queued candidate.
        report, items, read, references, snapshots = self._plan(self.values, persist=True, files=False, rule_id=rule['id'], manual=True)
        current = next((r for r in read['records'] if r['instance_id'] == item['instance_id'] and r['hash'] == item['hash']), None)
        candidate = next((i for i in items if i['instance_id'] == item['instance_id'] and i['hash'] == item['hash']), None)
        if not current or not candidate or candidate['status'] != 'ready' or report['rules'][0]['budget'] < 1:
            raise ManagementError('当前库存、范围或连续失效证据已变化，未删除', 409)
        references = self._references(read)
        descriptor = next((d for d in references if d['instance_id'] == item['instance_id'] and d['hash'] == item['hash']), None)
        snapshot = self.storage.inspect(descriptor, references)
        if not snapshot['ok']:
            raise ManagementError(REASONS['shared'], 409)
        if current['aliases'] != item['aliases'] or current['size'] != item['size']:
            raise ManagementError('任务身份或体积已变化，未删除', 409)
        if item['signature'] is not None and (current['signature'] != item['signature'] or descriptor != item['descriptor']
                                             or snapshot['files'] != item['snapshot']['files'] or not self.storage.recheck(item['snapshot'])):
            raise ManagementError('暂停前后任务范围或文件身份已变化，未删除', 409)
        return current, read['apis'][item['instance_id']], descriptor, snapshot

    def _abort(self, item, reason, outcome='skipped'):
        item.update(reason=reason, outcome=outcome)
        if item['intent_at'] is not None:
            item['phase'] = 'waiting_result'
        else:
            item['phase'] = 'restoring' if item['original'] is not None and not item['original']['paused'] else outcome
        self._save()

    def _one(self, item):
        api = self.fleet.registry.api(item['instance_id'], enabled=False)
        tasks = api.inventory_one()
        matches = [t for t in tasks if str(t.get('hash' if item['type'] == 'qb' else 'hashString', '')).lower() == item['hash']]
        if len(matches) > 1:
            raise ManagementError('任务身份无法确认', 409)
        return (matches[0] if matches else None), api

    def _restore(self, item):
        try:
            task, api = self._one(item)
            if task is None or not _safe_task(item['type'], task):
                raise ManagementError('暂停状态恢复需要人工核对', 409)
            current_files = api.files_one(item['type'], item['hash'])
            directory = task.get('save_path' if item['type'] == 'qb' else 'downloadDir')
            if (directory != item['descriptor']['download_dir'] or sorted(current_files, key=lambda f: f['name']) != item['descriptor']['files']
                    or not self.storage.recheck(item['snapshot'])):
                raise ManagementError('暂停状态恢复需要人工核对', 409)
            if not _paused(item['type'], task):
                force_ok = not item['original']['force'] or task.get('force_start') is True or task.get('state') == 'forcedUP'
                if not force_ok:
                    raise ManagementError('强制做种状态恢复未确认', 409)
                item['phase'] = item['outcome']
            elif not item['resume_requested']:
                item['resume_requested'] = True
                self._save()
                try:
                    api.start_one(item['type'], item['hash'], force=item['original']['force'])
                except Exception:
                    pass  # Lost reply is reconciled, never retried without a fresh identity read.
            elif self.fleet.clock() - item['started_at'] > self.fleet.settings().get('pause_timeout_seconds', 120):
                item.update(phase='needs_review', reason='暂停前状态恢复未确认，请核对下载器')
            self._save()
        except Exception:
            item.update(phase='needs_review', reason='暂停前状态恢复未确认，请核对任务与文件')
            self._save()

    def _backup(self, item, record, api):
        operation = self.directory / 'operations' / self.state['job']['id']
        for path in (operation.parent, operation):
            if _linked(path):
                raise ManagementError('恢复目录无法安全写入，未删除', 409)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        prefix = item['instance_id'] + '-' + item['hash']
        backup = operation / (prefix + '.torrent')
        if item['type'] == 'qb':
            data = api.qexport(item['hash'])
            meta = torrent_metadata(data)
            if not set(meta['hashes']) & set(item['aliases']) or meta['size'] != item['size']:
                raise ManagementError('恢复种子身份或体积无法确认，未删除', 409)
            if _linked(backup):
                raise ManagementError('恢复文件无法安全写入，未删除', 409)
            with backup.open('wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(backup, 0o600)
        _save(operation / (prefix + '.json'), {'instance_id': item['instance_id'], 'hash': item['hash'], 'type': item['type'],
              'aliases': item['aliases'], 'size': item['size'], 'descriptor': item['descriptor'], 'snapshot': item['snapshot'],
              'original': item['original'], 'at': self.fleet.clock(), 'category': record['raw'].get('category', ''),
              'labels': labels(record), 'magnet': ('magnet:?xt=urn:btih:' + item['hash']) if len(item['hash']) == 40
                                                else 'magnet:?xt=urn:btmh:1220' + item['hash']})
        path = self.directory / 'batch.json'
        batch = _read(path, {})
        seen = batch.get('seen_hashes', [])
        if not isinstance(seen, list) or any(not isinstance(h, str) or not HASH.fullmatch(h) for h in seen):
            raise ManagementError('永久排除资料无法确认，未删除', 409)
        batch['seen_hashes'] = sorted(set(seen) | set(item['aliases']))
        _save(path, batch)

    def _reconcile(self, item):
        try:
            task, _ = self._one(item)
            gone = self.storage.gone(item['snapshot'])
            if task is None and gone:
                item.update(phase='completed', reason='任务与原文件均已确认移除')
                # Only this current, successful read changes the inventory summary.
                self._plan(self.values, persist=True, rule_id=self.state['job']['rule_id'])
            elif task is None:
                item.update(phase='needs_review', reason='任务已移除，原文件残留或无法核验；未确认释放空间')
            elif self.fleet.clock() - item['intent_at'] >= self.fleet.settings().get('pause_timeout_seconds', 120):
                item.update(phase='needs_review', reason='删除回读未确认；停止该项，不重复发送删除')
        except Exception:
            if self.fleet.clock() - item['intent_at'] >= self.fleet.settings().get('pause_timeout_seconds', 120):
                item.update(phase='needs_review', reason='删除结果无法回读；停止该项，不重复发送删除')
        self._save()

    def recheck(self, body):
        """Read results again without issuing any native pause/resume/delete."""
        with self._guard():
            self._require_idle()
            job = self.state['job']
            if (not isinstance(body, dict) or set(body) != {'job_id'} or not job
                    or body['job_id'] != job['id'] or job['status'] != 'needs_review'):
                raise ManagementError('没有待核验的当前分类清理', 409)
            for item in job['items']:
                if item['phase'] != 'needs_review':
                    continue
                if item['intent_at'] is not None:
                    self._reconcile(item)
                else:
                    # A failed restoration can only be acknowledged after a
                    # current identity/file/status read proves the original state.
                    try:
                        task, api = self._one(item)
                        if (task is not None and item['original'] is not None and _safe_task(item['type'], task)
                                and _signature(item['type'], task, item['aliases']) == item['signature']
                                and _paused(item['type'], task) == item['original']['paused']
                                and (not item['original']['force'] or task.get('force_start') is True or task.get('state') == 'forcedUP')
                                and sorted(api.files_one(item['type'], item['hash']), key=lambda f: f['name']) == item['descriptor']['files']
                                and self.storage.recheck(item['snapshot'])):
                            item.update(phase=item['outcome'], reason='已只读核验暂停前状态，未发送删除')
                    except Exception:
                        pass
            self._finish()
            self.review_due = False
            return {'ok': True, 'job': self.public_job()}

    def _finish(self):
        job = self.state['job']
        if any(item['phase'] not in TERMINAL for item in job['items']):
            return
        status = ('needs_review' if any(i['phase'] == 'needs_review' for i in job['items']) else 'cancelled' if job['cancel_requested']
                  else 'failed' if any(i['phase'] == 'failed' for i in job['items']) else 'completed')
        job.update(status=status, finished_at=self.fleet.clock())
        state = self.state['rules'].get(job['rule_id'])
        if state:
            state['summary']['last_result'] = status
        self._save()
        if status == 'needs_review':
            self.pause(reset=False)
        self.fleet.state['automation_turn'] = 'refill'
        self.fleet._save()
        self.fleet._log('category_cleanup_finished', reason=status,
                        processed=sum(i['phase'] == 'completed' for i in job['items']),
                        errors=sum(i['phase'] in ('failed', 'needs_review') for i in job['items']))

    def step(self):
        with self._guard():
            if not self.busy() or self._busy_other():
                return False
            job = self.state['job']
            item = next((i for i in job['items'] if i['phase'] not in TERMINAL), None)
            if item is None:
                self._finish()
                return False
            if item['phase'] == 'waiting_result':
                self._reconcile(item)
            elif item['phase'] == 'restoring':
                self._restore(item)
            elif job['cancel_requested']:
                if item['phase'] == 'queued':
                    item.update(phase='cancelled', reason='已取消后续未执行项')
                    self._save()
                else:
                    self._abort(item, '已取消；正在恢复本次暂停前的状态', 'cancelled')
            else:
                try:
                    if item['phase'] == 'queued':
                        record, api, descriptor, snapshot = self._fresh_candidate(item)
                        raw = record['raw']
                        item.update(phase='preparing', descriptor=descriptor, snapshot=snapshot, signature=record['signature'],
                                    original={'paused': _paused(item['type'], raw),
                                              'force': item['type'] == 'qb' and (raw.get('force_start') is True or raw.get('state') == 'forcedUP')},
                                    started_at=self.fleet.clock())
                        self._save()  # Persist pause intent before the native request.
                        if not item['original']['paused']:
                            try:
                                api.stop_one(item['type'], item['hash'])
                            except Exception:
                                pass
                    elif item['phase'] in ('preparing', 'verified'):
                        task, api = self._one(item)
                        if task is None:
                            self._abort(item, '任务在删除前已变化，未发送删除')
                        elif not _paused(item['type'], task):
                            if self.fleet.clock() - item['started_at'] >= self.fleet.settings().get('pause_timeout_seconds', 120):
                                self._abort(item, '暂停未确认，未删除', 'failed')
                            return False
                        else:
                            record, api, descriptor, snapshot = self._fresh_candidate(item)
                            if item['phase'] == 'preparing':
                                self._backup(item, record, api)
                                item['phase'] = 'verified'
                                self._save()
                            else:
                                # The last reference/evidence read is current under all management locks.
                                if not self.storage.recheck(item['snapshot']):
                                    raise ManagementError('删除前文件身份已变化，未删除', 409)
                                item.update(phase='waiting_result', intent_at=self.fleet.clock())
                                self._save()  # Durable intent + exclusion precede the single native delete.
                                try:
                                    api.remove_data(item['type'], item['hash'])
                                except Exception:
                                    item['reason'] = '删除请求回执不明，先回读任务与文件'
                                    self._save()
                                self._reconcile(item)
                except Exception:
                    self._abort(item, '执行前资格、文件归属或恢复资料无法确认，未删除', 'skipped')
            if item['phase'] == 'needs_review':
                # Stop all remaining items immediately; no further destructive requests.
                for pending in job['items']:
                    if pending['phase'] == 'queued':
                        pending.update(phase='cancelled', reason='前一项结果待核验，后续未执行')
                self._save()
            self._finish()
            return self.busy() and item['phase'] != 'waiting_result'

    def due(self):
        now = self.fleet.clock()
        return [r for r in self.values['rules'] if (r['enabled'] or r['observe_only']) and
                now >= (self.state['rules'].get(r['id'], {}).get('summary', {}).get('next_check_at') or 0)]

    def wants_turn(self):
        return any(r['enabled'] and self.state['rules'].get(r['id'], {}).get('summary', {}).get('budget', 0) > 0
                   and self.state['rules'].get(r['id'], {}).get('summary', {}).get('mature', 0) > 0 for r in self.due())

    def defer(self):
        now, changed = self.fleet.clock(), False
        for rule in self.due():
            state = self.state['rules'].get(rule['id'])
            if state is None:
                report = _summary(rule, self.fleet.settings().get('target', 1100))
                state = {'version': rule_version(rule), 'active': False, 'observations': {}, 'summary': report, 'scan_cursor': 0}
                self.state['rules'][rule['id']] = state
            last = state['summary']['last_checked_at']
            if last is None or now - last > rule['check_minutes'] * 180:
                state['observations'] = {}
            state['summary'].update(status='busy', next_check_at=now + rule['retry_seconds'])
            changed = True
        if changed:
            self._save()

    def tick(self, *, allow_execute=True):
        if self.review_due and not self._busy_other():
            self.recheck({'job_id': self.state['job']['id']})
            return False
        if self.busy():
            return self.step()
        due = self.due()
        if not due:
            return False
        if self._busy_other():
            self.defer()
            return False
        with self._guard():
            for rule in due:
                if rule['enabled'] and allow_execute:
                    result = self._run_locked({'revision': self._revision(), 'rule_id': rule['id'], 'confirm': 'RUN_DELETE_TASKS_AND_DATA'})
                    if result['job'] is not None:
                        return True
                else:
                    self._plan(self.values, persist=True, files=True, rule_id=rule['id'])
        return False

    def interrupt(self):
        changed = bool(self.state['rules'])
        for state in self.state['rules'].values():
            state['observations'] = {}
            state['active'] = False
            state['summary'].update(active=False, inventory=None, budget=0, status='unknown', **dict.fromkeys(COUNTS, 0))
        self.annotations = {}
        if changed:
            self._save()

    def task_annotation(self, row):
        record = {'instance_id': row['instance_id'], 'type': row['client'], 'raw': row['_qb'] or row['_tr']}
        rules = [r for r in self.values['rules'] if row['pts'] and scope_match(r, record)]
        ids = [r['id'] for r in rules]
        entries = [i for i in self.annotations.get(row['id'], []) if i['rule_id'] in ids]
        priorities = {'conflict': 0, 'shared': 1, 'unknown': 2, 'protected': 3, 'waiting': 4, 'ready': 5, 'valid': 6}
        entry = min(entries, key=lambda i: priorities.get(i['status'], 9)) if entries else None
        status, reason = (entry['status'], entry['reason']) if entry else ('unknown', REASONS['unknown']) if ids else ('', '')
        if ids and (row['stale'] or not _safe_task(row['client'], record['raw'])):
            status, reason = 'protected', REASONS['protected']
        if status == 'ready':
            rule = next(r for r in rules if r['id'] == entry['rule_id'])
            last = self.state['rules'].get(rule['id'], {}).get('summary', {}).get('last_checked_at')
            if last is None or not 0 <= self.fleet.clock() - last <= rule['check_minutes'] * 180:
                status, reason = 'unknown', REASONS['unknown']
        return {'cleanup_rules': ids, 'cleanup_status': status, 'cleanup_reason': reason}
