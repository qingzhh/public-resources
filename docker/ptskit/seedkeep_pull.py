#!/usr/bin/env python3
"""Pull a persistent batch or maintain live seedkeep jobs across qB and TR."""
import argparse
import base64
import contextlib
import contextvars
from concurrent.futures import ThreadPoolExecutor
import html
import http.cookiejar
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from torrent_meta import MAX_META_BYTES, MetadataError, torrent_metadata
import seedkeep_configuration as configuration
import seedkeep_logstore as logstore
import seedkeep_tagging as tagging
_EVENT_DIRECTORY = contextvars.ContextVar('seedkeep_event_directory', default=None)


class PullError(Exception):
    pass


def event(name, **values):
    row = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': name, **values}
    directory = _EVENT_DIRECTORY.get()
    if directory is None:
        print(json.dumps(row, ensure_ascii=False), flush=True)
    else:
        logstore.append(directory, 'run.log', row)


@contextlib.contextmanager
def private_logging(directory):
    token = _EVENT_DIRECTORY.set(directory)
    try:
        yield
    finally:
        _EVENT_DIRECTORY.reset(token)


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def qualified(size, seeders, settings):
    return (type(size) is int and 0 < size < settings['max_bytes']
            and type(seeders) is int
            and settings['min_seeders'] <= seeders <= settings['max_seeders'])


def request(opener, url, body=None, headers=None, json_response=True, maximum=None, timeout=45):
    try:
        with opener.open(urllib.request.Request(url, data=body, headers=headers or {}), timeout=timeout) as response:
            data = response.read(maximum + 1) if maximum else response.read()
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise PullError('http_' + str(code)) from None
    except Exception as error:
        raise PullError('request_' + type(error).__name__) from None
    if maximum and len(data) > maximum:
        raise PullError('response_too_large')
    if not json_response:
        return data
    try:
        return json.loads(data)
    except Exception:
        raise PullError('invalid_json') from None


class Clients:
    def __init__(self, source, settings):
        source = configuration.resolve_source(source, settings)
        settings = configuration.resolve_settings(settings)
        self.source, self.settings = source, settings
        self.qb_url = configuration.qb_base(source) + '/api/v2/'
        self.qb = configuration.opener(source, 'bt', cookies=True)
        self.tr = configuration.opener(source, 'bt')
        try:
            credentials = configuration.tr_credentials(settings)
            user, password = credentials['username'], credentials['password']
        except Exception as error:
            raise PullError('tr_credentials_' + type(error).__name__) from None
        self.tr_headers = {'Content-Type': 'application/json',
                           'Authorization': 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode()}
        if source.get('username') or source.get('password'):
            self.qb_post('auth/login', {'username': source.get('username', ''), 'password': source.get('password', '')})

    def qb_get(self, operation):
        return request(self.qb, self.qb_url + operation, timeout=self.settings.get('qb_timeout_seconds', 45))

    def qb_post(self, operation, fields):
        return request(self.qb, self.qb_url + operation, urllib.parse.urlencode(fields).encode(),
                       {'Content-Type': 'application/x-www-form-urlencoded', 'Referer': self.qb_url.split('/api/v2/')[0] + '/'}, False, timeout=self.settings.get('qb_timeout_seconds', 45))

    def tr_get(self):
        body = json.dumps({'method': 'torrent-get', 'arguments': {'fields': ['hashString', 'name', 'totalSize', 'status', 'percentDone', 'trackerStats', 'error', 'doneDate', 'addedDate', 'labels']}}).encode()
        for _ in range(3):
            try:
                with self.tr.open(urllib.request.Request(self.settings['tr_url'], data=body, headers=self.tr_headers), timeout=self.settings.get('tr_timeout_seconds', 20)) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 409 and error.headers.get('X-Transmission-Session-Id'):
                    self.tr_headers['X-Transmission-Session-Id'] = error.headers['X-Transmission-Session-Id']
                    continue
                raise PullError('tr_http_' + str(error.code)) from None
            except Exception as error:
                raise PullError('tr_' + type(error).__name__) from None
            if result.get('result') != 'success':
                raise PullError('tr_rpc_failed')
            return result['arguments']['torrents']
        raise PullError('tr_session_failed')

    def snapshot(self):
        # Read TR on both sides of qB to cover normal qB-to-TR migration overlap.
        first = self.tr_get()
        qb = self.qb_get('torrents/info')
        second = self.tr_get()
        tr = {t['hashString'].lower(): t for t in first + second}
        return qb, tr

    def add(self, data):
        boundary = 'seedkeep' + uuid.uuid4().hex
        fields = {'savepath': self.source.get('download_path', ''), 'category': self.source.get('category', ''),
                  'tags': ','.join(tagging.merge_labels(self.source.get('tag', ''), [self.settings.get('managed_tag', tagging.DEFAULT_TAG)])), 'stopped': str(not self.settings.get('auto_start', True)).lower(), 'skip_checking': 'false', 'autoTMM': 'false'}
        parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode() for key, value in fields.items()]
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="filtered.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode())
        body = b''.join(parts) + data + f'\r\n--{boundary}--\r\n'.encode()
        answer = request(self.qb, self.qb_url + 'torrents/add', body,
                         {'Content-Type': 'multipart/form-data; boundary=' + boundary,
                          'Referer': self.qb_url.split('/api/v2/')[0] + '/'}, False, timeout=self.settings.get('qb_timeout_seconds', 45))
        if answer.strip() == b'Fails.':
            raise PullError('qb_add_rejected')


