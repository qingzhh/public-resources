"""Validated runtime defaults and private connection overrides for the web client."""
import copy
import http.cookiejar
import json
from pathlib import Path, PurePosixPath
import math
import urllib.parse
import urllib.request
import seedkeep_tagging as tagging


class ConfigurationError(Exception):
    pass


# Default values preserve the existing deployed behavior.
NUMBERS = {
    'max_run_seconds': (1800, 60, 86400, True),
    'pending_grace_seconds': (300, 30, 3600, True),
    'download_concurrency': (4, 1, 32, True),
    'request_interval_seconds': (1.0, .1, 60, False),
    'download_retries': (3, 1, 10, True),
    'download_retry_seconds': (2.0, .5, 60, False),
    'candidate_refresh_seconds': (180, 30, 3600, True),
    'candidate_limit': (1000, 1, 1000, True),
    'pts_cache_seconds': (60, 10, 3600, True),
    'page_refresh_seconds': (15, 5, 300, True),
    'task_page_size': (50, 10, 200, True),
    'log_limit': (100, 20, 1000, True),
    'management_cache_seconds': (30, 5, 300, True),
    'monitor_interval_seconds': (60, 15, 600, True),
    'tracker_fresh_seconds': (7200, 60, 86400, True),
    'delete_max_per_job': (50, 1, 200, True),
    'verify_timeout_seconds': (21600, 60, 172800, True),
    'pause_timeout_seconds': (120, 10, 600, True),
    'site_timeout_seconds': (15, 5, 120, True),
    'download_timeout_seconds': (45, 5, 300, True),
    'qb_timeout_seconds': (45, 5, 120, True),
    'tr_timeout_seconds': (20, 5, 120, True),
    'refill_trigger': (1100, 0, 99999, True),
    'refill_floor': (1000, 0, 99999, True),
    'refill_check_minutes': (5, 1, 1440, True),
    'refill_max_inflight': (500, 1, 10000, True),
    'refill_retry_seconds': (60, 10, 3600, True),
    'refill_site_max_age_minutes': (120, 1, 1440, True),
    'refill_reservation_hours': (72, 1, 720, True),
}
# Read and validate older settings, but do not expose or use their retired transfer cap.
LEGACY_NUMBERS = {'transfer_max_per_job': (20, 1, 100, True)}
DEFAULT_MAPPINGS = [{'qb': root, 'tr': root} for root in ('/downloads', '/media', '/downloads2')]
DEFAULTS = {**{key: rule[0] for key, rule in NUMBERS.items()}, 'auto_start': True,
            'seeders_limit_mode': 'site', 'manual_seeders_max': 10, 'transfer_path_mappings': DEFAULT_MAPPINGS,
            'refill_count_basis': 'managed_tasks', 'managed_tag': tagging.DEFAULT_TAG}
CONNECTION_FIELDS = ('api_base', 'site_use_proxy', 'site_proxy_url', 'qb_url', 'qb_username',
                     'bt_use_proxy', 'bt_proxy_url', 'tr_url', 'tr_username', 'download_path', 'category', 'tag', 'keep_torrent')
SECRET_FIELDS = ('token', 'qb_password', 'tr_password')


def directory(value, *, empty=False):
    if empty and value == '':
        return value
    if (not isinstance(value, str) or len(value) > 2048 or not value.startswith('/')
            or any(ord(c) < 32 for c in value) or '..' in value.split('/') or value.startswith('//')):
        raise ConfigurationError('目录必须是下载器容器内的绝对路径，不能包含 .. 或控制字符')
    return str(PurePosixPath(value))


def mappings(values):
    if not isinstance(values, list) or len(values) > 20:
        raise ConfigurationError('转种目录映射最多 20 条')
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {'qb', 'tr'}:
            raise ConfigurationError('每条转种映射应包含 qB 和 TR 目录')
        row = {key: directory(value[key]) for key in ('qb', 'tr')}
        if row['qb'] == '/' or row['tr'] == '/' or row['qb'] in seen:
            raise ConfigurationError('转种映射不能使用根目录 /，也不能重复 qB 来源目录')
        seen.add(row['qb'])
        result.append(row)
    return result


