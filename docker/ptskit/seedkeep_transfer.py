"""Saved MP-style transfer rules; fresh previews, serial scheduling and private completion history."""
import copy
import datetime as dt
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
import seedkeep_configuration as configuration
import seedkeep_pull as pull
from seedkeep_downloaders import ManagementError, inventory_rows

DEFAULT_RULE = {
    'enabled': False, 'notify': True, 'cron': '*/5 * * * *',
    'source_instance_id': '', 'target_instance_id': '', 'include_untagged': True,
    'include_categories': [], 'include_tags': [], 'exclude_tags': ['hr', 'HR'],
    'excluded_dirs': [], 'path_mappings': configuration.DEFAULT_MAPPINGS,
    'target_labels': ['转移做种'], 'start_after_verify': True,
    'delete_source': True, 'delete_duplicate_source': False,
}
REASONS = {
    'ready': '符合规则，完整下载后快速接管保种',
    'incomplete': '未列入 qB 已完成', 'excluded_dir': '目录已排除',
    'category': '来源分类不在指定范围', 'untagged': '未允许无标签任务',
    'excluded_tag': '含不转移标签', 'include_tag': '未包含全部指定标签',
    'path': '来源目录不在规则路径映射中', 'not_ready': '完整下载、任务状态或接管条件未满足',
    'target_exists': '目的已有同种，规则设置为跳过',
    'target_conflict': '目的同种目录或总体积不一致',
    'already_processed': '本来源与目的已成功执行过此任务',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def cron_fields(expression):
    """Five-field APScheduler-style crontab subset, with Monday=0 and named weekdays."""
    if not isinstance(expression, str) or len(expression) > 200 or len(expression.split()) != 5:
        raise ManagementError('执行周期应为五段 cron，例如 0 */6 * * *')
    result = []
    aliases = [{}, {}, {}, {name: n + 1 for n, name in enumerate(
        ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))},
        {name: n for n, name in enumerate(('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'))}]
    for part, (low, high), names in zip(expression.lower().split(), ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6)), aliases):
        selected = set()
        def number(text):
            if text in names:
                return names[text]
            if not re.fullmatch(r'[0-9]{1,2}', text):
                raise ManagementError('cron 只支持数字、名称、*、范围、逗号和 /步长')
            return int(text)
        for entry in part.split(','):
            chunks = entry.split('/')
            if len(chunks) > 2 or not chunks[0]:
                raise ManagementError('cron 范围或步长无效')
            if len(chunks) == 2 and not re.fullmatch(r'[0-9]{1,2}', chunks[1]):
                raise ManagementError('cron 步长应为正整数')
            step = int(chunks[1]) if len(chunks) == 2 else 1
            if not 1 <= step <= high - low + 1:
                raise ManagementError('cron 步长超出范围')
            if chunks[0] == '*':
                start, end = low, high
            elif '-' in chunks[0]:
                limits = chunks[0].split('-')
                if len(limits) != 2:
                    raise ManagementError('cron 范围无效')
                start, end = (number(value) for value in limits)
            else:
                start = number(chunks[0])
                end = high if len(chunks) == 2 else start
            if not low <= start <= end <= high:
                raise ManagementError('cron 数值超出允许范围；星期一为 0、星期日为 6')
            selected.update(range(start, end + 1, step))
        if not selected:
            raise ManagementError('cron 字段不能为空')
        result.append(sorted(selected))
    return result


def next_schedule(expression, now):
    minute, hour, day, month, weekday = cron_fields(expression)
    date = dt.datetime.fromtimestamp(now).date()
    # Gregorian date/weekday combinations repeat within 400 years, including rare leap weekdays.
    for offset in range(146097 + 1):
        current = date + dt.timedelta(days=offset)
        if current.day not in day or current.month not in month or current.weekday() not in weekday:
            continue
        for h in hour:
            for m in minute:
                wanted = dt.datetime.combine(current, dt.time(h, m))
                candidate = wanted.timestamp()
                if candidate > now and dt.datetime.fromtimestamp(candidate) == wanted:
                    return candidate
    raise ManagementError('cron 日期组合没有可执行时间，请检查月份、日期和星期')


