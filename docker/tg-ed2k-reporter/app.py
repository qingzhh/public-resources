from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from collector import Collector, FetchError, Telegram
from ed2k import Message, normalize
from state import Store, run_lock
from reporter import MsReporter, ReportError, report_batch
from dashboard import Auth, Control, ReportStop, WebError, WebServer, create_app, parse_item_id, record_event

SETTINGS_PATH = "/config/settings.json"
SECRETS_PATH = "/secrets/report.json"
DATA_PATH = "/data"
MAX_INPUT_BYTES = 16 * 1024 * 1024
BOUNDS = {
    "initial_days": (1, 90), "poll_seconds": (60, 86400),
    "max_pages_per_cycle": (1, 200), "edit_pages": (0, 10),
    "telegram_spacing_seconds": (0.5, 30), "http_timeout_seconds": (5, 120),
    "max_reports_per_cycle": (1, 100), "report_spacing_seconds": (1, 120),
    "max_attempts": (1, 20), "retry_base_seconds": (60, 86400),
    "retry_cap_seconds": (60, 604800),
}


class ConfigError(RuntimeError):
    pass


def load_settings(path):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigError("settings_unreadable") from None
    if not isinstance(values, dict) or set(values) != set(BOUNDS) | {"channels", "report_enabled"}:
        raise ConfigError("settings_fields_invalid")
    channels = values["channels"]
    if not isinstance(channels, list) or not channels or len(channels) > 10:
        raise ConfigError("channels_invalid")
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,64}", name) for name in channels):
        raise ConfigError("channel_name_invalid")
    if len(set(channels)) != len(channels):
        raise ConfigError("channels_invalid")
    if type(values["report_enabled"]) is not bool:
        raise ConfigError("report_enabled_invalid")
    for key, (low, high) in BOUNDS.items():
        if type(values[key]) not in (int, float) or not low <= values[key] <= high:
            raise ConfigError("settings_range_invalid:" + key)
        if key not in ("telegram_spacing_seconds", "report_spacing_seconds") and type(values[key]) is not int:
            raise ConfigError("settings_integer_required:" + key)
    if values["retry_cap_seconds"] < values["retry_base_seconds"]:
        raise ConfigError("retry_range_invalid")
    return values


def load_secrets(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigError("report_credentials_unreadable") from None
    if not isinstance(value, dict):
        raise ConfigError("report_credentials_invalid")
    return value


def get_proxy():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not proxy or urlsplit(proxy).scheme not in ("http", "https") or not urlsplit(proxy).hostname:
        raise ConfigError("telegram_proxy_required")
    return proxy


def safe_log(event, **values):
    print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **values}, ensure_ascii=False), flush=True)


def read_input(path):
    with Path(path).open("rb") as stream:
        value = stream.read(MAX_INPUT_BYTES + 1)
    if len(value) > MAX_INPUT_BYTES:
        raise ValueError("input_too_large")
    return value.decode("utf-8-sig")


def import_file(store, path, now):
    text = read_input(path)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ident = int(digest[:15], 16)
    return store.ingest([Message("inbox", ident, now, text)], now)