def validate_runtime(settings, *, strict=True):
    value = dict(settings)
    target = value.get('target', 1200)
    compatible_trigger = min(1100, max(0, target - 1)) if type(target) is int else 1100
    value.setdefault('refill_trigger', compatible_trigger)
    value.setdefault('refill_floor', min(1000, compatible_trigger))
    for key, default in DEFAULTS.items():
        value.setdefault(key, copy.deepcopy(default))
    for key, (_, minimum, maximum, integer) in {**NUMBERS, **LEGACY_NUMBERS}.items():
        if key not in value:
            continue
        number = value[key]
        # Native batch callers may retain their established small test budgets.
        if not strict and key in ('max_run_seconds', 'pending_grace_seconds'):
            continue
        if (type(number) not in ((int,) if integer else (int, float))
                or not minimum <= number <= maximum or not math.isfinite(number)):
            raise ConfigurationError('运行参数超出允许范围：' + key)
    if value['refill_count_basis'] not in ('site_effective', 'managed_tasks'):
        raise ConfigurationError('补量计数口径应为站端有效保种或本地标签保种数量')
    if value['refill_count_basis'] == 'site_effective' and (
            type(target) is not int or not 0 <= value['refill_floor'] <= value['refill_trigger'] < target):
        raise ConfigurationError('站端有效模式须满足：警戒线 ≤ 补量触发线 < 维持目标')
    if type(value['auto_start']) is not bool:
        raise ConfigurationError('添加后自动开始必须是开关值')
    if value['seeders_limit_mode'] not in ('site', 'manual'):
        raise ConfigurationError('人数上限来源应为站点或手动设置')
    if type(value['manual_seeders_max']) is not int or not 0 <= value['manual_seeders_max'] <= 100000:
        raise ConfigurationError('手动人数上限应为 0–100000 的整数')
    try:
        value['managed_tag'] = tagging.tag_value(value['managed_tag'])
    except tagging.TaggingError as error:
        raise ConfigurationError(str(error)) from None
    value['transfer_path_mappings'] = mappings(value['transfer_path_mappings'])
    return value


def url(value, *, empty=False):
    if empty and value == '':
        return value
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ConfigurationError('连接地址格式无效')
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment):
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ConfigurationError('地址应使用 http:// 或 https://，账号密码请填写在独立字段') from None
    return value.rstrip('/')


def qb_base(source):
    if source.get('qb_url'):
        return source['qb_url'].rstrip('/')
    host = source['host'].removeprefix('http://').removeprefix('https://').rstrip('/')
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    return ('https' if source.get('use_https') else 'http') + '://' + host + ':' + str(source['port'])


def resolve_source(base, settings):
    value = dict(base or {})
    overrides = settings.get('connections', {})
    direct = ('api_base', 'token', 'site_use_proxy', 'site_proxy_url', 'qb_url', 'bt_use_proxy',
              'bt_proxy_url', 'download_path', 'category', 'tag', 'keep_torrent')
    value.update({key: overrides[key] for key in direct if key in overrides})
    for original, key in (('username', 'qb_username'), ('password', 'qb_password')):
        if key in overrides:
            value[original] = overrides[key]
    return value


def resolve_settings(settings):
    value = dict(settings)
    if 'tr_url' in settings.get('connections', {}):
        value['tr_url'] = settings['connections']['tr_url']
    return value


def tr_credentials(settings, fallback=None):
    overrides = settings.get('connections', {})
    if 'tr_username' in overrides and 'tr_password' in overrides:
        return {'username': overrides['tr_username'], 'password': overrides['tr_password']}
    try:
        if settings.get('tr_credentials_file'):
            return json.loads(Path(settings['tr_credentials_file']).read_text(encoding='utf-8-sig'))
        if fallback is not None:
            return {'username': fallback.get('username', ''), 'password': fallback.get('password', '')}
    except Exception:
        pass
    raise ConfigurationError('Transmission 账号配置无法读取')