class FleetClients:
    """Read every enabled instance; only the explicitly default qB accepts additions."""
    def __init__(self, source, settings, api_factory=None, *, require_default=True):
        from seedkeep_instances import Registry
        from seedkeep_downloaders import API
        self.registry = Registry(lambda: source, lambda: settings, api_factory or API)
        try:
            self.instances = [row for row in self.registry.items() if row['enabled']]
            self.primary = self.registry.primary()
            if self.primary is None and require_default:
                raise PullError('enabled_default_qb_required')
            self.primary_api = self.registry.api(self.primary['id']) if self.primary else None
            self.source, self.settings = (self.primary_api.source, self.primary_api.settings) if self.primary_api else (source, settings)
        except PullError:
            raise
        except Exception:
            raise PullError('invalid_downloader_registry') from None

    def snapshot(self):
        qb, tr = {}, {}
        copies = {'qb': {}, 'tr': {}}
        # Two full passes cover migration overlap, including between qB instances.
        for _ in range(2):
            for instance in self.instances:
                try:
                    api = self.primary_api if self.primary and instance['id'] == self.primary['id'] else self.registry.api(instance['id'])
                    rows = api.inventory_one()
                    kind = instance['type']
                    for task in rows:
                        value = str(task.get('hash' if kind == 'qb' else 'hashString', '')).lower()
                        if not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', value):
                            raise ValueError()
                        size = task.get('total_size' if kind == 'qb' else 'totalSize')
                        if type(size) is not int or size < 0:
                            raise ValueError()
                        for old in (qb.get(value), tr.get(value)):
                            if old and old.get('total_size', old.get('totalSize')) != size:
                                raise ValueError()
                        copies[kind].setdefault(value, {})[instance['id']] = task
                        (qb if kind == 'qb' else tr)[value] = task
                except Exception:
                    raise PullError('fleet_inventory_unconfirmed') from None
        for kind, tasks in (('qb', qb), ('tr', tr)):
            for value, task in tuple(tasks.items()):
                tasks[value] = {**task, '_seedkeep_copies': list(copies[kind][value].values())}
        return list(qb.values()), tr

    def qb_get(self, operation):
        if self.primary_api is None:
            raise PullError('enabled_default_qb_required')
        return self.primary_api.qget(operation)

    def qb_post(self, operation, fields):
        if self.primary_api is None:
            raise PullError('enabled_default_qb_required')
        return self.primary_api.qpost(operation, fields)

    def add(self, data):
        if self.primary_api is None:
            raise PullError('enabled_default_qb_required')
        # The snapshot authenticates this API before the shared multipart implementation.
        self.qb, self.qb_url = self.primary_api.qb, self.primary_api.qb_url
        Clients.add(self, data)


