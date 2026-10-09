from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request

from reporter import MAX_RESPONSE_BYTES, ReportError

COMPLETION_HEADER = 'ED2K HASH 上报完成'
COMPLETION_FIELDS = {'总行数', '有效条目', '已上报', '云端已存在', '未识别', '解析失败', '失败'}
OBSERVER_KEY = 'ms_plugin_observer'


def parse_completion(row, instance_id):
    """A batch end witness, never a per-file success receipt."""
    if not isinstance(row, dict) or row.get('code') != f'plugin_instance_{instance_id}':
        return None
    if type(row.get('id')) is not int or row['id'] < 1:
        return None
    if not isinstance(row.get('caller'), str) or not re.fullmatch(r'plugin/ed2k_hash_reporter\.go:[0-9]+', row['caller']):
        return None
    message = row.get('msg')
    if not isinstance(message, str):
        return None
    lines = message.replace('\r\n', '\n').strip().split('\n')
    if len(lines) != 8 or lines[0] != COMPLETION_HEADER:
        return None
    result = {}
    for line in lines[1:]:
        match = re.fullmatch(r'([^:：]+)[:：]\s*([0-9]{1,18})\s*', line)
        if not match or match[1] not in COMPLETION_FIELDS or match[1] in result:
            return None
        result[match[1]] = int(match[2])
    if set(result) != COMPLETION_FIELDS or result['有效条目'] > result['总行数']:
        return None
    return result


class MsPluginClient:
    def __init__(self, reporter, instance_id, *, store, queue_file='/downloads/.tg-ed2k-queue/normalized.txt', api=None, clock=time.time):
        if type(instance_id) is not int or instance_id < 1:
            raise ReportError('ms_plugin_instance_invalid')
        self.reporter, self.instance_id, self.store = reporter, instance_id, store
        self.queue_file, self.api_override, self.clock = queue_file, api, clock
        self.last_error = None

    def _get(self, path):
        request = urllib.request.Request(self.reporter.ms_url + path, headers={'Authorization': 'Bearer ' + self.reporter.key, 'Accept': 'application/json'}, method='GET')
        try:
            try:
                response = self.reporter.local.open(request, timeout=self.reporter.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if response.code in (401, 403):
                    raise ReportError('ms_auth_failed', auth=True)
                if response.code == 429 or response.code >= 500:
                    raise ReportError('ms_plugin_http_' + str(response.code), transient=True)
                if response.code != 200:
                    raise ReportError('ms_plugin_http_' + str(response.code))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ReportError('ms_plugin_response_too_large', transient=True)
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
            raise ReportError('ms_plugin_network_error', transient=True) from None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ReportError('ms_plugin_response_invalid', transient=True) from None
        if not isinstance(body, dict) or type(body.get('code')) is not int:
            raise ReportError('ms_plugin_response_invalid', transient=True)
        if body['code'] != 20000:
            raise ReportError('ms_plugin_business_' + str(body['code']), transient=body['code'] >= 50000)
        return body.get('data')

    def _api(self, path, payload=None, *, write=False):
        if self.api_override is not None:
            return self.api_override(path, payload)
        if payload is None:
            return self._get(path)
        return self.reporter.request(path, payload, remote=False, write=write)

    def _validate(self):
        detail = self._api('/api/v1/pluginsInstance/detail/' + str(self.instance_id))
        if not isinstance(detail, dict) or detail.get('id') != self.instance_id or detail.get('code') != 'ed2k_hash_reporter' or detail.get('enabled') is not True:
            raise ReportError('ms_plugin_instance_changed')
        config = detail.get('config')
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                raise ReportError('ms_plugin_config_invalid') from None
        if not isinstance(config, dict) or config.get('driverName') != self.reporter.driver or config.get('listFilePath') != self.queue_file or config.get('cron') not in ('', None) or config.get('ed2kText') not in ('', None) or config.get('notice') is not True:
            raise ReportError('ms_plugin_config_changed')

    def _logs(self):
        result = self._api('/api/v1/logs/page?pageNum=1&pageSize=200', {'keyword': '', 'level': '', 'module': '', 'code': f'plugin_instance_{self.instance_id}'})
        if not isinstance(result, dict) or not isinstance(result.get('list'), list):
            raise ReportError('ms_plugin_logs_invalid', transient=True)
        return [row for row in result['list'] if isinstance(row, dict) and row.get('code') == f'plugin_instance_{self.instance_id}' and type(row.get('id')) is int and row['id'] > 0]

    def run(self):
        self._validate()
        rows = self._logs()
        batch = self.store.get('plugin_batch') or {}
        observer = {'instance_id': self.instance_id, 'batch_id': batch.get('id'), 'checkpoint': max((row['id'] for row in rows), default=0), 'dispatched_at': self.clock(), 'finished': False}
        # Save the log boundary before the mutating request. A timeout is not
        # permission to send it again, including after a reporter restart.
        self.store.set(OBSERVER_KEY, observer)
        self._api('/api/v1/pluginsInstance/call/' + str(self.instance_id), {'action': 'run'}, write=True)
        return {'completed': False}

    def running(self):
        try:
            self._validate()
            rows = self._logs()
            observer = self.store.get(OBSERVER_KEY)
            if observer is None:
                # This dedicated instance has no Cron or direct text input.
                # Provisioning and the initial controlled probe are complete
                # before the sole queue worker can own it.
                return False if not rows or parse_completion(max(rows, key=lambda row: row['id']), self.instance_id) is not None else None
            if not isinstance(observer, dict) or observer.get('instance_id') != self.instance_id or type(observer.get('checkpoint')) is not int:
                self.last_error = 'ms_plugin_observer_invalid'
                return None
            completions = [(row['id'], parse_completion(row, self.instance_id)) for row in rows if row['id'] > observer['checkpoint']]
            completions = [(ident, summary) for ident, summary in completions if summary is not None]
            if completions:
                ident, summary = max(completions, key=lambda entry: entry[0])
                observer.update(finished=True, terminal_log_id=ident, summary=summary, confirmed_at=self.clock())
                self.store.set(OBSERVER_KEY, observer)
                self.last_error = None
                return False
            if observer.get('finished') is True:
                self.last_error = None
                return False
            # No matching end witness: preserve the frozen batch. Duration or
            # a generic API success cannot prove that the plugin is finished.
            self.last_error = None
            return True
        except ReportError as exc:
            self.last_error = str(exc)
            if exc.auth:
                self.store.set('report_pause', {'reason': 'auth_failed', 'at': self.clock()})
            return None