def connection_values(source, settings, credentials):
    return {'api_base': source.get('api_base', ''), 'site_use_proxy': bool(source.get('site_use_proxy')),
            'site_proxy_url': source.get('site_proxy_url', ''), 'qb_url': qb_base(source),
            'qb_username': source.get('username', ''), 'bt_use_proxy': bool(source.get('bt_use_proxy')),
            'bt_proxy_url': source.get('bt_proxy_url', ''), 'tr_url': resolve_settings(settings).get('tr_url', ''),
            'tr_username': credentials.get('username', ''), 'download_path': source.get('download_path', ''),
            'category': source.get('category', ''), 'tag': source.get('tag', ''), 'keep_torrent': bool(source.get('keep_torrent'))}


def prepare_connections(values, source, settings, credentials):
    if not isinstance(values, dict) or set(values) - set(CONNECTION_FIELDS + SECRET_FIELDS + ('clear_qb_password',)):
        raise ConfigurationError('连接配置包含不支持的字段')
    current = connection_values(source, settings, credentials)
    secret = {'token': source.get('token', ''), 'qb_password': source.get('password', ''),
              'tr_password': credentials.get('password', '')}
    candidate = {**current, **secret}
    for key, item in values.items():
        if key in ('site_use_proxy', 'bt_use_proxy', 'keep_torrent', 'clear_qb_password'):
            if type(item) is not bool:
                raise ConfigurationError('代理与清空密码选项必须是开关值')
            if key != 'clear_qb_password':
                candidate[key] = item
        elif key in SECRET_FIELDS:
            if not isinstance(item, str) or len(item) > 8192 or any(ord(c) < 32 for c in item):
                raise ConfigurationError('密码或 Token 格式无效')
            if item:
                candidate[key] = item
        else:
            if not isinstance(item, str) or len(item) > 2048 or any(ord(c) < 32 for c in item):
                raise ConfigurationError('连接参数格式无效')
            candidate[key] = item.strip()
    if values.get('clear_qb_password'):
        if values.get('qb_password'):
            raise ConfigurationError('不能同时填写新 qB 密码和勾选清空密码')
        candidate['qb_password'] = ''
    for key in ('api_base', 'qb_url', 'tr_url'):
        candidate[key] = url(candidate[key])
    for key in ('site_proxy_url', 'bt_proxy_url'):
        candidate[key] = url(candidate[key], empty=True)
    candidate['download_path'] = directory(candidate['download_path'], empty=True)
    if not candidate['tr_username'] or not candidate['tr_password']:
        raise ConfigurationError('Transmission 账号和密码不能为空，网页登录沿用此账号')
    if len(candidate['category']) > 100 or len(candidate['tag']) > 500:
        raise ConfigurationError('分类或标签过长')
    return candidate


def opener(source, scope='site', *, cookies=False):
    enabled = source.get('site_use_proxy' if scope == 'site' else 'bt_use_proxy', False)
    proxy = source.get('site_proxy_url' if scope == 'site' else 'bt_proxy_url', '')
    handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if enabled and proxy else urllib.request.ProxyHandler() if enabled else urllib.request.ProxyHandler({})
    handlers = [handler]
    if cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return urllib.request.build_opener(*handlers)


def mapped_path(path, settings=None):
    try:
        normalized = directory(path)
    except ConfigurationError:
        return None
    options = DEFAULT_MAPPINGS if settings is None else settings.get('transfer_path_mappings', DEFAULT_MAPPINGS)
    for row in sorted(options, key=lambda entry: len(entry['qb']), reverse=True):
        root = row['qb']
        if normalized == root or normalized.startswith(root + '/'):
            return normalized, row['tr'] + normalized[len(root):]
    return None
