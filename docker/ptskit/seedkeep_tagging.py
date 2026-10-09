"""Exact editable tag scope and conservative, deduplicated local seedkeep counters."""
import re

DEFAULT_TAG = 'pts保种组'
_HASH = re.compile(r'[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?\Z')
STATES = ('valid', 'invalid', 'unknown', 'inactive', 'downloading')


class TaggingError(ValueError):
    pass


def tag_value(value):
    if not isinstance(value, str):
        raise TaggingError('管理标签必须是文本')
    value = value.strip()
    if not 1 <= len(value) <= 128 or ',' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TaggingError('管理标签须为 1–128 个字符的单个标签，不能包含逗号或控制字符')
    return value


def has_tag(torrent, kind, tag):
    value = torrent.get('tags') if kind == 'qb' else torrent.get('labels')
    if kind == 'qb':
        if not isinstance(value, str):
            raise TaggingError('下载器未提供可确认的任务标签')
        labels = [part.strip() for part in value.split(',')]
    else:
        if not isinstance(value, list) or any(not isinstance(part, str) for part in value):
            raise TaggingError('下载器未提供可确认的任务标签')
        labels = [part.strip() for part in value]
    return tag in labels


def _record_aliases(state):
    groups, lookup = [], {}
    for bucket in ('accepted', 'pending', 'unconfirmed'):
        records = state.get(bucket, {})
        if not isinstance(records, dict):
            raise TaggingError('历史身份资料无效')
        for record in records.values():
            if not isinstance(record, dict) or not isinstance(record.get('hashes', []), list):
                raise TaggingError('历史身份资料无效')
            values = record.get('hashes', []) + [record.get('hash', '')]
            if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in values):
                raise TaggingError('历史身份资料无效')
            values = {value.lower() for value in values}
            _merge(groups, lookup, values, None)
    return {value: group['aliases'] for value, group in lookup.items()}


def _merge(groups, lookup, values, member):
    matches = []
    for value in values:
        existing = lookup.get(value)
        if existing is not None and all(existing is not other for other in matches):
            matches.append(existing)
    if matches:
        group = matches[0]
        for other in matches[1:]:
            group['aliases'].update(other['aliases'])
            group['members'].extend(other['members'])
            groups.remove(other)
    else:
        group = {'aliases': set(), 'members': []}
        groups.append(group)
    group['aliases'].update(values)
    group['members'].append(member)
    for value in group['aliases']:
        lookup[value] = group


def inventory_tasks(torrents):
    torrents = list(torrents.values()) if isinstance(torrents, dict) else torrents
    if not isinstance(torrents, list):
        raise TaggingError('下载器库存响应无效')
    result = []
    for task in torrents:
        if not isinstance(task, dict):
            raise TaggingError('下载器库存响应无效')
        copies = task.get('_seedkeep_copies', [task])
        if not isinstance(copies, list) or not copies or any(not isinstance(copy, dict) for copy in copies):
            raise TaggingError('下载器库存响应无效')
        result.extend(copies)
    return result


def _inventory_groups(state, qb, tr, tag):
    tag = tag_value(tag)
    history = _record_aliases(state)
    groups, lookup = [], {}
    for kind, torrents in (('qb', inventory_tasks(qb)), ('tr', inventory_tasks(tr))):
        for torrent in torrents:
            key = torrent.get('hash' if kind == 'qb' else 'hashString')
            if not isinstance(key, str) or not _HASH.fullmatch(key):
                raise TaggingError('下载器种子身份无效')
            values = {key.lower()}
            if kind == 'qb':
                for name in ('infohash_v1', 'infohash_v2'):
                    value = torrent.get(name)
                    if value not in (None, ''):
                        if not isinstance(value, str) or not _HASH.fullmatch(value):
                            raise TaggingError('下载器种子身份无效')
                        values.add(value.lower())
            for value in tuple(values):
                values.update(history.get(value, ()))
            _merge(groups, lookup, values, {'kind': kind, 'selected': has_tag(torrent, kind, tag)})
    return groups, lookup, history


def _counts(groups):
    result = dict.fromkeys(('managed_active', 'managed_qb', 'managed_tr', 'managed_both'), 0)
    for group in groups:
        kinds = {member['kind'] for member in group['members'] if member['selected']}
        if kinds:
            result['managed_active'] += 1
            result['managed_qb'] += 'qb' in kinds
            result['managed_tr'] += 'tr' in kinds
            result['managed_both'] += len(kinds) == 2
    return result


