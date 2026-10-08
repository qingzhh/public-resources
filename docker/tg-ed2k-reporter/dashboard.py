from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ed2k import normalize
from state import Store

MAX_WEB_TEXT_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 2 * MAX_WEB_TEXT_BYTES + 65536
STATES = {'pending', 'retry', 'inflight', 'uncertain', 'reported', 'existing', 'failed', 'blocked'}
ACTIONS = {'collect_now', 'pause_reporting', 'resume_reporting', 'retry_failed', 'import'}
SAFE_SETTINGS = ('channels', 'initial_days', 'poll_seconds', 'max_reports_per_cycle', 'report_spacing_seconds', 'max_attempts', 'report_enabled')
COOKIE_NAME = 'ed2k_session'


class WebError(RuntimeError):
    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason, self.status = reason, status


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def password_record(username, password):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{2,64}', username):
        raise WebError('username_invalid')
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or password.isspace():
        raise WebError('password_length_invalid')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return {'username': username, 'salt': base64.urlsafe_b64encode(salt).decode('ascii'), 'password_hash': base64.urlsafe_b64encode(digest).decode('ascii')}


def validate_record(value):
    try:
        if not isinstance(value, dict) or set(value) != {'username', 'salt', 'password_hash'}:
            raise ValueError()
        if not isinstance(value['username'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{2,64}', value['username']):
            raise ValueError()
        salt = base64.b64decode(value['salt'], altchars=b'-_', validate=True)
        digest = base64.b64decode(value['password_hash'], altchars=b'-_', validate=True)
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise WebError('web_credentials_invalid', 503) from None
    return salt, digest


class Auth:
    def __init__(self, config, directory, *, clock=time.time):
        try:
            if isinstance(config, (str, Path)):
                path = Path(config)
                if path.stat().st_size > 8192:
                    raise ValueError()
                config = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(config, dict) or set(config) - {'username', 'salt', 'password_hash', 'allowed_hosts', 'secure_cookie'}:
                raise ValueError()
            record = {key: config[key] for key in ('username', 'salt', 'password_hash')}
            validate_record(record)
            hosts = config.get('allowed_hosts')
            if not isinstance(hosts, list) or not hosts or len(hosts) > 20:
                raise ValueError()
            checked = set()
            for host in hosts:
                if not isinstance(host, str) or len(host) > 253:
                    raise ValueError()
                try:
                    checked.add(str(ipaddress.ip_address(host)).lower())
                except ValueError:
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
                        raise ValueError()
                    checked.add(host.lower())
            secure = config.get('secure_cookie', False)
            if type(secure) is not bool:
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            raise WebError('web_configuration_invalid', 503) from None
        self.allowed_hosts = checked | {'127.0.0.1', 'localhost', '::1'}
        self.secure_cookie, self.clock = secure, clock
        self.path = Path(directory) / '.web-auth.json'
        self.lock = threading.RLock()
        self.sessions = OrderedDict()
        self.failures = OrderedDict()
        if self.path.exists():
            try:
                if self.path.stat().st_size > 4096:
                    raise ValueError()
                record = json.loads(self.path.read_text(encoding='utf-8'))
                validate_record(record)
                if record['username'] != config['username']:
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise WebError('web_saved_credentials_invalid', 503) from None
        else:
            atomic_json(self.path, record)
        self.record = record
        self.salt, self.digest = validate_record(record)

    def valid_password(self, password):
        if not isinstance(password, str) or len(password) > 128:
            return False
        value = hashlib.scrypt(password.encode('utf-8'), salt=self.salt, n=16384, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(value, self.digest)

    def login(self, username, password, peer):
        with self.lock:
            now = self.clock()
            count, first = self.failures.get(peer, (0, now))
            if now - first >= 300:
                count, first = 0, now
            if count >= 10:
                raise WebError('login_throttled', 429)
            password_valid = self.valid_password(password)
            user_valid = isinstance(username, str) and len(username) <= 64 and hmac.compare_digest(username.encode('utf-8'), self.record['username'].encode('utf-8'))
            if not password_valid or not user_valid:
                self.failures[peer] = (count + 1, first)
                while len(self.failures) > 256:
                    self.failures.popitem(last=False)
                raise WebError('login_failed', 401)
            self.failures.pop(peer, None)
            self._expire()
            token = secrets.token_urlsafe(32)
            session = {'username': self.record['username'], 'csrf_token': secrets.token_urlsafe(32), 'expires': now + 12 * 3600}
            self.sessions[token] = session
            while len(self.sessions) > 64:
                self.sessions.popitem(last=False)
            return token, copy.deepcopy(session)

    def _expire(self):
        now = self.clock()
        for token in list(self.sessions):
            if self.sessions[token]['expires'] <= now:
                del self.sessions[token]

    def session(self, token):
        if not isinstance(token, str) or len(token) > 128:
            return None
        with self.lock:
            self._expire()
            value = self.sessions.get(token)
            return copy.deepcopy(value) if value else None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def change_password(self, current, replacement):
        with self.lock:
            if not self.valid_password(current):
                raise WebError('current_password_invalid', 403)
            record = password_record(self.record['username'], replacement)
            atomic_json(self.path, record)
            self.record = record
            self.salt, self.digest = validate_record(record)
            self.sessions.clear()
            self.failures.clear()


class Control:
    """HTTP threads enqueue commands; only the existing worker mutates SQLite."""
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=16)
        self.jobs = OrderedDict()
        self.wake = threading.Event()
        self.pause_requested = threading.Event()
        self.pending_bytes = 0
        self.closed = False
        self.cycle_running = False
        self.next_cycle_at = None

    @staticmethod
    def public(job):
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith('_')})

    def submit(self, action, **values):
        if action not in ACTIONS:
            raise WebError('action_invalid')
        weight = len(values.get('text', '').encode('utf-8'))
        if weight > MAX_WEB_TEXT_BYTES:
            raise WebError('input_too_large', 413)
        with self.lock:
            if self.closed:
                raise WebError('service_stopping', 503)
            if self.queue.full() or self.pending_bytes + weight > 2 * MAX_WEB_TEXT_BYTES:
                raise WebError('management_queue_full', 503)
            for ident in list(self.jobs):
                job = self.jobs[ident]
                if job['state'] in ('succeeded', 'failed') and (self.clock() - job.get('finished_at', self.clock()) > 3600 or len(self.jobs) >= 128):
                    del self.jobs[ident]
            if len(self.jobs) >= 128:
                raise WebError('management_queue_full', 503)
            job = {'id': secrets.token_hex(16), 'action': action, 'state': 'queued', 'created_at': self.clock(), '_values': values, '_bytes': weight}
            self.jobs[job['id']] = job
            self.pending_bytes += weight
            self.queue.put_nowait(job)
            if action == 'pause_reporting':
                self.pause_requested.set()
            self.wake.set()
            return self.public(job)

    def pop(self):
        with self.lock:
            try:
                job = self.queue.get_nowait()
            except queue.Empty:
                self.wake.clear()
                return None
            job['state'] = 'running'
            job['started_at'] = self.clock()
            return job

    def finish(self, job, *, result=None, error=None):
        with self.lock:
            job['state'] = 'failed' if error else 'succeeded'
            job['finished_at'] = self.clock()
            if error:
                job['error'] = error if re.fullmatch(r'[A-Za-z0-9_:.]+', error) else 'management_error'
            else:
                job['result'] = result or {}
            self.pending_bytes -= job.pop('_bytes', 0)
            job.pop('_values', None)

    def job(self, ident):
        with self.lock:
            value = self.jobs.get(ident)
            if value is None:
                raise WebError('job_not_found', 404)
            return self.public(value)

    def runtime(self):
        with self.lock:
            return {'cycle_running': self.cycle_running, 'next_cycle_at': self.next_cycle_at, 'pause_requested': self.pause_requested.is_set()}

    def cycle(self, running, *, next_at=None):
        with self.lock:
            self.cycle_running, self.next_cycle_at = running, next_at

    def close(self):
        with self.lock:
            self.closed = True
            self.wake.set()
            while True:
                try:
                    job = self.queue.get_nowait()
                except queue.Empty:
                    break
                self.finish(job, error='service_stopped')


