"""Downloader APIs and safe, account-local seeding observations."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.cookiejar
import json
import math
from pathlib import Path, PurePosixPath
import re
import urllib.error
import urllib.parse
import urllib.request
import seedkeep_pull as pull
from torrent_meta import MAX_META_BYTES
import seedkeep_configuration as configuration

TR_FIELDS = ['id', 'hashString', 'name', 'totalSize', 'status', 'percentDone', 'downloadDir',
             'trackerStats', 'labels', 'rateUpload', 'rateDownload', 'error', 'leftUntilDone', 'recheckProgress', 'haveValid', 'haveUnchecked', 'doneDate', 'addedDate']
HASH = re.compile(r'^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$')
FRESH_SECONDS = 7200
# These three namespaces were verified to bind the same host directories in qB and TR.
TRANSFER_ROOTS = ('/downloads', '/media', '/downloads2')


class ManagementError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def hashes(values, maximum=50):
    if not isinstance(values, list) or not values or (maximum is not None and len(values) > maximum):
        raise ManagementError('请选择至少 1 个任务' if maximum is None else '请选择 1–' + str(maximum) + ' 个任务')
    if any(not isinstance(value, str) or not HASH.fullmatch(value) for value in values):
        raise ManagementError('任务哈希无效')
    return list(dict.fromkeys(value.lower() for value in values))


def count(value):
    return value if type(value) is int and value >= 0 else None


def completed_qb(torrent):
    """Only a fully downloaded source in a completed qB state can be handed over."""
    progress = torrent.get('progress')
    size, completed = count(torrent.get('total_size')), count(torrent.get('completed'))
    return (size is not None and size > 0 and completed is not None and completed >= size
            and type(progress) in (int, float) and math.isfinite(progress) and progress == 1
            and count(torrent.get('amount_left')) == 0
            and torrent.get('state') in ('uploading', 'stalledUP', 'forcedUP', 'stoppedUP', 'pausedUP', 'queuedUP'))


def pts_url(url, source):
    if not isinstance(url, str):
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or '').lower()
        domain = (urllib.parse.urlsplit(source.get('api_base', '')).hostname or '').lower().removeprefix('www.')
        return bool(domain and (host == domain or host.endswith('.' + domain)))
    except ValueError:
        return False


def transfer_path(path, settings=None):
    mapped = configuration.mapped_path(path, settings)
    return mapped[0] if mapped else None


class API:
    """Independent qB and TR connections; no Docker socket and no external URLs in errors."""
    def __init__(self, source, settings):
        source = configuration.resolve_source(source, settings)
        settings = configuration.resolve_settings(settings)
        self.source, self.settings = source, settings
        self.qb_url = configuration.qb_base(source) + '/api/v2/'
        self.qb = configuration.opener(source, 'bt', cookies=True)
        self.tr = configuration.opener(settings.get('tr_proxy_source', source), 'bt')
        self.logged_in = False
        self.tr_headers = None

    def qrequest(self, operation, fields=None, *, binary=False):
        if not self.logged_in and operation != 'auth/login':
            if self.source.get('username') or self.source.get('password'):
                answer = self.qrequest('auth/login', {'username': self.source.get('username', ''), 'password': self.source.get('password', '')}, binary=True)
                if answer.strip() != b'Ok.':
                    raise ManagementError('qBittorrent 认证失败', 502)
            self.logged_in = True
        body = urllib.parse.urlencode(fields).encode() if fields is not None else None
        headers = {'Referer': self.qb_url.split('/api/v2/')[0] + '/'}
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            return pull.request(self.qb, self.qb_url + operation, body, headers,
                                json_response=not binary, maximum=MAX_META_BYTES if binary else 16 * 1024 * 1024, timeout=self.settings.get('qb_timeout_seconds', 45))
        except pull.PullError:
            raise ManagementError('qBittorrent 请求失败，请检查连接和权限', 502) from None

    def qget(self, operation, fields=None, *, binary=False):
        if fields:
            operation += '?' + urllib.parse.urlencode(fields)
        return self.qrequest(operation, binary=binary)

    def qpost(self, operation, fields):
        return self.qrequest(operation, fields, binary=True)

    def rpc(self, method, arguments=None):
        if self.tr_headers is None:
            try:
                auth = configuration.tr_credentials(self.settings)
                encoded = base64.b64encode((auth['username'] + ':' + auth['password']).encode()).decode()
                self.tr_headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' + encoded}
            except Exception:
                raise ManagementError('Transmission 凭证无法读取', 502) from None
        body = json.dumps({'method': method, 'arguments': arguments or {}}).encode()
        for _ in range(3):
            try:
                with self.tr.open(urllib.request.Request(self.settings['tr_url'], data=body, headers=self.tr_headers), timeout=self.settings.get('tr_timeout_seconds', 20)) as response:
                    data = response.read(16 * 1024 * 1024 + 1)
                    if len(data) > 16 * 1024 * 1024:
                        raise ValueError()
                    value = json.loads(data)
            except urllib.error.HTTPError as error:
                session = error.headers.get('X-Transmission-Session-Id')
                status = error.code
                error.close()
                if status == 409 and session:
                    self.tr_headers['X-Transmission-Session-Id'] = session
                    continue
                raise ManagementError('Transmission 请求失败，请检查连接和权限', 502) from None
            except Exception:
                raise ManagementError('Transmission 连接失败', 502) from None
            if not isinstance(value, dict) or value.get('result') != 'success' or not isinstance(value.get('arguments'), dict):
                raise ManagementError('Transmission 未接受该操作', 502)
            return value['arguments']
        raise ManagementError('Transmission 会话校验失败', 502)

    def inventory_one(self, client):
        if client == 'qb':
            rows = self.qget('torrents/info')
        elif client == 'tr':
            rows = self.rpc('torrent-get', {'fields': TR_FIELDS}).get('torrents')
        else:
            raise ManagementError('下载器类型无效')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ManagementError('下载器任务响应无效', 502)
        for row in rows:
            value = row.get('hash' if client == 'qb' else 'hashString')
            size = row.get('total_size' if client == 'qb' else 'totalSize')
            if not isinstance(value, str) or not HASH.fullmatch(value) or type(size) is not int or size < 0:
                raise ManagementError('下载器任务身份或体积响应无效', 502)
        return rows

    def inventory(self):
        def qread():
            return self.qget('torrents/info')
        def tread():
            return self.rpc('torrent-get', {'fields': TR_FIELDS})['torrents']
        qb, tr, errors = [], [], {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {'qb': executor.submit(qread), 'tr': executor.submit(tread)}
            for name, future in futures.items():
                try:
                    value = future.result()
                    if not isinstance(value, list):
                        raise ValueError()
                    if name == 'qb':
                        qb = value
                    else:
                        tr = value
                except Exception:
                    errors[name] = '下载器读取失败，请检查连接'
        return qb, tr, errors

    def qtrackers(self, hash_value):
        hashes([hash_value])
        return self.qget('torrents/trackers', {'hash': hash_value})

    def qexport(self, hash_value):
        hashes([hash_value])
        return self.qget('torrents/export', {'hash': hash_value}, binary=True)

    def qstop(self, hash_value):
        hashes([hash_value])
        self.qpost('torrents/stop', {'hashes': hash_value})

    def qstart(self, hash_value):
        hashes([hash_value])
        self.qpost('torrents/start', {'hashes': hash_value})

    def qremove(self, hash_value):
        hashes([hash_value])
        self.qpost('torrents/delete', {'hashes': hash_value, 'deleteFiles': 'false'})

    def files_one(self, client, hash_value):
        """Read the exact torrent file list only when ownership verification needs it."""
        value = hashes([hash_value])[0]
        if client == 'qb':
            rows = self.qget('torrents/files', {'hash': value})
        elif client == 'tr':
            torrents = self.rpc('torrent-get', {'ids': [value], 'fields': ['hashString', 'files']}).get('torrents')
            if (not isinstance(torrents, list) or len(torrents) != 1 or not isinstance(torrents[0], dict)
                    or str(torrents[0].get('hashString', '')).lower() != value):
                raise ManagementError('文件清单任务身份无法确认', 502)
            rows = torrents[0].get('files')
        else:
            raise ManagementError('下载器类型无效')
        if (not isinstance(rows, list) or not rows or len(rows) > 20000
                or any(not isinstance(row, dict) or not isinstance(row.get('name'), str)
                       or not row['name'] or len(row['name']) > 4096
                       or type(row.get('size' if client == 'qb' else 'length')) is not int
                       or not 0 <= row['size' if client == 'qb' else 'length'] <= 2 ** 63 - 1 for row in rows)):
            raise ManagementError('文件清单未知或超出检查范围，未删除', 502)
        return [{'name': row['name'], 'size': row['size' if client == 'qb' else 'length']} for row in rows]

    def stop_one(self, client, hash_value):
        value = hashes([hash_value])[0]
        if client == 'qb':
            self.qstop(value)
        elif client == 'tr':
            self.rpc('torrent-stop', {'ids': [value]})
        else:
            raise ManagementError('下载器类型无效')

    def start_one(self, client, hash_value, *, force=False):
        value = hashes([hash_value])[0]
        if client == 'qb':
            self.qstart(value)
            if force:
                self.qpost('torrents/setForceStart', {'hashes': value, 'value': 'true'})
        elif client == 'tr':
            self.rpc('torrent-start', {'ids': [value]})
        else:
            raise ManagementError('下载器类型无效')

    def remove_data(self, client, hash_value):
        """Explicit task-and-data action, separate from every existing keep-data action."""
        value = hashes([hash_value])[0]
        if client == 'qb':
            self.qpost('torrents/delete', {'hashes': value, 'deleteFiles': 'true'})
        elif client == 'tr':
            self.rpc('torrent-remove', {'ids': [value], 'delete-local-data': True})
        else:
            raise ManagementError('下载器类型无效')

    def limits(self, only=None):
        if only not in (None, 'qb', 'tr'):
            raise ManagementError('下载器类型无效')
        result = {}
        for name in (('qb', 'tr') if only is None else (only,)):
            try:
                if name == 'qb':
                    native = self.qget('app/preferences')
                    alt = self.qget('transfer/speedLimitsMode', binary=True).strip() == b'1'
                    result[name] = {'connected': True, 'error': None,
                                    'upload_kib': max(0, native['up_limit']) / 1024,
                                    'download_kib': max(0, native['dl_limit']) / 1024,
                                    'alternative_enabled': alt,
                                    'alternative_upload_kib': max(0, native['alt_up_limit']) / 1024,
                                    'alternative_download_kib': max(0, native['alt_dl_limit']) / 1024,
                                    'schedule_enabled': bool(native.get('scheduler_enabled'))}
                else:
                    native = self.rpc('session-get')
                    unit = native.get('units', {}).get('speed-bytes', 1000)
                    if type(unit) is not int or unit not in (1000, 1024):
                        raise ValueError()
                    result[name] = {'connected': True, 'error': None,
                                    'upload_kib': native['speed-limit-up'] * unit / 1024 if native['speed-limit-up-enabled'] else 0,
                                    'download_kib': native['speed-limit-down'] * unit / 1024 if native['speed-limit-down-enabled'] else 0,
                                    'alternative_enabled': native['alt-speed-enabled'],
                                    'alternative_upload_kib': native['alt-speed-up'] * unit / 1024,
                                    'alternative_download_kib': native['alt-speed-down'] * unit / 1024,
                                    'schedule_enabled': bool(native.get('alt-speed-time-enabled'))}
            except Exception:
                result[name] = {'connected': False, 'error': '限速读取失败，请检查下载器连接'}
        return result

    def set_limits(self, values, *, only=None):
        if not isinstance(values, dict) or set(values) - {'client', 'upload_kib', 'download_kib', 'disable_alternative'}:
            raise ManagementError('限速参数无效')
        name = values.get('client')
        if only is not None and only != name:
            raise ManagementError('实例类型不匹配')
        if name not in ('qb', 'tr') or type(values.get('disable_alternative', False)) is not bool:
            raise ManagementError('请选择下载器并检查备用限速开关')
        for key in ('upload_kib', 'download_kib'):
            value = values.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1048576 or (value and value < 1):
                raise ManagementError('限速应为 0 或至少 1 KiB/s，0 表示不限速')
        previous = None
        alt_before = None
        try:
            if name == 'qb':
                native = self.qget('app/preferences')
                previous = {key: native[key] for key in ('up_limit', 'dl_limit')}
                alt_before = self.qget('transfer/speedLimitsMode', binary=True).strip() == b'1'
                changes = {'up_limit': round(values['upload_kib'] * 1024), 'dl_limit': round(values['download_kib'] * 1024)}
                self.qpost('app/setPreferences', {'json': json.dumps(changes)})
                if values.get('disable_alternative') and alt_before:
                    self.qpost('transfer/toggleSpeedLimitsMode', {})
                after = self.qget('app/preferences')
                if any(after[key] != value for key, value in changes.items()):
                    raise ValueError()
                if values.get('disable_alternative') and self.qget('transfer/speedLimitsMode', binary=True).strip() != b'0':
                    raise ValueError()
            else:
                native = self.rpc('session-get')
                unit = native.get('units', {}).get('speed-bytes', 1000)
                if unit not in (1000, 1024):
                    raise ValueError()
                previous = {key: native[key] for key in ('speed-limit-up', 'speed-limit-down', 'speed-limit-up-enabled', 'speed-limit-down-enabled', 'alt-speed-enabled')}
                changes = {'speed-limit-up': round(values['upload_kib'] * 1024 / unit),
                           'speed-limit-down': round(values['download_kib'] * 1024 / unit),
                           'speed-limit-up-enabled': values['upload_kib'] > 0,
                           'speed-limit-down-enabled': values['download_kib'] > 0}
                if values.get('disable_alternative'):
                    changes['alt-speed-enabled'] = False
                self.rpc('session-set', changes)
                after = self.rpc('session-get')
                if any(after.get(key) != value for key, value in changes.items()):
                    raise ValueError()
        except Exception:
            restored = False
            if previous is not None:
                try:
                    if name == 'qb':
                        self.qpost('app/setPreferences', {'json': json.dumps(previous)})
                        if alt_before != (self.qget('transfer/speedLimitsMode', binary=True).strip() == b'1'):
                            self.qpost('transfer/toggleSpeedLimitsMode', {})
                        restored = (all(self.qget('app/preferences').get(key) == value for key, value in previous.items())
                                    and alt_before == (self.qget('transfer/speedLimitsMode', binary=True).strip() == b'1'))
                    else:
                        self.rpc('session-set', previous)
                        restored = all(self.rpc('session-get').get(key) == value for key, value in previous.items())
                except Exception:
                    pass
            raise ManagementError('限速保存未确认，已恢复原设置' if restored else '限速保存未确认，请刷新检查当前设置', 502) from None
        return self.limits(only=only)


def tr_seeder_values(torrent, source, now, fresh_seconds=FRESH_SECONDS):
    values = []
    for tracker in torrent.get('trackerStats', []):
        if not pts_url(tracker.get('announce'), source):
            continue
        value = count(tracker.get('seederCount'))
        stamp = max(tracker.get('lastScrapeTime', 0) if tracker.get('lastScrapeSucceeded') else 0,
                    tracker.get('lastAnnounceTime', 0) if tracker.get('lastAnnounceSucceeded') else 0)
        if value is not None and type(stamp) in (int, float) and 0 < stamp <= now + 60 and now - stamp <= fresh_seconds:
            values.append(value)
    return values


def tr_seeders(torrent, source, now):
    values = tr_seeder_values(torrent, source, now)
    return max(values) if values else None


def classify(is_pts, limit, seeds, complete, active, *, boundary_unconfirmed=True):
    seeders = max(seeds) if seeds else None
    conflict = limit is not None and seeds and min(seeds) <= limit < max(seeds)
    if not is_pts:
        validity, label, reason = 'other', '非 PTS', '该任务不属于 PTS'
    elif limit is None or seeders is None or conflict:
        validity, label, reason = 'unknown', '未知', '无法确认当前 PTS 人数或站点上限' if not conflict else '当前 Tracker 人数报告不一致'
    elif seeders > limit:
        validity, label, reason = 'invalid', '失效（人数超限）', '当前做种人数严格超过 PTS 上限'
    elif not complete:
        validity, label, reason = 'downloading', '下载未完成', '人数符合条件，下载完成后才能做种'
    elif not active:
        validity, label, reason = 'inactive', '未在做种', '人数符合条件，但任务暂停、排队或存在错误'
    elif boundary_unconfirmed and seeders == limit:
        validity, label, reason = 'unknown', '边界待确认', '当前人数处于站端上限边界，是否计入站端有效尚待确认'
    else:
        validity, label, reason = 'valid', '有效（本地判定）', '人数符合站点上限且本地正在做种，实际计数仍以站端为准'
    return {'seeders': seeders, 'validity': validity, 'validity_text': label, 'reason': reason}


def task_state(client, torrent):
    """qB-style, overlapping status filters; completion never grants transfer permission."""
    groups = []
    if client == 'qb':
        state = torrent.get('state')
        # qB 5.2 TorrentImpl::isCompleted() uses the upload-side states, even during recheck.
        completed = state in ('uploading', 'stalledUP', 'forcedUP', 'checkingUP',
                              'stoppedUP', 'pausedUP', 'queuedUP')
        labels = {'uploading': '做种中', 'stalledUP': '做种中（等待上传）', 'forcedUP': '强制做种',
                  'queuedUP': '排队做种', 'stoppedUP': '已暂停（已完成）', 'pausedUP': '已暂停（已完成）',
                  'checkingUP': '校验中（已完成）', 'downloading': '下载中', 'stalledDL': '等待下载',
                  'forcedDL': '强制下载', 'metaDL': '获取种子元数据', 'forcedMetaDL': '强制获取元数据',
                  'queuedDL': '排队下载', 'stoppedDL': '已暂停（未完成）', 'pausedDL': '已暂停（未完成）',
                  'checkingDL': '校验中（未完成）', 'checkingResumeData': '检查恢复数据',
                  'moving': '移动文件中', 'missingFiles': '文件缺失', 'error': '任务错误'}
        downloading = state in ('downloading', 'stalledDL', 'forcedDL', 'metaDL', 'forcedMetaDL',
                                'queuedDL', 'stoppedDL', 'pausedDL', 'checkingDL')
        seeding = state in ('uploading', 'stalledUP', 'forcedUP', 'checkingUP', 'queuedUP')
        paused = state in ('stoppedUP', 'stoppedDL', 'pausedUP', 'pausedDL')
        queued = state in ('queuedUP', 'queuedDL')
        checking = state in ('checkingUP', 'checkingDL', 'checkingResumeData')
        stalled = state in ('stalledUP', 'stalledDL')
        error = state in ('error', 'missingFiles')
        moving = state == 'moving'
        running = state in labels and not paused and not error
        label = labels.get(state, '状态未知')
        upload, download = torrent.get('upspeed', 0), torrent.get('dlspeed', 0)
    else:
        state = torrent.get('status')
        progress = torrent.get('percentDone', 0)
        completed = isinstance(progress, (int, float)) and math.isfinite(progress) and progress >= 1
        suffix = '（已完成）' if completed else '（未完成）'
        labels = {0: '已暂停' + suffix, 1: '排队校验' + suffix, 2: '校验中' + suffix,
                  3: '排队下载', 4: '下载中', 5: '排队做种', 6: '做种中'}
        error = bool(torrent.get('error'))
        label = '任务错误' if error else labels.get(state, '状态未知')
        downloading, seeding = state in (3, 4), state == 6
        paused, queued, checking = state == 0, state in (1, 3, 5), state in (1, 2)
        running, moving = state in (4, 6), False
        upload, download = torrent.get('rateUpload', 0), torrent.get('rateDownload', 0)
        stalled = running and not error and upload == 0 and download == 0
    for name, matches in (('completed', completed), ('downloading', downloading), ('seeding', seeding),
                          ('paused', paused), ('queued', queued), ('checking', checking), ('stalled', stalled),
                          ('error', error), ('moving', moving), ('running', running)):
        if matches:
            groups.append(name)
    if label == '状态未知':
        groups.append('unknown')
    active = any(isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0 for rate in (upload, download))
    groups.append('active' if active else 'inactive')
    return {'completed': bool(completed), 'state_text': label, 'state_groups': groups}


def inventory_rows(qb, tr, source, batch, limit, now, settings=None):
    boundary_unconfirmed = (settings or {}).get('seeders_limit_mode', 'site') != 'manual'
    records, record_aliases = {}, {}
    for bucket in ('accepted', 'pending', 'unconfirmed'):
        for tid, record in batch.get(bucket, {}).items():
            values = pull.record_hashes(record)
            for value in values:
                records[value] = tid
                record_aliases[value] = values
    groups, aliases = [], {}
    for client, torrents in (('qb', qb), ('tr', tr)):
        for torrent in torrents:
            key = str(torrent.get('hash' if client == 'qb' else 'hashString', '')).lower()
            if not HASH.fullmatch(key):
                continue
            values = {key}
            if client == 'qb':
                values.update(str(torrent.get(field, '')).lower() for field in ('infohash_v1', 'infohash_v2') if HASH.fullmatch(str(torrent.get(field, ''))))
            for value in tuple(values):
                values.update(record_aliases.get(value, ()))
            found = []
            for value in values:
                if value in aliases and all(aliases[value] is not entry for entry in found):
                    found.append(aliases[value])
            if found:
                group = found[0]
                for other in found[1:]:
                    for name in ('qb', 'tr'):
                        group[name] = group[name] or other[name]
                    group['aliases'].update(other['aliases'])
                    groups.remove(other)
                    for value in other['aliases']:
                        aliases[value] = group
            else:
                group = {'hash': key, 'qb': None, 'tr': None, 'aliases': set()}
                groups.append(group)
            group[client] = torrent
            group['aliases'].update(values)
            for value in values:
                aliases[value] = group
    rows = []
    for group in groups:
        q, t = group['qb'] or {}, group['tr'] or {}
        locations = [client for client in ('qb', 'tr') if group[client] is not None]
        tid = next((records[value] for value in group['aliases'] if value in records), None)
        is_pts = tid is not None or pts_url(q.get('tracker'), source) or any(pts_url(track.get('announce'), source) for track in t.get('trackerStats', []))
        seeds = tr_seeder_values(t, source, now, (settings or {}).get('tracker_fresh_seconds', FRESH_SECONDS))
        if pts_url(q.get('tracker'), source) and count(q.get('num_complete')) is not None:
            seeds.append(q['num_complete'])
        qstate, tstate = q.get('state'), t.get('status')
        qactive = qstate in ('uploading', 'stalledUP', 'forcedUP')
        tactive = tstate == 6 and not t.get('error')
        progress = max(q.get('progress', 0), t.get('percentDone', 0))
        states = {client: task_state(client, q if client == 'qb' else t) for client in locations}
        state_text = states[locations[0]]['state_text'] if len(locations) == 1 else ' / '.join(
            ('qB' if client == 'qb' else 'TR') + '：' + states[client]['state_text'] for client in locations)
        client_states = {}
        for client in locations:
            cp = q.get('progress', 0) if client == 'qb' else t.get('percentDone', 0)
            active = qactive if client == 'qb' else tactive
            client_states[client] = {**classify(is_pts, limit, seeds, cp >= 1, active, boundary_unconfirmed=boundary_unconfirmed), 'progress': min(1, max(0, cp)),
                                     **states[client],
                                     'upload_speed': q.get('upspeed', 0) if client == 'qb' else t.get('rateUpload', 0),
                                     'download_speed': q.get('dlspeed', 0) if client == 'qb' else t.get('rateDownload', 0)}
        size = q.get('total_size', t.get('totalSize', 0))
        can_transfer = bool(q and completed_qb(q) and transfer_path(q.get('save_path'), settings)
                            and tstate not in (1, 2))
        rows.append({'hash': group['hash'], 'name': str(q.get('name', t.get('name', '未命名任务')))[:500],
                     'pts': bool(is_pts), 'managed': tid is not None, 'torrent_id': tid,
                     'locations': locations, 'location': 'qB + TR' if len(locations) == 2 else 'qB' if locations[0] == 'qb' else 'TR',
                     'size': size, 'progress': min(1, max(0, progress)), 'qb_state': qstate, 'tr_state': tstate,
                     'state_text': state_text, 'completed': any(state['completed'] for state in states.values()),
                     'state_groups': sorted({name for state in states.values() for name in state['state_groups']}),
                     **classify(is_pts, limit, seeds, progress >= 1, qactive or tactive, boundary_unconfirmed=boundary_unconfirmed),
                     'client_states': client_states,
                     'invalid_since': None, 'delete_after': None, 'delete_ready': False, 'can_transfer': can_transfer,
                     'transfer_phase': None, 'upload_speed': q.get('upspeed', 0) + t.get('rateUpload', 0),
                     'download_speed': q.get('dlspeed', 0) + t.get('rateDownload', 0),
                     '_qb': group['qb'], '_tr': group['tr'], '_aliases': group['aliases']})
    rows.sort(key=lambda row: (not row['pts'], row['name'].lower(), row['hash']))
    return rows