def text_list(value):
    if not isinstance(value, list) or len(value) > 50 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 100
            or any(ord(c) < 32 for c in item) or ',' in item for item in value):
        raise ManagementError('分类或标签应为最多 50 个非空文本，每项最多 100 字')
    return list(dict.fromkeys(item.strip() for item in value))


def validate(values):
    if not isinstance(values, dict) or set(values) - {'max_per_run'} != set(DEFAULT_RULE):
        raise ManagementError('转种规则字段无效')
    result = copy.deepcopy(values)
    if 'max_per_run' in result:
        if type(result['max_per_run']) is not int or not 1 <= result['max_per_run'] <= 50:
            raise ManagementError('旧版转种数量字段无效')
        result.pop('max_per_run')
    for key in ('enabled', 'notify', 'include_untagged', 'start_after_verify', 'delete_source', 'delete_duplicate_source'):
        if type(result[key]) is not bool:
            raise ManagementError('转种规则开关应为布尔值')
    for key in ('source_instance_id', 'target_instance_id'):
        if not isinstance(result[key], str) or len(result[key]) > 100:
            raise ManagementError('来源或目的实例标识无效')
    cron_fields(result['cron'])
    result['cron'] = ' '.join(result['cron'].split())
    for key in ('include_categories', 'include_tags', 'exclude_tags', 'target_labels'):
        result[key] = text_list(result[key])
    try:
        result['path_mappings'] = configuration.mappings(result['path_mappings'])
        paths = result['excluded_dirs']
        if not isinstance(paths, list) or len(paths) > 50:
            raise configuration.ConfigurationError('不转移目录最多 50 条')
        result['excluded_dirs'] = list(dict.fromkeys(configuration.directory(path) for path in paths))
        if '/' in result['excluded_dirs']:
            raise configuration.ConfigurationError('不转移目录不能使用根目录 /')
    except configuration.ConfigurationError as error:
        raise ManagementError(str(error)) from None
    return result


def filter_reason(torrent, values):
    try:
        path = configuration.directory(torrent.get('save_path'))
    except configuration.ConfigurationError:
        return 'path'
    if any(path == excluded or path.startswith(excluded + '/') for excluded in values['excluded_dirs']):
        return 'excluded_dir'
    if values['include_categories'] and str(torrent.get('category', '')) not in values['include_categories']:
        return 'category'
    tags = {tag.strip() for tag in str(torrent.get('tags', '')).split(',') if tag.strip()}
    if not tags:
        return None if values['include_untagged'] else 'untagged'
    if tags.intersection(values['exclude_tags']):
        return 'excluded_tag'
    if not set(values['include_tags']).issubset(tags):
        return 'include_tag'
    return None


def state_document(value):
    fields = {'next_run_at', 'last_run_at', 'last_result', 'history', 'observed_job_ids'}
    def timestamp(number):
        return number is None or (type(number) in (int, float) and math.isfinite(number) and number >= 0)
    if (not isinstance(value, dict) or set(value) != fields
            or not all(timestamp(value[key]) for key in ('next_run_at', 'last_run_at'))
            or not isinstance(value['history'], dict) or not isinstance(value['observed_job_ids'], list)
            or any(not isinstance(item, str) or not re.fullmatch(r'[0-9a-f]{32}', item) for item in value['observed_job_ids'])):
        raise ManagementError('转种规则恢复资料无效，请检查备份；未清空历史', 409)
    for key, record in value['history'].items():
        if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key)
                or not isinstance(record, dict) or set(record) != {'finished_at', 'identity_keys'}
                or not timestamp(record['finished_at']) or not isinstance(record['identity_keys'], list)
                or any(not isinstance(k, str) or not re.fullmatch(r'[0-9a-f]{64}', k) for k in record['identity_keys'])):
            raise ManagementError('转种历史资料无效，请检查备份；未清空历史', 409)
    result = value['last_result']
    if result is not None:
        required = {'job_id', 'status', 'finished_at', 'completed', 'failed', 'skipped', 'cancelled', 'source_removed', 'source_kept', 'notify'}
        if (not isinstance(result, dict) or set(result) - (required | {'message'}) or not required.issubset(result)
                or result['status'] not in ('completed', 'failed', 'cancelled', 'interrupted', 'deferred')
                or (result['job_id'] is not None and not re.fullmatch(r'[0-9a-f]{32}', str(result['job_id'])))
                or not timestamp(result['finished_at']) or type(result['notify']) is not bool
                or any(type(result[k]) is not int or result[k] < 0 for k in ('completed', 'failed', 'skipped', 'cancelled', 'source_removed', 'source_kept'))):
            raise ManagementError('转种结果恢复资料无效，请检查备份', 409)
        if 'message' in result:
            result['message'] = '本次转种暂未开始，请检查实例连接和当前作业'
    return value