def inventory_counts(state, qb, tr, tag):
    """Count all tagged tasks, including unrecorded/downloading/inactive tasks."""
    return _counts(_inventory_groups(state, qb, tr, tag)[0])


def inventory_budget(state, qb, tr, tag):
    groups, live, history = _inventory_groups(state, qb, tr, tag)
    pending = {min(history[record['hash'].lower()]) for record in state.get('pending', {}).values()
               if not history[record['hash'].lower()].intersection(live)}
    return {**_counts(groups), 'pending_reserved': len(pending)}


def unknown_summary(tag, checked_at=None, limit=None):
    return {'tag': tag, 'connected': False, 'checked_at': checked_at, 'seeders_max': limit,
            **dict.fromkeys(('total', 'qb', 'tr', 'both') + STATES)}


def summarize_rows(rows, instances, tag, checked_at, limit=None, state=None):
    """Project existing local validity judgements without observing/destructive writes."""
    tag = tag_value(tag)
    history = _record_aliases(state or {})
    enabled = {row['id']: row for row in instances if row['enabled']}
    if any(not row['connected'] for row in enabled.values()):
        return unknown_summary(tag, checked_at, limit)
    result = {'tag': tag, 'connected': True, 'checked_at': checked_at, 'seeders_max': limit,
              **dict.fromkeys(('total', 'qb', 'tr', 'both') + STATES, 0)}
    groups, lookup = [], {}
    for row in rows:
        if row['instance_id'] not in enabled:
            continue
        raw = row['_qb'] if row['client'] == 'qb' else row['_tr']
        try:
            selected = has_tag(raw, row['client'], tag)
        except TaggingError:
            return unknown_summary(tag, checked_at, limit)
        if not selected:
            continue
        values = set(row.get('_aliases', (row['hash'],)))
        if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in values):
            return unknown_summary(tag, checked_at, limit)
        values = {value.lower() for value in values}
        for value in tuple(values):
            values.update(history.get(value, ()))
        _merge(groups, lookup, values, row)
    for group in groups:
        members = group['members']
        kinds = {row['client'] for row in members}
        states = {row.get('_seeding_validity', 'invalid' if row.get('unregistered') is True else row['validity']) for row in members}
        if 'invalid' in states and 'valid' in states:
            state = 'unknown'
        elif 'invalid' in states:
            state = 'invalid'
        elif 'valid' in states:
            state = 'valid'
        elif 'downloading' in states:
            state = 'downloading'
        elif states == {'inactive'}:
            state = 'inactive'
        else:
            state = 'unknown'
        result['total'] += 1
        result[state] += 1
        result['qb'] += 'qb' in kinds
        result['tr'] += 'tr' in kinds
        result['both'] += len(kinds) == 2
    return result


DISPLAY_COUNTS = ('completed_total', 'valid', 'invalid', 'unknown', 'inactive', 'downloading')


def unknown_display(tag, checked_at=None, limit=None):
    return {'tag': tag, 'connected': False, 'checked_at': checked_at, 'seeders_max': limit,
            **dict.fromkeys(DISPLAY_COUNTS), 'completion_unknown': None, 'instances': []}


def _display_state(row):
    # This display projection never grants transfer/deletion permission.
    import math
    from seedkeep_downloaders import task_state
    kind = row['client']
    raw = row['_qb'] if kind == 'qb' else row['_tr']
    mapped = task_state(kind, raw)
    if kind == 'qb':
        state = raw.get('state')
        complete_states = ('uploading', 'stalledUP', 'forcedUP', 'checkingUP',
                           'stoppedUP', 'pausedUP', 'queuedUP')
        incomplete_states = ('downloading', 'stalledDL', 'forcedDL', 'metaDL',
                             'forcedMetaDL', 'queuedDL', 'stoppedDL', 'pausedDL', 'checkingDL')
        if state not in complete_states + incomplete_states:
            return None, False
        progress = raw.get('progress')
        if progress is not None and (type(progress) not in (int, float) or
                not math.isfinite(progress) or not 0 <= progress <= 1 or
                (state in complete_states and progress < 1)):
            return None, False
    else:
        progress, state = raw.get('percentDone'), raw.get('status')
        if (type(progress) not in (int, float) or not math.isfinite(progress) or
                not 0 <= progress <= 1 or type(state) is not int or state not in range(7)):
            return None, False
    complete = mapped['completed']
    groups = set(mapped['state_groups'])
    downloading = not complete and 'downloading' in groups and not groups.intersection(
        ('paused', 'checking', 'error', 'moving', 'unknown'))
    return complete, downloading


