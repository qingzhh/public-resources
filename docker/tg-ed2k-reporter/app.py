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
    def __init__(self, store, settings, secrets, proxy, *, stop=None):
        self.store, self.settings = store, settings
        self.stop = stop if stop is not None else threading.Event()
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
            summary["report"] = report_batch(self.store, self.reporter, self.settings, limit=report_limit, stop=self.stop, sleep=self.pause)
        self.store.set("last_cycle", {"at": time.time(), **summary})
        self.store.set("heartbeat", time.time())
        safe_log("cycle", **summary)
        return summary

    def run(self):
        self.store.recover(time.time())
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except InterruptedError:
                break
            wait = max(1, self.settings["poll_seconds"] - (time.monotonic() - started))
            deadline = time.monotonic() + wait
            while not self.stop.is_set() and time.monotonic() < deadline:
                self.store.set("heartbeat", time.time())
                self.stop.wait(min(30, max(0, deadline - time.monotonic())))
        safe_log("stopped")


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="独立 Telegram ED2K 采集与 HASH 上报")
    parser.add_argument("--settings", default=SETTINGS_PATH)
    parser.add_argument("--secrets", default=SECRETS_PATH)
    parser.add_argument("--data", default=DATA_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "collect", "status", "health", "retry-failed"):
        commands.add_parser(name)
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
            return 0 if heartbeat and 0 <= time.time() - heartbeat <= 180 else 1
        finally:
            store.close()
    with run_lock(args.data):
        store = Store(args.data)
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
            service = Service(store, settings, secrets, get_proxy(), stop=stop)
            if args.command == "run":
                service.run()
            else:
                if args.command == "once" and args.report_limit is not None and args.report_limit < 1:
                    raise ConfigError("report_limit_must_be_positive")
                store.recover(time.time())
                service.cycle(collect_only=args.command == "collect", report_limit=getattr(args, "report_limit", None))
            return 0
        finally:
            store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, ReportError) as exc:
        safe_log("configuration_error", reason=str(exc))
        raise SystemExit(2)
    except InterruptedError:
        raise SystemExit(0)
    except Exception as exc:
        # Avoid dumping URLs, authentication headers, or data in tracebacks.
        safe_log("fatal", error_type=type(exc).__name__)
        raise SystemExit(1)
