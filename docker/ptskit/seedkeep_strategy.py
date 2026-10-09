"""Offline refill decisions; private durable observations, safe public counters only."""
import copy
import json
import math
from pathlib import Path
import re
import time

from seedkeep_downloaders import inventory_rows, pts_url, task_state
from seedkeep_pull import save_json
import seedkeep_tagging as tagging

HASH = re.compile(r'^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$')
STATUSES = {'idle', 'refilling', 'waiting_downloads', 'waiting_sync', 'at_target',
            'below_trigger', 'unknown', 'disabled', 'busy', 'running'}
BASES = {'site_effective', 'managed_tasks'}
MAX_TASKS = 100000
MAX_ALIASES = 128
MAX_STATE_BYTES = 64 * 1024 * 1024


class StrategyError(Exception):
    """Intentionally generic: never include persisted data or downloader details."""
    status = 500


def _time(value):
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= 253402300799)


def _observed_time(value, now):
    return value if _time(value) and 0 < value <= now + 60 else None


def _sync_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
        return None
    try:
        parsed = time.strptime(value, '%Y-%m-%d %H:%M:%S')
        stamp = time.mktime(parsed)  # Service/Docker local timezone, including DST.
        if time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp)) != value:
            return None
        return stamp if _time(stamp) else None
    except (ValueError, OverflowError, OSError):
        return None


def _blank(basis='managed_tasks'):
    return {'basis': basis, 'active': False, 'status': 'idle', 'site_current': None,
            'site_synced_at': None, 'reserved': 0, 'inflight': 0, 'allowance': 0,
            'receipt_reserved': 0, 'pending_reserved': 0, 'last_checked_at': None,
            'warning': False, 'next_check_at': None}


def _aliases(record):
    values = record.get('hashes', [])
    if not isinstance(values, list) or len(values) > MAX_ALIASES:
        raise ValueError('invalid aliases')
    values = values + [record.get('hash')]
    if any(not isinstance(value, str) or not HASH.fullmatch(value) for value in values):
        raise ValueError('invalid hash')
    return sorted({value.lower() for value in values})


def _validate(state):
    if not isinstance(state, dict) or set(state) != {'version', 'active', 'summary', 'observations'}:
        raise ValueError('invalid state')
    if type(state['version']) is not int or state['version'] != 1 or type(state['active']) is not bool:
        raise ValueError('invalid version')
    summary = state['summary']
    if not isinstance(summary, dict) or set(summary) != set(_blank()):
        raise ValueError('invalid summary')
    if summary['basis'] not in BASES or summary['status'] not in STATUSES:
        raise ValueError('invalid status')
    if any(type(summary[key]) is not bool for key in ('active', 'warning')) or summary['active'] != state['active']:
        raise ValueError('invalid flags')
    for key in ('reserved', 'inflight', 'allowance', 'receipt_reserved', 'pending_reserved'):
        if type(summary[key]) is not int or not 0 <= summary[key] <= 2 * MAX_TASKS:
            raise ValueError('invalid count')
    current = summary['site_current']
    if current is not None and (type(current) is not int or current < 0):
        raise ValueError('invalid current')
    synced = summary['site_synced_at']
    if synced is not None and _sync_time(synced) is None:
        raise ValueError('invalid sync')
    for key in ('last_checked_at', 'next_check_at'):
        if summary[key] is not None and not _time(summary[key]):
            raise ValueError('invalid time')
    observations = state['observations']
    if not isinstance(observations, list) or len(observations) > MAX_TASKS:
        raise ValueError('invalid observations')
    seen = set()
    for record in observations:
        if not isinstance(record, dict) or set(record) != {'aliases', 'first_seen', 'completed_at'}:
            raise ValueError('invalid observation')
        values = record['aliases']
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_ALIASES:
            raise ValueError('invalid observation aliases')
        if any(not isinstance(value, str) or not HASH.fullmatch(value) or value != value.lower() for value in values):
            raise ValueError('invalid observation hash')
        if len(set(values)) != len(values) or seen.intersection(values):
            raise ValueError('overlapping observations')
        seen.update(values)
        if not _time(record['first_seen']):
            raise ValueError('invalid first observation')
        completed = record['completed_at']
        if completed is not None and (not _time(completed) or completed < record['first_seen']):
            raise ValueError('invalid completion observation')
    return state