def _display_counts(groups):
    result = dict.fromkeys(DISPLAY_COUNTS, 0)
    result['completion_unknown'] = 0
    for group in groups:
        members = [row for row in group['members'] if row['_display_selected']]
        if not members:
            continue
        completed = [row for row in members if row['_display_complete'] is True]
        if completed:
            # Completed copies take precedence over unfinished copies of the same identity.
            states = {row.get('_seeding_validity', 'invalid' if row.get('unregistered') is True
                              else row.get('validity', 'unknown')) for row in completed}
            validity = ('unknown' if {'valid', 'invalid'} <= states else
                        'invalid' if 'invalid' in states else 'valid' if 'valid' in states else
                        'inactive' if states == {'inactive'} else 'unknown')
            result['completed_total'] += 1
            result[validity] += 1
        elif any(row['_display_complete'] is None for row in members):
            result['completion_unknown'] += 1
        elif any(row['_display_downloading'] for row in members):
            result['downloading'] += 1
    if result['completion_unknown']:
        # A state we cannot classify may be complete or downloading; partial counts aren't exact.
        result.update(dict.fromkeys(DISPLAY_COUNTS))
    return result


def summarize_display(rows, instances, tag, checked_at, limit=None, state=None):
    """Read-only completed seeding and actual downloads; execution inventory stays unchanged."""
    tag = tag_value(tag)
    history = _record_aliases(state or {})
    enabled = {item['id']: item for item in instances if item['enabled']}
    groups, lookup, by_instance = [], {}, {iid: [] for iid in enabled}
    invalid_instances = set()
    for parent in rows:
        iid = parent['instance_id']
        if iid not in enabled or not enabled[iid]['connected']:
            continue
        for original in parent.get('_display_members', [parent]):
            row = dict(original)
            try:
                raw = row['_qb'] if row['client'] == 'qb' else row['_tr']
                selected = has_tag(raw, row['client'], tag)
                values = set(row.get('_aliases', (row['hash'],)))
                if not values or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in values):
                    raise TaggingError('下载器种子身份无效')
                values = {value.lower() for value in values}
                for value in tuple(values):
                    values.update(history.get(value, ()))
                complete, downloading = _display_state(row) if selected else (False, False)
            except (TaggingError, KeyError, TypeError, ValueError):
                invalid_instances.add(iid)
                continue
            row.update(_display_selected=selected, _display_complete=complete, _display_downloading=downloading)
            _merge(groups, lookup, values, row)
            by_instance[iid].append((values, row))
    result = {'tag': tag, 'connected': not invalid_instances and all(item['connected'] for item in enabled.values()),
              'checked_at': checked_at, 'seeders_max': limit, **_display_counts(groups), 'instances': []}
    if not result['connected']:
        result.update(dict.fromkeys(DISPLAY_COUNTS + ('completion_unknown',)))
    for item in instances:
        iid = item['id']
        available = item['enabled'] and item['connected'] and iid not in invalid_instances
        local_groups, local_lookup = [], {}
        for values, row in by_instance.get(iid, []):
            _merge(local_groups, local_lookup, values, row)
        result['instances'].append({'instance_id': iid, 'name': item.get('name', ''), 'type': item.get('type'),
            'enabled': item['enabled'], 'connected': bool(available), 'checked_at': checked_at,
            **(_display_counts(local_groups) if available else dict.fromkeys(DISPLAY_COUNTS + ('completion_unknown',)))})
    return result


def log_counters(summary):
    return {'seeding_' + key: summary[key] for key in ('total',) + STATES
            if type(summary.get(key)) is int and summary[key] >= 0}


def merge_labels(existing, additions):
    if isinstance(existing, str):
        existing = [part.strip() for part in existing.split(',') if part.strip()]
    if not isinstance(existing, list) or any(not isinstance(part, str) for part in existing):
        raise TaggingError('任务标签响应无效')
    return list(dict.fromkeys(existing + additions))