def history_hashes(settings):
    path = Path(settings['historical_config'])
    config = json.loads(path.read_text())
    excluded_path = config.get('exclude_hashes_file')
    if not excluded_path:
        return set()
    data = json.loads(Path(excluded_path).read_text())
    if isinstance(data, dict):
        data = data['hashes'] if isinstance(data.get('hashes'), list) else list(data)
    if not isinstance(data, list) or any(not re.fullmatch(r'[0-9a-fA-F]{40,64}', str(value)) for value in data):
        raise PullError('invalid_historical_exclusions')
    return {str(value).lower() for value in data}


def candidate(item, settings):
    if not qualified(item.get('size'), item.get('seeders'), settings):
        return None
    if not item.get('download_url'):
        return None
    return {'id': str(item['id']), 'size': item['size'], 'seeders': item['seeders'],
            'url': item['download_url'], 'hash': str(item.get('hash', '')).lower()}


def pool(source, settings, clients):
    source = configuration.resolve_source(source, settings)
    site = configuration.opener(source)
    api = request(site, source['api_base'].rstrip('/') + '/api/v1/seedkeep/refill?limit=' + str(settings.get('candidate_limit', 1000)),
                  headers={'Authorization': 'Bearer ' + source['token']}, maximum=8 * 1024 * 1024, timeout=settings.get('site_timeout_seconds', 15))
    if api.get('ret') != 0:
        raise PullError('seedkeep_api_status_failed')
    raw = api.get('data', {}).get('candidates', [])
    items = {c['id']: c for item in raw if (c := candidate(item, settings)) is not None}
    api_eligible = len(items)
    if settings.get('mode') == 'maintain' or len(items) < settings['target']:
        rss = clients.qb_get('rss/items?withData=true')
        feeds = []
        def walk(value):
            if isinstance(value, dict):
                if isinstance(value.get('url'), str):
                    feeds.append(value['url'])
                else:
                    for child in value.values():
                        walk(child)
        walk(rss)
        expected_host = urllib.parse.urlsplit(source['api_base']).hostname
        feeds = [url for url in feeds if urllib.parse.urlsplit(url).hostname == expected_host
                 and urllib.parse.urlsplit(url).path.endswith('/seedkeeprss.php')]
        if len(feeds) != 1:
            raise PullError('unexpected_full_pool_feed_scope')
        try:
            with site.open(feeds[0], timeout=settings.get('site_timeout_seconds', 15)) as response:
                for _, node in ET.iterparse(response, events=('end',)):
                    if node.tag.rsplit('}', 1)[-1] != 'item':
                        continue
                    enclosure = node.find('enclosure')
                    if enclosure is not None:
                        text = html.unescape(re.sub('<[^>]*>', ' ', node.findtext('description', '')))
                        match = re.search(r'做种\s*[:：]?\s*(\d+)', text)
                        url = enclosure.get('url', '')
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                        tid = query.get('id', [''])[0]
                        if match and tid.isdigit():
                            c = candidate({'id': tid, 'size': int(enclosure.get('length', '0')),
                                           'seeders': int(match[1]), 'download_url': url,
                                           'hash': node.findtext('guid', '').strip()}, settings)
                            if c is not None:
                                items[c['id']] = c
                    node.clear()
        except Exception as error:
            raise PullError('full_pool_' + type(error).__name__) from None
    event('candidate_pool', api_returned=len(raw), api_eligible=api_eligible,
          eligible_with_full_pool=len(items))
    return items, time.time()