class ReportStop:
    def __init__(self, stop, control):
        self.stop, self.control = stop, control

    def is_set(self):
        return self.stop.is_set() or self.control.pause_requested.is_set()


@contextmanager
def read_store(directory):
    store = Store(directory, readonly=True)
    try:
        store.db.execute('BEGIN')
        yield store
    finally:
        store.db.rollback()
        store.close()


def parse_item_id(ident):
    if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}:[1-9][0-9]{0,18}', ident):
        raise WebError('item_id_invalid')
    md4, size = ident.split(':')
    if int(size) > 2**63 - 1:
        raise WebError('item_id_invalid')
    return {'md4': md4, 'size': int(size)}


def catalog(store, *, state='all', q='', page=1, page_size=25):
    if state != 'all' and state not in STATES:
        raise WebError('filter_invalid')
    if not isinstance(q, str) or len(q) > 200 or '\x00' in q:
        raise WebError('search_invalid')
    if type(page) is not int or not 1 <= page <= 1_000_000 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise WebError('pagination_invalid')
    clauses, params = [], []
    if state != 'all':
        clauses.append('state=?')
        params.append(state)
    if q:
        escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        clauses.append("(name LIKE ? ESCAPE '\\' OR md4 LIKE ? ESCAPE '\\')")
        params.extend(['%' + escaped + '%'] * 2)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    total = store.db.execute('SELECT COUNT(*) FROM items' + where, params).fetchone()[0]
    rows = store.db.execute('SELECT * FROM items' + where + ' ORDER BY updated_at DESC,md4,size LIMIT ? OFFSET ?', [*params, page_size, (page - 1) * page_size])
    items = []
    for row in rows:
        value = dict(row)
        value['id'] = f"{value['md4']}:{value['size']}"
        value['size'] = str(value['size'])
        try:
            receipt = json.loads(value['receipt']) if value['receipt'] else None
            value['receipt'] = {key: receipt[key] for key in ('via', 'code', 'status') if key in receipt} if isinstance(receipt, dict) else None
        except (ValueError, TypeError):
            value['receipt'] = None
        source = store.db.execute('SELECT channel,message_id FROM sightings WHERE md4=? AND size=? ORDER BY seen_at DESC LIMIT 1', (row['md4'], row['size'])).fetchone()
        value['source_channel'] = source['channel'] if source else None
        value['source_message_id'] = str(source['message_id']) if source else None
        items.append(value)
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