class Strategy:
    def __init__(self, directory, clock=time.time):
        self.path = Path(directory) / 'refill_state.json'
        self.clock = clock
        self._state = {'version': 1, 'active': False, 'summary': _blank(), 'observations': []}
        try:
            if self.path.exists():
                if self.path.stat().st_size > MAX_STATE_BYTES:
                    raise ValueError('state too large')
                self._state = _validate(json.loads(self.path.read_text(encoding='utf-8')))
        except Exception:
            raise StrategyError('补量策略状态无法安全恢复') from None

    def _commit(self, state):
        try:
            _validate(state)
            if len(json.dumps(state, ensure_ascii=False).encode('utf-8')) > MAX_STATE_BYTES:
                raise ValueError('state too large')
            save_json(self.path, state)
        except Exception:
            raise StrategyError('补量策略状态无法安全保存') from None
        self._state = state

    def public(self, settings, enabled, next_check_at):
        """Pure projection. A basis switch must not reuse the other basis' sample."""
        basis = settings.get('refill_count_basis', 'managed_tasks')
        summary = dict(self._state['summary']) if self._state['summary']['basis'] == basis else _blank(basis)
        summary['next_check_at'] = next_check_at if _time(next_check_at) else None
        if basis == 'site_effective' and summary['site_current'] is not None:
            now, synced = self.clock(), _sync_time(summary['site_synced_at'])
            fresh = (_time(now) and synced is not None
                     and -60 <= now - synced <= settings.get('refill_site_max_age_minutes', 120) * 60)
            if not fresh:
                summary.update(site_current=None, site_synced_at=None, allowance=0, warning=False)
                if summary['status'] not in {'busy', 'running', 'disabled'}:
                    summary['status'] = 'unknown'
        if not enabled:
            summary.update(active=False, status='disabled', allowance=0)
        if basis == 'managed_tasks':
            summary.update(site_current=None, site_synced_at=None, warning=False)
        return summary

    def reset(self, *, preserve_active=False):
        state = copy.deepcopy(self._state)
        active = state['active'] if preserve_active else False
        summary = _blank(state['summary']['basis'])
        summary['active'] = active
        state.update(active=active, summary=summary)
        self._commit(state)

    def defer(self, status='busy'):
        if status not in STATUSES:
            raise StrategyError('补量策略状态无效')
        state = copy.deepcopy(self._state)
        state['summary']['status'] = status
        self._commit(state)

    def pause(self):
        state = copy.deepcopy(self._state)
        state['active'] = False
        state['summary'].update(active=False, status='disabled', allowance=0)
        self._commit(state)

    def _inventory(self, qb, tr, batch, source, settings, now):
        """Unify current and saved aliases before the shared inventory groups tasks."""
        qb = tagging.inventory_tasks(qb)
        tr = tagging.inventory_tasks(tr)
        parent = {}

        def root(value):
            parent.setdefault(value, value)
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def link(values):
            first = root(values[0])
            for value in values[1:]:
                parent[root(value)] = first

        clean = {bucket: {} for bucket in ('accepted', 'pending', 'unconfirmed')}
        managed = set()
        for bucket in clean:
            records = batch.get(bucket, {})
            if not isinstance(records, dict) or len(records) > MAX_TASKS:
                raise ValueError('invalid batch')
            for tid, record in records.items():
                values = _aliases(record)
                link(values)
                managed.update(values)
                clean[bucket][tid] = {'hash': values[0], 'hashes': values, 'added_at': record.get('added_at')}
        for observation in self._state['observations']:
            link(observation['aliases'])
        components = {}
        for value in parent:
            components.setdefault(root(value), set()).add(value)
        for values in components.values():
            if len(values) > MAX_ALIASES:
                raise ValueError('too many aliases')
        for records in clean.values():
            for record in records.values():
                record['hashes'] = sorted(components[root(record['hash'])])
        managed = {alias for values in components.values() if managed.intersection(values) for alias in values}
        # Synthetic records carry only previously observed PTS identities, no task metadata.
        inventory_batch = copy.deepcopy(clean)
        for index, observation in enumerate(self._state['observations']):
            values = sorted(components[root(observation['aliases'][0])])
            inventory_batch['unconfirmed']['observation:' + str(index)] = {'hash': values[0], 'hashes': values}
        tr_list = tr
        rows = inventory_rows(qb, tr_list, source or {}, inventory_batch, None, now, settings)
        if len(rows) > MAX_TASKS:
            raise ValueError('too many tasks')
        by_alias = {}
        for row in rows:
            row['managed'] = bool(managed.intersection(row['_aliases']))
            row['_clients'] = []
            for alias in row['_aliases']:
                by_alias[alias] = row
        # inventory keeps one representative per client; observe all instance states.
        for client, torrents in (('qb', qb), ('tr', tr_list)):
            for torrent in torrents:
                row = by_alias.get(str(torrent.get('hash' if client == 'qb' else 'hashString', '')).lower())
                if row is None:
                    continue
                status = task_state(client, torrent)
                row['_clients'].append((client, torrent, status))
                row['pts'] = row['pts'] or (pts_url(torrent.get('tracker'), source or {}) if client == 'qb' else
                                           any(pts_url(track.get('announce'), source or {}) for track in torrent.get('trackerStats', [])))
        return rows, clean

    def _observe(self, rows, batch, settings, now, synced):
        previous = {alias: record for record in self._state['observations'] for alias in record['aliases']}
        observations, live = [], set()
        reserved = inflight = receipts = 0
        timeout = settings.get('refill_reservation_hours', 72) * 3600
        for row in rows:
            live.update(row['_aliases'])
            if not row['pts']:
                continue
            aliases = sorted(row['_aliases'])
            if len(aliases) > MAX_ALIASES:
                raise ValueError('too many task aliases')
            old = {id(previous[alias]): previous[alias] for alias in aliases if alias in previous}
            first = min((record['first_seen'] for record in old.values()), default=None)
            completed_at = min((record['completed_at'] for record in old.values() if record['completed_at'] is not None), default=None)
            complete = any(status['completed'] or torrent.get('progress' if client == 'qb' else 'percentDone', 0) >= 1
                           for client, torrent, status in row['_clients'])
            if completed_at is not None and any({'checking', 'moving'}.intersection(status['state_groups'])
                                                for _, _, status in row['_clients']):
                complete = True  # Rechecking/migration preserves the previous receipt phase.
            clients = [(torrent, ('added_on', 'added_at') if client == 'qb' else ('addedDate', 'added_at'),
                        'completion_on' if client == 'qb' else 'doneDate') for client, torrent, _ in row['_clients']]
            if first is None:
                if not complete:
                    times = [_observed_time(torrent.get(field), now) for torrent, fields, _ in clients for field in fields]
                    first = min([now] + [value for value in times if value is not None and value <= now])
                else:
                    times = [_observed_time(torrent.get(field), now) for torrent, _, field in clients]
                    times = [value for value in times if value is not None and value <= now and synced is not None and value > synced]
                    if not times:
                        continue  # Old completed tasks are not receipts or confirmed effective tasks.
                    completed_at = first = min(times)
            if complete and completed_at is None:
                completed_at = now
            eligible = any(not {'paused', 'error', 'unknown'}.intersection(status['state_groups'])
                           for _, _, status in row['_clients'])
            if not complete:
                inflight += 1
                if eligible and now - first < timeout:
                    reserved += 1
            elif eligible and completed_at is not None and now - first < timeout and (synced is None or synced <= completed_at):
                reserved += 1
                receipts += 1
            observations.append({'aliases': aliases, 'first_seen': first, 'completed_at': completed_at})
        pending_groups = set()
        grace = settings.get('pending_grace_seconds', 300)
        for record in batch['pending'].values():
            values = set(record['hashes'])
            added = _observed_time(record.get('added_at'), now)
            if live.intersection(values) or added is None or not 0 <= now - added < grace:
                continue
            # Aliases have already been transitively canonicalized by _inventory.
            pending_groups.add(record['hashes'][0])
        pending = len(pending_groups)
        return observations, reserved + pending, inflight, receipts, pending

    def evaluate(self, settings, site, qb, tr, batch, *, force=False, source=None):
        now = self.clock()
        if not _time(now):
            raise StrategyError('补量策略时钟无效')
        basis = settings.get('refill_count_basis', 'managed_tasks')
        state = copy.deepcopy(self._state)
        summary = _blank(basis)
        summary.update(active=state['active'], last_checked_at=now)
        synced = _sync_time(site.get('synced_at')) if isinstance(site, dict) else None
        trustworthy = (isinstance(site, dict) and site.get('available') is True
                       and site.get('stale') is False and type(site.get('current')) is int
                       and site['current'] >= 0 and synced is not None
                       and -60 <= now - synced <= settings.get('refill_site_max_age_minutes', 120) * 60)
        if basis == 'site_effective' and not trustworthy:
            # No observation refresh from an untrusted decision snapshot.
            summary.update({key: self._state['summary'][key] for key in
                            ('reserved', 'inflight', 'receipt_reserved', 'pending_reserved')})
            summary['status'] = 'unknown'
            state['summary'] = summary
            self._commit(state)
            return dict(summary)
        try:
            rows, clean = self._inventory(qb, tr, batch, source, settings, now)
            observations, reserved, inflight, receipts, pending = self._observe(
                rows, clean, settings, now, synced if trustworthy else None)
        except Exception:
            raise StrategyError('补量策略任务快照无效') from None
        state['observations'] = observations
        summary.update(reserved=reserved, inflight=inflight, receipt_reserved=receipts, pending_reserved=pending)
        target = settings['target']
        if basis == 'site_effective':
            current = site['current']
            summary.update(site_current=current, site_synced_at=site['synced_at'],
                           warning=current < settings.get('refill_floor', 1000))
            if current >= target:
                state['active'] = False
            elif current < settings.get('refill_trigger', 1100):
                state['active'] = True
            missing = target - current - reserved
            should_refill = state['active'] or force
        else:
            try:
                current = tagging.inventory_counts(batch, qb, tr, settings.get('managed_tag', tagging.DEFAULT_TAG))['managed_active']
            except tagging.TaggingError:
                raise StrategyError('标签保种库存无法确认') from None
            state['active'] = current + pending < target
            missing = target - current - pending
            should_refill = state['active']
        capacity = settings.get('refill_max_inflight', 500) - inflight - pending
        allowance = max(0, min(missing, settings['max_per_run'], capacity)) if should_refill and current < target else 0
        if current >= target:
            status = 'at_target'
        elif allowance:
            status = 'refilling'
        elif not should_refill:
            status = 'idle'
        elif inflight or pending:
            status = 'waiting_downloads'
        elif receipts:
            status = 'waiting_sync'
        else:
            status = 'below_trigger'
        summary.update(active=state['active'], status=status, allowance=allowance)
        state['summary'] = summary
        self._commit(state)
        return dict(summary)