class Downloader:
    def __init__(self, source, settings=None, deadline=None):
        self.settings = settings or {}
        self.source = configuration.resolve_source(source, self.settings)
        self.lock = threading.Lock()
        self.next_request = 0
        self.deadline = deadline

    def remaining(self):
        return float('inf') if self.deadline is None else max(0, self.deadline - time.monotonic())

    def wait(self, seconds):
        time.sleep(min(max(0, seconds), self.remaining()))
        return self.remaining() > 0

    def get(self, c):
        for attempt in range(self.settings.get('download_retries', 3)):
            if not self.remaining():
                return None, None, 'run_time_limit'
            try:
                with self.lock:
                    now = time.monotonic()
                    start = max(now, self.next_request)
                    self.next_request = start + self.settings.get('request_interval_seconds', 1.0)
                if not self.wait(start - time.monotonic()):
                    return None, None, 'run_time_limit'
                opener = configuration.opener(self.source)
                timeout = min(self.settings.get('download_timeout_seconds', 45), self.remaining())
                if timeout <= 0:
                    return None, None, 'run_time_limit'
                data = request(opener, c['url'], json_response=False, maximum=MAX_META_BYTES, timeout=timeout)
                return data, torrent_metadata(data), None
            except (PullError, MetadataError) as error:
                if not self.remaining():
                    return None, None, 'run_time_limit'
                if attempt == self.settings.get('download_retries', 3) - 1:
                    return None, None, str(error)
                if not self.wait(self.settings.get('download_retry_seconds', 2)):
                    return None, None, 'run_time_limit'


def validate_settings(settings):
    settings = dict(settings)
    settings.setdefault('mode', 'batch')
    settings.setdefault('max_per_run', 50)
    settings.setdefault('max_run_seconds', 1800)
    settings.setdefault('pending_grace_seconds', 300)
    settings.setdefault('interval_hours', 2)
    settings.setdefault('cron_minute', 17)
    if settings['mode'] not in ('batch', 'maintain'):
        raise PullError('invalid_mode')
    for key in ('target', 'max_bytes', 'max_per_run', 'max_run_seconds'):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise PullError('invalid_' + key)
    for key in ('min_seeders', 'max_seeders', 'pending_grace_seconds', 'cron_minute'):
        if type(settings[key]) is not int or settings[key] < 0:
            raise PullError('invalid_' + key)
    if settings['min_seeders'] > settings['max_seeders'] or settings['cron_minute'] > 59:
        raise PullError('invalid_filter_or_schedule')
    if type(settings['interval_hours']) is not int or settings['interval_hours'] not in (1, 2, 3, 4, 6, 8, 12, 24):
        raise PullError('invalid_interval_hours')
    try:
        return configuration.resolve_settings(configuration.validate_runtime(settings, strict=False))
    except configuration.ConfigurationError as error:
        raise PullError(str(error)) from None


def record_hashes(record):
    return {value.lower() for value in record.get('hashes', []) + [record['hash']]}


def managed_counts(state, qb, tr, tag=None):
    if tag is not None:
        try:
            return tagging.inventory_budget(state, qb, tr, tag)
        except tagging.TaggingError as error:
            raise PullError(str(error)) from None
    records = [record for key in ('accepted', 'pending', 'unconfirmed')
               for record in state.get(key, {}).values()]
    qb_live = {t['hash'].lower() for t in qb}
    tr_live = set(tr)
    qb_hashes = {r['hash'].lower() for r in records if record_hashes(r) & qb_live}
    tr_hashes = {r['hash'].lower() for r in records if record_hashes(r) & tr_live}
    live = qb_hashes | tr_hashes
    reserved = sum(not record_hashes(record).intersection(qb_live | tr_live)
                   for record in state.get('pending', {}).values())
    return {'managed_active': len(live), 'managed_qb': len(qb_hashes),
            'managed_tr': len(tr_hashes), 'managed_both': len(qb_hashes & tr_hashes),
            'pending_reserved': reserved}


