from __future__ import annotations

import datetime
import re
import time
from collections import Counter

from reporter import ReportError

OUTBOX_KEY = 'ms_notice_outbox'
STATUS_KEY = 'ms_notice_status'
MAX_CONTENT_BYTES = 1800
HISTORY_LIMIT = 50
STATES = {
    'reported': '上报确认', 'existing': '云端已有', 'pending': '等待处理',
    'retry': '待重试', 'uncertain': '待回查', 'inflight': '待回查',
    'failed': '失败', 'blocked': '受阻',
}
TERMINAL = {'sent', 'failed', 'uncertain'}
NAS_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))


def readable_size(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'):
        if size < 1024 or unit == 'EiB':
            return f'{size:.2f} {unit}' if unit != 'B' else f'{size:.0f} B'
        size /= 1024


def _filename(value):
    # Keep each file on one line and prevent channel HTML from treating a name
    # as markup. Full names remain available in the private dashboard history.
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value).strip()
    return value.translate(str.maketrans({'<': '＜', '>': '＞', '&': '＆'})) or '(未命名)'


def _shorten(value, byte_limit):
    if len(value.encode('utf-8')) <= byte_limit:
        return value
    return value.encode('utf-8')[:byte_limit - 3].decode('utf-8', 'ignore') + '…'


def format_batch_notices(batch, rows, summary):
    """Describe final item states, not a generic native-plugin success log."""
    if not rows:
        return []
    counts = Counter(row['state'] for row in rows)
    finished_at = datetime.datetime.fromtimestamp(summary['updated_at'], NAS_TIMEZONE)
    total_size = sum(row['size'] for row in rows)
    header = (
        f"批次：{batch['id'][:8]}\n"
        f"完成：{finished_at:%Y-%m-%d %H:%M:%S} UTC+8\n"
        f"本批：{len(rows)} 条，合计 {readable_size(total_size)}\n"
        f"上报确认 {counts['reported']}，云端已有 {counts['existing']}，"
        f"待处理/回查 {sum(counts[k] for k in ('pending', 'retry', 'uncertain', 'inflight'))}，"
        f"失败/受阻 {counts['failed'] + counts['blocked']}\n"
    )
    footer = '\n确认成功项已移出共享 TXT，完整记录保留在管理网页。'
    fixed_bytes = len((header + footer + '\n').encode('utf-8'))
    entries = []
    for index, row in enumerate(rows, 1):
        tail = f"\n大小：{readable_size(row['size'])}（{row['size']:,} 字节）\n"
        prefix = f"\n{index}. [{STATES.get(row['state'], '待确认')}] "
        name_budget = MAX_CONTENT_BYTES - fixed_bytes - len((prefix + tail).encode('utf-8'))
        entries.append(prefix + _shorten(_filename(row['name']), name_budget) + tail)
    pages, current = [], ''
    for entry in entries:
        if current and len((header + current + entry + footer).encode('utf-8')) > MAX_CONTENT_BYTES:
            pages.append(header + current + footer)
            current = ''
        current += entry
    if current:
        pages.append(header + current + footer)
    return [
        {'title': f"TG ED2K 上报明细 · {batch['id'][:8]}" + (f' ({index}/{len(pages)})' if len(pages) > 1 else ''),
         'content': content}
        for index, content in enumerate(pages, 1)
    ]


class BatchNotices:
    """Worker-owned, at-most-once MS notice attempts, independent of uploads.

    enqueue() belongs to the same SQLite transaction that retires the batch.
    A persisted sending marker prevents re-sending a possibly delivered notice
    after a timeout or restart. Terminal jobs retain only a safe status summary.
    """

    def __init__(self, store, send, *, clock=time.time):
        self.store, self.send, self.clock = store, send, clock

    @staticmethod
    def _valid(jobs):
        return isinstance(jobs, list) and all(
            isinstance(job, dict) and isinstance(job.get('id'), str)
            and re.fullmatch(r'[0-9a-f]{32}:[1-9][0-9]*', job['id'])
            and job.get('state') in {'pending', 'sending', *TERMINAL}
            and (job['state'] in TERMINAL or isinstance(job.get('title'), str) and isinstance(job.get('content'), str))
            for job in jobs
        )

    def _status(self, jobs, *, error=None):
        counts = Counter(job['state'] for job in jobs)
        return {'pending': counts['pending'], 'sent': counts['sent'], 'failed': counts['failed'],
                'uncertain': counts['uncertain'], 'updated_at': self.clock(), 'error': error}

    def enqueue(self, batch, rows, summary):
        # No notice for withdrawn, unsubmitted, or entirely pre-existing work.
        if not batch.get('submitted'):
            return
        jobs = self.store.get(OUTBOX_KEY, [])
        if not self._valid(jobs):
            self.store._set(STATUS_KEY, {'error': 'ms_notice_state_invalid', 'updated_at': self.clock()})
            return
        known = {job['id'] for job in jobs}
        for index, message in enumerate(format_batch_notices(batch, rows, summary), 1):
            ident = f"{batch['id']}:{index}"
            if ident not in known:
                jobs.append({'id': ident, 'batch_id': batch['id'], 'state': 'pending',
                             'created_at': self.clock(), 'updated_at': self.clock(), **message})
        self.store._set(OUTBOX_KEY, jobs)
        self.store._set(STATUS_KEY, self._status(jobs))

    def _save(self, jobs, *, error=None):
        waiting = [job for job in jobs if job['state'] not in TERMINAL]
        completed = [job for job in jobs if job['state'] in TERMINAL][-HISTORY_LIMIT:]
        retained = waiting + completed
        with self.store.db:
            self.store._set(OUTBOX_KEY, retained)
            self.store._set(STATUS_KEY, self._status(retained, error=error))

    @staticmethod
    def _retire(job, state, now, error=None):
        job.update(state=state, updated_at=now, error=error)
        job.pop('title', None)
        job.pop('content', None)

    def tick(self, *, stop=None):
        result = {'changed': False, 'sent': 0, 'failed': 0, 'uncertain': 0}
        if stop is not None and stop.is_set():
            return result
        jobs = self.store.get(OUTBOX_KEY, [])
        if not self._valid(jobs):
            with self.store.db:
                self.store._set(STATUS_KEY, {'error': 'ms_notice_state_invalid', 'updated_at': self.clock()})
            return {**result, 'error': 'ms_notice_state_invalid'}
        for job in jobs:
            if job['state'] == 'sending':
                self._retire(job, 'uncertain', self.clock(), 'ms_notice_interrupted')
                result.update(changed=True, uncertain=result['uncertain'] + 1)
        if result['changed']:
            self._save(jobs)
        job = next((job for job in jobs if job['state'] == 'pending'), None)
        if job is None:
            return result
        job.update(state='sending', updated_at=self.clock())
        self._save(jobs)
        state, error = 'sent', None
        try:
            if self.send(job['title'], job['content']) is not True:
                raise ReportError('ms_notice_response_invalid', uncertain=True)
        except ReportError as exc:
            state = 'uncertain' if exc.uncertain else 'failed'
            reason = str(exc)
            error = reason if re.fullmatch(r'ms_notice_[a-z0-9_]+', reason) else 'ms_notice_failed'
        except Exception:
            # Never propagate a notification failure into resource dispatch or
            # expose filenames, addresses, or credentials through exception text.
            state, error = 'uncertain', 'ms_notice_failed'
        self._retire(job, state, self.clock(), error)
        self._save(jobs, error=error)
        result.update(changed=True)
        result[state] += 1
        return result