class Service:
    def __init__(self, store, settings, secrets, proxy, *, stop=None, control=None):
        self.store, self.settings, self.control = store, settings, control
        self.stop = stop if stop is not None else threading.Event()
        self.next_cycle = 0
        if control is not None and store.get('report_manual_pause'):
            control.pause_requested.set()
        self.telegram = Telegram(proxy, timeout=settings["http_timeout_seconds"], spacing=settings["telegram_spacing_seconds"], sleep=self.pause)
        original_fetch = self.telegram.fetch

        def fetch(*args, **kwargs):
            if self.stop.is_set():
                raise InterruptedError("shutdown")
            return original_fetch(*args, **kwargs)

        self.telegram.fetch = fetch
        self.collector = Collector(store, self.telegram, settings)
        self.reporter = MsReporter(secrets, timeout=settings["http_timeout_seconds"], proxy=proxy) if settings["report_enabled"] else None
        self.fingerprint = hashlib.sha256(json.dumps(secrets, sort_keys=True).encode()).hexdigest()
        if self.reporter is not None and store.get("credentials_fingerprint") != self.fingerprint:
            store.set("credentials_fingerprint", self.fingerprint)
            store.set("report_pause", None)
        store.set("poll_seconds", settings["poll_seconds"])

    def pause(self, seconds):
        if self.stop.wait(seconds):
            raise InterruptedError("shutdown")

    def cycle(self, *, collect_only=False, report_limit=None):
        summary = {"collect": [], "inbox": {"new": 0, "invalid": 0}, "report": {}}
        self.store.set("heartbeat", time.time())
        for channel in self.settings["channels"]:
            try:
                result = self.collector.collect(channel)
                summary["collect"].append({"channel": channel, **result})
            except FetchError as exc:
                summary["collect"].append({"channel": channel, "error": str(exc)})
        for path in sorted(Path("/inbox").glob("*.txt")):
            if self.stop.is_set():
                raise InterruptedError("shutdown")
            try:
                counts = import_file(self.store, path, time.time())
                summary["inbox"]["new"] += counts["new"]
                summary["inbox"]["invalid"] += counts["invalid"]
            except (OSError, ValueError, UnicodeError):
                safe_log("inbox_read_error")
        self.store.export(self.store.directory / "normalized.txt")
        if self.reporter is not None and not collect_only and not self.stop.is_set():
            if self.store.get('report_manual_pause'):
                summary['report'] = {'paused': True}
            else:
                report_stop = ReportStop(self.stop, self.control) if self.control is not None else self.stop
                summary['report'] = report_batch(self.store, self.reporter, self.settings, limit=report_limit, stop=report_stop, sleep=self.report_wait)
        self.store.set("last_cycle", {"at": time.time(), **summary})
        if self.control is not None:
            record_event(self.store, 'cycle', summary)
        self.store.set("heartbeat", time.time())
        safe_log("cycle", **summary)
        return summary

    def report_wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop.is_set():
                raise InterruptedError('shutdown')
            if self.control is not None and self.control.pause_requested.is_set():
                return
            self.stop.wait(min(0.25, max(0, deadline - time.monotonic())))

    def scheduled_cycle(self):
        started = time.monotonic()
        if self.control is not None:
            self.control.cycle(True)
        try:
            return self.cycle()
        finally:
            self.next_cycle = time.monotonic() + max(1, self.settings['poll_seconds'] - (time.monotonic() - started))
            if self.control is not None:
                self.control.cycle(False, next_at=time.time() + max(0, self.next_cycle - time.monotonic()))

    def commands(self):
        if self.control is None:
            return
        for _ in range(16):
            if self.stop.is_set():
                return
            job = self.control.pop()
            if job is None:
                return
            action, values = job['action'], job['_values']
            try:
                if action == 'collect_now':
                    result = self.scheduled_cycle()
                elif action == 'pause_reporting':
                    self.control.pause_requested.set()
                    self.store.set('report_manual_pause', {'reason': 'manual', 'at': time.time()})
                    result = {'paused': True}
                elif action == 'resume_reporting':
                    self.store.set('report_manual_pause', None)
                    self.control.pause_requested.clear()
                    self.next_cycle = 0
                    result = {'paused': bool(self.store.get('report_pause'))}
                elif action == 'retry_failed':
                    item = parse_item_id(values['item_id']) if values.get('item_id') else None
                    count = self.store.retry_failed(time.time(), item=item)
                    if item is not None and not count:
                        raise WebError('item_not_retryable')
                    if count:
                        self.next_cycle = 0
                    result = {'requeued': count}
                elif action == 'import':
                    text = values['text']
                    ident = int(hashlib.sha256(text.encode('utf-8')).hexdigest()[:15], 16)
                    now = time.time()
                    result = self.store.ingest([Message('web', ident, now, text)], now)
                    self.store.export(self.store.directory / 'normalized.txt')
                    if result['new']:
                        self.next_cycle = 0
                else:
                    raise WebError('action_invalid')
                record_event(self.store, 'import' if action == 'import' else 'action', {'action': action, **result})
                self.control.finish(job, result=result)
            except InterruptedError:
                self.control.finish(job, error='service_stopped')
                raise
            except Exception as exc:
                reason = exc.reason if isinstance(exc, WebError) else 'management_error'
                self.control.finish(job, error=reason)
                safe_log('management_error', action=action, error_type=type(exc).__name__)
            self.store.set('heartbeat', time.time())

    def run(self):
        self.store.recover(time.time())
        while not self.stop.is_set():
            try:
                self.commands()
                if self.stop.is_set():
                    break
                if time.monotonic() >= self.next_cycle:
                    self.scheduled_cycle()
            except InterruptedError:
                break
            self.store.set('heartbeat', time.time())
            wait = min(30, max(0, self.next_cycle - time.monotonic()))
            if self.control is None:
                self.stop.wait(wait)
            else:
                self.control.cycle(False, next_at=time.time() + max(0, self.next_cycle - time.monotonic()))
                self.control.wake.wait(min(1, wait))
        safe_log('stopped')


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="独立 Telegram ED2K 采集与 HASH 上报")
    parser.add_argument("--settings", default=SETTINGS_PATH)
    parser.add_argument("--secrets", default=SECRETS_PATH)
    parser.add_argument("--data", default=DATA_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ('collect', 'status', 'health', 'retry-failed'):
        commands.add_parser(name)
    daemon = commands.add_parser('run')
    daemon.add_argument('--web', action='store_true')
    daemon.add_argument('--web-host', default='0.0.0.0')
    daemon.add_argument('--web-port', type=int, default=8890)
    daemon.add_argument('--web-auth', default='/secrets/web.json')
    once = commands.add_parser("once")
    once.add_argument("--report-limit", type=int, default=None)
    incoming = commands.add_parser("import")
    incoming.add_argument("--input", required=True)
    clean = commands.add_parser("normalize")
    clean.add_argument("--input", required=True)
    clean.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "normalize":
        result = normalize(read_input(args.input))
        with Path(args.output).open("w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(link.normalized for link in result.links) + ("\n" if result.links else ""))
        print(json.dumps({"valid": len(result.links), "invalid": len(result.errors), "repaired": sum(item.repaired for item in result.links)}))
        return 0 if not result.errors else 2
    if args.command in ("status", "health"):
        store = Store(args.data, readonly=True)
        try:
            status = store.status()
            if args.command == "status":
                print(json.dumps(status, ensure_ascii=False))
                return 0
            heartbeat = status["heartbeat"]
            if not heartbeat or not 0 <= time.time() - heartbeat <= 180:
                return 1
            port = store.get('web_port')
            if port is not None:
                from urllib.request import ProxyHandler, build_opener
                try:
                    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/api/session', timeout=3) as response:
                        if response.status != 200:
                            return 1
                except OSError:
                    return 1
            return 0
        finally:
            store.close()
    with run_lock(args.data):
        store = Store(args.data)
        server = None
        try:
            if args.command == "import":
                print(json.dumps(import_file(store, args.input, time.time())))
                store.export(store.directory / "normalized.txt")
                return 0
            if args.command == "retry-failed":
                print(json.dumps({"requeued": store.retry_failed(time.time())}))
                return 0
            settings = load_settings(args.settings)
            if args.command == "collect":
                settings["report_enabled"] = False
            secrets = load_secrets(args.secrets) if settings["report_enabled"] else {}
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            control = Control() if args.command == 'run' and args.web else None
            service = Service(store, settings, secrets, get_proxy(), stop=stop, control=control)
            if args.command == 'run':
                if args.web:
                    if not 1 <= args.web_port <= 65535:
                        raise ConfigError('web_port_invalid')
                    auth = Auth(args.web_auth, args.data)
                    server = WebServer(create_app(args.data, settings, auth, control), args.web_host, args.web_port, stop, control)
                    store.set('web_port', args.web_port)
                    server.start()
                else:
                    store.set('web_port', None)
                service.run()
            else:
                if args.command == "once" and args.report_limit is not None and args.report_limit < 1:
                    raise ConfigError("report_limit_must_be_positive")
                store.recover(time.time())
                service.cycle(collect_only=args.command == "collect", report_limit=getattr(args, "report_limit", None))
            return 0
        finally:
            if server is not None:
                stop.set()
                server.close()
            store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, ReportError, WebError) as exc:
        safe_log("configuration_error", reason=str(exc))
        raise SystemExit(2)
    except InterruptedError:
        raise SystemExit(0)
    except Exception as exc:
        # Avoid dumping URLs, authentication headers, or data in tracebacks.
        safe_log("fatal", error_type=type(exc).__name__)
        raise SystemExit(1)