def reconcile_pending(state, qb, tr, settings):
    live = {t['hash'].lower(): t['total_size'] for t in qb}
    for key, torrent in tr.items():
        if key in live and live[key] != torrent['totalSize']:
            raise PullError('cross_client_size_mismatch')
        live[key] = torrent['totalSize']
    for bucket in ('pending', 'unconfirmed'):
        for tid, record in list(state[bucket].items()):
            present = record_hashes(record).intersection(live)
            if present:
                if any(live[value] != record['size'] for value in present):
                    raise PullError('uncertain_pending_size_mismatch')
                state['accepted'][tid] = record
                del state[bucket][tid]
            elif bucket == 'pending':
                if settings['mode'] == 'batch':
                    raise PullError('uncertain_pending_add_requires_check')
                if time.time() - record['added_at'] >= settings['pending_grace_seconds']:
                    # Keep the hash permanently, even if the add failed or was removed.
                    state['unconfirmed'][tid] = record
                    del state['pending'][tid]


def run(settings, source, directory, clients=None, pool_reader=pool, downloader=None, *, allowance=None):
    settings = validate_settings(settings)
    if allowance is not None and (settings['mode'] != 'maintain' or type(allowance) is not int or not 1 <= allowance <= 1000):
        raise PullError('invalid_refill_allowance')
    source = configuration.resolve_source(source, settings)
    if clients is None:
        clients = FleetClients(source, settings) if 'downloaders' in settings else Clients(source, settings)
    if isinstance(clients, FleetClients):
        source = clients.source
        settings = {**settings, 'connections': clients.settings.get('connections', {})}
        for key in ('tr_url', 'tr_proxy_source', 'tr_labels'):
            if key in clients.settings:
                settings[key] = clients.settings[key]
    state_path = directory / 'batch.json'
    status_path = directory / 'status.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'accepted': {}, 'pending': {}, 'baseline': None}
    # Task removal may create permanent exclusions before the first refill batch.
    state.setdefault('accepted', {})
    state.setdefault('pending', {})
    state.setdefault('baseline', None)
    state.setdefault('unconfirmed', {})
    criteria = {key: settings[key] for key in ('target', 'max_bytes', 'min_seeders', 'max_seeders')}
    old_criteria = state.get('criteria', criteria)
    checked = () if settings['mode'] == 'maintain' and settings.get('allow_filter_changes') else (('max_bytes', 'min_seeders', 'max_seeders') if settings['mode'] == 'maintain' else tuple(criteria))
    if any(old_criteria[key] != criteria[key] for key in checked):
        raise PullError('existing_batch_criteria_changed')
    # The original batch target remains recorded; maintain target is configurable.
    state.setdefault('criteria', criteria)
    state['schema_version'] = 2
    state['active_criteria'] = criteria
    historical = history_hashes(settings)
    historical.update(state.get('seen_hashes', []))
    qb, tr = clients.snapshot()
    if state['baseline'] is None:
        state['baseline'] = {'qb': {t['hash'].lower(): {key: t.get(key) for key in ('tags', 'category', 'save_path', 'total_size')} for t in qb},
                             'tr_hashes': list(tr), 'historical_hashes': sorted(historical), 'created_at': time.time()}
    for key in ('qb', 'tr_hashes', 'historical_hashes'):
        historical.update(state['baseline'].get(key, []))
    errors = skipped = added = 0
    started_at = time.time()
    deadline = time.monotonic() + settings['max_run_seconds']

    def remember():
        historical.update(t['hash'].lower() for t in qb)
        historical.update(tr)
        state['seen_hashes'] = sorted(historical)
        # Persist newly observed hashes even when pending validation fails.
        save_json(state_path, state)
        reconcile_pending(state, qb, tr, settings)
        save_json(state_path, state)

    def snapshot():
        nonlocal qb, tr
        qb, tr = clients.snapshot()
        remember()

    def remaining():
        if allowance is not None:
            return max(0, min(allowance, settings['max_per_run']) - added)
        counts = managed_counts(state, qb, tr, settings['managed_tag'] if settings['mode'] == 'maintain' else None)
        current = counts['managed_active'] + counts['pending_reserved'] if settings['mode'] == 'maintain' else len(state['accepted'])
        needed = max(0, settings['target'] - current)
        return min(needed, settings['max_per_run'] - added) if settings['mode'] == 'maintain' else needed

    def status(outcome='running', reason=None):
        counts = managed_counts(state, qb, tr, settings['managed_tag'] if settings['mode'] == 'maintain' else None)
        current = counts['managed_active'] if settings['mode'] == 'maintain' else len(state['accepted'])
        result = {'mode': settings['mode'], 'target': settings['target'],
                  'accepted': len(state['accepted']), 'pending': len(state['pending']),
                  'unconfirmed': len(state['unconfirmed']), **counts,
                  'finished': current >= settings['target'] if allowance is None else added >= min(allowance, settings['max_per_run']), 'criteria': criteria,
                  'added_this_run': added, 'max_per_run': settings['max_per_run'],
                  'seeding_total': counts['managed_active'] if settings['mode'] == 'maintain' else None,
                  'errors': errors, 'skipped_existing': skipped, 'run_outcome': outcome,
                  'refill_allowance': allowance,
                  'stop_reason': reason, 'started_at': started_at, 'updated_at': time.time()}
        save_json(status_path, result)
        return result

    def known_hashes():
        known = set(historical)
        for bucket in ('accepted', 'pending', 'unconfirmed'):
            for record in state[bucket].values():
                known.update(record_hashes(record))
        return known

    remember()
    status()
    if not remaining():
        event('target_already_satisfied', **status('success', 'target_or_pending_reservation'))
        return
    all_candidates, source_time = pool_reader(source, settings, clients)
    attempted = set()
    downloader = downloader or Downloader(source, settings, deadline=deadline)
    consecutive_errors = 0
    reason = 'target_or_run_limit'
    with ThreadPoolExecutor(max_workers=settings['download_concurrency']) as executor:
        while remaining():
            if time.monotonic() >= deadline:
                reason = 'run_time_limit'
                break
            if time.time() - source_time > settings['candidate_refresh_seconds']:
                all_candidates, source_time = pool_reader(source, settings, clients)
            snapshot()
            if not remaining():
                break
            known = known_hashes()
            recorded_ids = set().union(*(set(state[key]) for key in ('accepted', 'pending', 'unconfirmed')))
            choices = [c for tid, c in all_candidates.items() if tid not in attempted
                       and tid not in recorded_ids and c['hash'] not in known]
            choices.sort(key=lambda c: (c['seeders'], c['size'], int(c['id'])))
            if not choices:
                if settings['mode'] == 'batch':
                    raise PullError('insufficient_qualified_unique_candidates')
                reason = 'candidate_pool_exhausted'
                break
            choices = choices[:min(max(8, settings['download_concurrency']), remaining())]
            futures = [executor.submit(downloader.get, c) for c in choices]
            for c, future in zip(choices, futures):
                attempted.add(c['id'])
                try:
                    data, meta, error = future.result(timeout=max(0, deadline - time.monotonic()))
                except TimeoutError:
                    for pending in futures:
                        pending.cancel()
                    reason = 'run_time_limit'
                    break
                if time.monotonic() >= deadline:
                    reason = 'run_time_limit'
                    break
                if error:
                    errors += 1
                    consecutive_errors += 1
                    event('download_failed', reason=error, errors=errors)
                    status()
                    if consecutive_errors >= 5:
                        raise PullError('repeated_download_failures')
                    continue
                consecutive_errors = 0
                if time.monotonic() >= deadline:
                    reason = 'run_time_limit'
                    break
                if time.time() - source_time > settings['candidate_refresh_seconds']:
                    all_candidates, source_time = pool_reader(source, settings, clients)
                c = all_candidates.get(c['id'])
                if c is None or not qualified(meta['size'], c['seeders'], settings):
                    continue
                if c['hash'] and c['hash'] not in meta['hashes']:
                    raise PullError('source_metainfo_identity_mismatch')
                snapshot()
                if not remaining():
                    break
                if known_hashes().intersection(meta['hashes']):
                    skipped += 1
                    continue
                record = {'hash': meta['hashes'][0][:40], 'hashes': meta['hashes'],
                          'size': meta['size'], 'seeders_at_pull': c['seeders'],
                          'source_time': source_time, 'added_at': time.time()}
                if isinstance(clients, FleetClients):
                    record['instance_id'] = clients.primary['id']
                    record['destination'] = {key: source.get(key, '') for key in ('download_path', 'category', 'tag', 'keep_torrent')}
                state['pending'][c['id']] = record
                save_json(state_path, state)
                clients.add(data)
                found = []
                for _ in range(12):
                    query = 'torrents/info?' + urllib.parse.urlencode({'hashes': '|'.join(meta['hashes'])})
                    found = clients.qb_get(query)
                    if found:
                        break
                    time.sleep(0.5)
                if found:
                    if len(found) != 1 or found[0]['total_size'] != meta['size']:
                        raise PullError('added_metainfo_not_verified')
                    if record['hash'] != found[0]['hash'].lower():
                        raise PullError('added_hash_not_verified')
                    if (found[0].get('category', '') != source.get('category', '')
                            or (source.get('tag') and not {v.strip() for v in source['tag'].split(',') if v.strip()}.issubset({v.strip() for v in found[0].get('tags', '').split(',')}))
                            or (source.get('download_path') and Path(found[0]['save_path']) != Path(source['download_path']))):
                        raise PullError('added_destination_not_verified')
                else:
                    # The existing completion flow may already have moved it to TR.
                    _, moved = clients.snapshot()
                    if moved.get(record['hash'], {}).get('totalSize') != meta['size']:
                        raise PullError('added_metainfo_not_verified')
                if source.get('keep_torrent'):
                    torrent_dir = directory / 'torrents'
                    torrent_dir.mkdir(exist_ok=True, mode=0o700)
                    path = torrent_dir / (c['id'] + '.torrent')
                    path.write_bytes(data)
                    os.chmod(path, 0o600)
                state['accepted'][c['id']] = record
                del state['pending'][c['id']]
                added += 1
                save_json(state_path, state)
                snapshot()
                result = status()
                if added % 25 == 0 or not remaining():
                    event('progress', **result)
    snapshot()
    event('run_complete', **status('success', reason))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--log', action='store_true', help='Append to the private rotating run.log')
    parser.add_argument('--refill-allowance', type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    import fcntl
    directory = args.config.resolve().parent
    with (directory / 'run.lock').open('a') as lock, contextlib.ExitStack() as stack:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            event('already_running')
            return 0
        if args.log:
            stack.enter_context(private_logging(directory))
        try:
            settings = validate_settings(json.loads(args.config.read_text()))
            source = json.loads(Path(settings['source_config']).read_text(encoding='utf-8-sig'))
            run(settings, source, directory, allowance=args.refill_allowance)
        except Exception as error:
            reason = str(error) if isinstance(error, (PullError, MetadataError)) else type(error).__name__
            event('failed', reason=reason)
            path = directory / 'status.json'
            try:
                last = json.loads(path.read_text()) if path.exists() else {}
                save_json(path, {**last, 'run_outcome': 'failed', 'last_error': reason, 'updated_at': time.time()})
            except Exception:
                event('status_write_failed')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