class Rules:
    def __init__(self, fleet):
        self.fleet = fleet
        self.path = fleet.directory / 'transfer_rules.json'
        self.state_path = fleet.directory / 'transfer_rules_state.json'
        defaults = copy.deepcopy(DEFAULT_RULE)
        rows = fleet.registry.items()
        primary = fleet.registry.primary()
        defaults['source_instance_id'] = (primary or next((r for r in rows if r['type'] == 'qb' and r['enabled']), {})).get('id', '')
        defaults['target_instance_id'] = next((r['id'] for r in rows if r['type'] == 'tr' and r['enabled']), '')
        defaults['path_mappings'] = copy.deepcopy(fleet.settings().get('transfer_path_mappings', configuration.DEFAULT_MAPPINGS))
        self.values = validate(json.loads(self.path.read_text(encoding='utf-8-sig')) if self.path.exists() else defaults)
        self.state = state_document(json.loads(self.state_path.read_text(encoding='utf-8-sig')) if self.state_path.exists() else {
            'next_run_at': None, 'last_run_at': None, 'last_result': None, 'history': {}, 'observed_job_ids': []})
        self.last_attempt = None
        if self.values['enabled'] and self.state.get('next_run_at') is None:
            self.state['next_run_at'] = next_schedule(self.values['cron'], fleet.clock())

    def revision(self):
        return digest([self.values, self.fleet.registry.public()['revision']])

    def require_revision(self, revision):
        if (not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)
                or not hmac.compare_digest(revision, self.revision())):
            raise ManagementError('转种规则或实例已被其他页面修改，请刷新后再保存或执行；草稿保留', 409)

    def public(self):
        with self.fleet.lock:
            self.observe()
            return {'values': copy.deepcopy(self.values), 'revision': self.revision(), 'saved': self.path.exists(),
                    'instances_revision': self.fleet.registry.public()['revision'],
                    'timezone': os.environ.get('TZ') or time.tzname[0], 'busy': self.fleet.busy(),
                    'runtime': {**{key: copy.deepcopy(self.state.get(key)) for key in ('next_run_at', 'last_run_at', 'last_result')},
                                'completed_count': len(self.state.get('history', {}))}}

    def pair(self, values):
        return self.fleet.registry.pair(values['source_instance_id'], values['target_instance_id'])

    def update(self, body):
        if not isinstance(body, dict) or set(body) - {'revision', 'values', 'confirm'}:
            raise ManagementError('转种规则保存请求无效')
        with self.fleet.lock:
            self.require_revision(body.get('revision'))
            if self.fleet.busy():
                raise ManagementError('已有管理作业，请完成后再保存转种规则', 409)
            values = validate(body.get('values'))
            if values['source_instance_id'] or values['target_instance_id'] or values['enabled']:
                self.pair(values)
            due = next_schedule(values['cron'], self.fleet.clock())
            if values['enabled']:
                if body.get('confirm') != 'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA':
                    raise ManagementError('请确认启用定时 qB→TR 转种；文件始终保留')
                if not values['path_mappings']:
                    raise ManagementError('启用定时转种前请填写路径映射')
            with self.fleet.guard():
                pull.save_json(self.path, values)
                self.values = values
                self.state['next_run_at'] = due if values['enabled'] else None
                pull.save_json(self.state_path, self.state)
            self.fleet._log('transfer_rules_saved')
            return {'ok': True, **self.public()}

    def pause(self):
        with self.fleet.lock:
            if self.values['enabled']:
                self.values['enabled'] = False
                self.state['next_run_at'] = None
                pull.save_json(self.path, self.values)
                pull.save_json(self.state_path, self.state)

    @staticmethod
    def history_key(source, target, value):
        return digest([source, target, value])

    def plan(self, values):
        source, settings = self.pair(values)
        settings['transfer_path_mappings'] = copy.deepcopy(values['path_mappings'])
        try:
            qb = self.fleet.registry.api(values['source_instance_id']).inventory_one()
            tr = self.fleet.registry.api(values['target_instance_id']).inventory_one()
            rows = inventory_rows(qb, tr, source, {}, None, self.fleet.clock(), settings)
        except Exception:
            raise ManagementError('来源或目的任务读取失败，请检查两个实例；未执行转种', 502) from None
        counts = dict.fromkeys(('source_total', 'completed', 'matched', 'eligible', 'selected',
                                'target_existing', 'already_processed', 'skipped'), 0)
        items, ready = [], []
        known = {identity for key, record in self.state['history'].items() for identity in (key, *record['identity_keys'])}
        for row in rows:
            q, t = row['_qb'], row['_tr']
            if not q:
                continue
            value = q['hash'].lower()
            complete = row['client_states']['qb']['completed']
            counts['source_total'] += 1
            counts['completed'] += int(complete)
            reason = 'incomplete' if not complete else filter_reason(q, values)
            if reason is None:
                counts['matched'] += 1
                counts['target_existing'] += int(bool(t))
                mapped = configuration.mapped_path(q.get('save_path'), settings)
                if not mapped:
                    reason = 'path'
                elif any(self.history_key(values['source_instance_id'], values['target_instance_id'], alias) in known for alias in row['_aliases']):
                    reason = 'already_processed'
                    counts['already_processed'] += 1
                elif t and not values['delete_duplicate_source']:
                    reason = 'target_exists'
                elif t and (t.get('downloadDir', '').rstrip('/') != mapped[1] or t.get('totalSize') != q.get('total_size')):
                    reason = 'target_conflict'
                elif not row['can_transfer']:
                    reason = 'not_ready'
            eligible = reason is None
            ref = {'instance_id': values['source_instance_id'], 'hash': value}
            item = {**ref, 'id': ref['instance_id'] + ':' + value, 'name': self.fleet._safe_text(row['name']),
                    'state_text': row['client_states']['qb']['state_text'], 'eligible': eligible,
                    'target_exists': bool(t), 'reason_code': reason or 'ready', 'reason': REASONS[reason or 'ready']}
            items.append(item)
            if eligible:
                ready.append(ref)
        items.sort(key=lambda item: (not item['eligible'], item['name'].lower(), item['id']))
        ready.sort(key=lambda ref: ref['hash'])
        counts['eligible'] = len(ready)
        counts['selected'] = len(ready)
        counts['skipped'] = len(items) - len(ready)
        return {'counts': counts, 'items': items[:100], 'limit': 100, 'truncated': len(items) > 100,
                'checked_at': self.fleet.clock()}, ready, {item['id']: item for item in items}

    def preview(self, body):
        if not isinstance(body, dict) or set(body) != {'values'}:
            raise ManagementError('转种预览请求格式无效')
        with self.fleet.lock:
            return self.plan(validate(body['values']))[0]

    def run(self, body, *, scheduled=False):
        if not isinstance(body, dict) or set(body) - {'revision', 'confirm', 'tasks'} or body.get('confirm') != 'RUN_QB_TO_TR_RULE_KEEP_DATA':
            raise ManagementError('请确认按保存规则执行转种并保留文件')
        with self.fleet.lock:
            self.require_revision(body.get('revision'))
            if not self.path.exists():
                raise ManagementError('请先保存转种规则，再执行一次', 409)
            if self.fleet.busy():
                raise ManagementError('已有管理作业，请完成或取消后再转种', 409)
            if not self.values['path_mappings']:
                raise ManagementError('执行转种前请填写规则路径映射')
            self.observe()
            report, ready, all_items = self.plan(self.values)
            if 'tasks' in body:
                tasks = self.fleet._tasks(body)
                for ref in tasks:
                    item = all_items.get(ref['instance_id'] + ':' + ref['hash'])
                    if not item or not item['eligible']:
                        raise ManagementError('所选任务未全部满足已保存的来源、筛选和转种资格；未执行', 409)
                ready = tasks
                report['counts']['selected'] = len(tasks)
            if not ready:
                with self.fleet.guard():
                    self.state.update(last_run_at=self.fleet.clock(), last_result={
                        'job_id': None, 'status': 'completed', 'finished_at': self.fleet.clock(), 'completed': 0,
                        'failed': 0, 'skipped': report['counts']['skipped'], 'cancelled': 0,
                        'source_removed': 0, 'source_kept': 0, 'notify': self.values['notify']})
                    if scheduled:
                        self.state['next_run_at'] = next_schedule(self.values['cron'], self.fleet.clock())
                    pull.save_json(self.state_path, self.state)
                return {'ok': True, 'job': None, 'counts': report['counts'], 'message': '没有符合规则且可转种的任务'}
            manager = self.fleet._manager(self.values['source_instance_id'], self.values['target_instance_id'])
            options = copy.deepcopy(self.values)
            manager.begin('transfer', {'hashes': [ref['hash'] for ref in ready], 'confirm': 'MOVE_QB_TO_TR_KEEP_DATA'},
                          transfer_options=options)
            manager.state['job']['rule_counts'] = report['counts']
            manager.save()
            self.state['last_run_at'] = self.fleet.clock()
            if scheduled:
                self.state['next_run_at'] = next_schedule(self.values['cron'], self.fleet.clock())
            pull.save_json(self.state_path, self.state)
            self.fleet.cache_at = None
            return {'ok': True, 'job': self.fleet.public_job(), 'counts': report['counts']}

    def observe(self):
        observed = self.state.setdefault('observed_job_ids', [])
        changed = False
        for manager in self.fleet._all_managers():
            job = manager.state.get('job') or {}
            if not job.get('rule_run') or job.get('status') in ('running', 'waiting') or job.get('id') in observed:
                continue
            items = job['items']
            for item in items:
                if item['phase'] == 'completed':
                    identities = (item.get('source') or {}).get('hashes', [item['hash']])
                    key = self.history_key(job['source_instance_id'], job['target_instance_id'], item['hash'])
                    self.state.setdefault('history', {})[key] = {
                        'finished_at': job.get('finished_at'), 'identity_keys': [
                            self.history_key(job['source_instance_id'], job['target_instance_id'], value) for value in identities]}
            self.state['last_result'] = {
                'job_id': job['id'], 'status': job['status'], 'finished_at': job.get('finished_at'),
                **{key: sum(item['phase'] == key for item in items) for key in ('completed', 'failed', 'skipped', 'cancelled')},
                'source_removed': sum(bool(item.get('source_removed')) for item in items),
                'source_kept': sum(bool(item.get('source_kept')) and item['phase'] == 'completed' for item in items),
                'notify': job['transfer_options']['notify']}
            observed.append(job['id'])
            changed = True
        if changed:
            self.state['observed_job_ids'] = observed[-200:]
            pull.save_json(self.state_path, self.state)

    def tick(self):
        self.observe()
        if not self.values['enabled'] or self.fleet.busy():
            return
        now = self.fleet.clock()
        due = self.state.get('next_run_at')
        if due is None:
            self.state['next_run_at'] = next_schedule(self.values['cron'], now)
            pull.save_json(self.state_path, self.state)
            return
        if now < due or (self.last_attempt is not None and now - self.last_attempt < 60):
            return
        self.last_attempt = now
        try:
            self.run({'revision': self.revision(), 'confirm': 'RUN_QB_TO_TR_RULE_KEEP_DATA'}, scheduled=True)
        except Exception as error:
            if not isinstance(error, ManagementError) and getattr(error, 'status', None) != 409:
                raise
            # A failed read/locked runner keeps the occurrence pending, rather than counting an empty run.
            self.state['last_result'] = {'job_id': None, 'status': 'deferred', 'finished_at': now,
                                        'completed': 0, 'failed': 0, 'skipped': 0, 'cancelled': 0,
                                        'source_removed': 0, 'source_kept': 0, 'notify': self.values['notify'],
                                        'message': '本次转种暂未开始，请检查实例连接和当前作业'}
            pull.save_json(self.state_path, self.state)
            self.fleet._log('transfer_rule_run_deferred', errors=1)