def input_text(value):
    if not isinstance(value, str):
        raise WebError('input_invalid')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise WebError('input_invalid') from None
    if size > MAX_WEB_TEXT_BYTES:
        raise WebError('input_too_large', 413)
    if not value.strip() or value.count('\n') > 20000 or value.lower().count('ed2k://') > 10000:
        raise WebError('input_invalid')
    return value.removeprefix('\ufeff')


def preview(store, text):
    result = normalize(input_text(text))
    seen = set()
    new = 0
    for link in result.links:
        key = (link.md4, link.size)
        if key not in seen:
            seen.add(key)
            new += store.db.execute('SELECT 1 FROM items WHERE md4=? AND size=?', key).fetchone() is None
    return {'valid': len(result.links), 'new': new, 'duplicates': len(result.links) - new, 'repaired': sum(link.repaired for link in result.links), 'invalid': len(result.errors), 'errors': [{'index': index, 'reason': error.reason} for index, error in enumerate(result.errors[:20], 1)]}


def record_event(store, kind, summary):
    events = store.get('web_events', [])
    if not isinstance(events, list):
        events = []
    events.append({'at': time.time(), 'type': kind, 'summary': summary})
    store.set('web_events', events[-80:])


def create_app(directory, settings, auth, control):
    from flask import Flask, jsonify, request, send_file
    from werkzeug.exceptions import HTTPException

    assets = Path(__file__).with_name('web')
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_JSON_BYTES, JSON_SORT_KEYS=False)

    def session_value():
        return auth.session(request.cookies.get(COOKIE_NAME))

    def payload(*fields):
        if not request.is_json:
            raise WebError('json_required', 415)
        value = request.get_json()
        if not isinstance(value, dict) or set(value) - set(fields):
            raise WebError('request_invalid')
        return value

    @app.before_request
    def protect():
        try:
            host = urlsplit('http://' + request.host).hostname
        except ValueError:
            host = None
        if host is None or host.lower() not in auth.allowed_hosts:
            raise WebError('host_not_allowed', 403)
        if request.method != 'POST' or not request.path.startswith('/api/'):
            if request.path.startswith('/api/') and request.path not in ('/api/session', '/api/login') and session_value() is None:
                raise WebError('authentication_required', 401)
            return
        origin = request.headers.get('Origin')
        if (origin is not None and origin != request.host_url.rstrip('/')) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise WebError('origin_not_allowed', 403)
        if request.path == '/api/login':
            return
        session = session_value()
        if session is None:
            raise WebError('authentication_required', 401)
        token = request.headers.get('X-CSRF-Token', '')
        if len(token) > 128 or not hmac.compare_digest(token.encode('utf-8'), session['csrf_token'].encode('ascii')):
            raise WebError('csrf_invalid', 403)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(WebError)
    def web_error(exc):
        return jsonify(error=exc.reason), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        reasons = {400: 'request_invalid', 404: 'not_found', 405: 'method_not_allowed', 413: 'input_too_large', 415: 'json_required'}
        return jsonify(error=reasons.get(exc.code, 'request_failed')), exc.code

    @app.errorhandler(Exception)
    def server_error(exc):
        # Do not let Flask log tracebacks containing submitted text or credentials.
        print(json.dumps({'event': 'web_error', 'error_type': type(exc).__name__}), flush=True)
        return jsonify(error='service_unavailable'), 503

    @app.get('/')
    def index():
        return send_file(assets / 'index.html')

    @app.get('/<name>')
    def asset(name):
        if name == 'favicon.ico':
            return '', 204
        if name not in ('style.css', 'app.js'):
            raise WebError('not_found', 404)
        return send_file(assets / name)

    @app.get('/api/session')
    def session_info():
        session = session_value()
        return jsonify(authenticated=bool(session), **({key: session[key] for key in ('username', 'csrf_token')} if session else {}))

    @app.post('/api/login')
    def login():
        if request.content_length is not None and request.content_length > 4096:
            raise WebError('request_invalid')
        value = payload('username', 'password')
        token, session = auth.login(value.get('username'), value.get('password'), request.remote_addr or 'unknown')
        response = jsonify(authenticated=True, username=session['username'], csrf_token=session['csrf_token'])
        response.set_cookie(COOKIE_NAME, token, max_age=12 * 3600, httponly=True, secure=auth.secure_cookie, samesite='Strict', path='/')
        return response

    @app.post('/api/logout')
    def logout():
        payload()
        auth.logout(request.cookies.get(COOKIE_NAME))
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE_NAME, path='/', secure=auth.secure_cookie, httponly=True, samesite='Strict')
        return response

    @app.post('/api/password')
    def change_password():
        value = payload('current_password', 'new_password')
        auth.change_password(value.get('current_password'), value.get('new_password'))
        response = jsonify(ok=True, reauthenticate=True)
        response.delete_cookie(COOKIE_NAME, path='/', secure=auth.secure_cookie, httponly=True, samesite='Strict')
        return response

    @app.get('/api/status')
    def status():
        with read_store(directory) as store:
            value = store.status()
            value['counts'] = {name: value['counts'].get(name, 0) for name in sorted(STATES)}
            manual = store.get('report_manual_pause')
            value['manual_pause'] = manual
            value['report_pause'] = value['report_pause'] or manual
            events = store.get('web_events', [])
        runtime = control.runtime()
        if manual:
            runtime['pause_requested'] = False
        return jsonify(status=value, settings={key: settings[key] for key in SAFE_SETTINGS}, runtime=runtime, recent_events=list(reversed(events)))

    @app.get('/api/items')
    def items():
        try:
            page = int(request.args.get('page', '1'))
            page_size = int(request.args.get('page_size', '25'))
        except ValueError:
            raise WebError('pagination_invalid') from None
        with read_store(directory) as store:
            value = catalog(store, state=request.args.get('state', 'all'), q=request.args.get('q', ''), page=page, page_size=page_size)
        return jsonify(value)

    @app.get('/api/export')
    def export():
        destination = Path(directory) / 'normalized.txt'
        if not destination.is_file():
            raise WebError('export_not_ready', 503)
        return send_file(destination, mimetype='text/plain; charset=utf-8', as_attachment=True, download_name='ed2k-normalized.txt', conditional=False, etag=False)

    @app.post('/api/import/preview')
    def import_preview():
        value = payload('text')
        with read_store(directory) as store:
            result = preview(store, value.get('text'))
        return jsonify(result)

    @app.post('/api/import')
    def import_submit():
        value = payload('text')
        text = input_text(value.get('text'))
        return jsonify(job=control.submit('import', text=text)), 202

    @app.post('/api/actions')
    def action():
        value = payload('action', 'item_id')
        name = value.get('action')
        if not isinstance(name, str) or name not in ACTIONS - {'import'}:
            raise WebError('action_invalid')
        item = value.get('item_id')
        if item is not None:
            if name != 'retry_failed':
                raise WebError('action_invalid')
            parse_item_id(item)
        return jsonify(job=control.submit(name, **({'item_id': item} if item is not None else {}))), 202

    @app.get('/api/jobs/<ident>')
    def job(ident):
        if not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise WebError('job_not_found', 404)
        return jsonify(job=control.job(ident))

    return app


class WebServer:
    def __init__(self, application, host, port, stop, control):
        from waitress import create_server
        self.stop, self.control = stop, control
        self.server = create_server(application, host=host, port=port, threads=4, map={}, connection_limit=32, channel_timeout=30, cleanup_interval=5, max_request_header_size=8192, max_request_body_size=MAX_JSON_BYTES)
        self.thread = threading.Thread(target=self.run, name='dashboard-http', daemon=True)

    def run(self):
        try:
            self.server.run()
        finally:
            if not self.stop.is_set():
                self.stop.set()
                self.control.wake.set()

    def start(self):
        self.thread.start()

    def close(self):
        self.control.close()
        self.server.close()
        self.server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)
        self.thread.join(timeout=5)
