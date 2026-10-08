from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CLOUD_URL = "https://msapi.weikeba.cn"
COMPLETE = {"exists", "created", "updated"}
STOPPED = {"blocked", "ignored", "conflict"}
NEEDED = {"need_payload", "report_required"}


class ReportError(RuntimeError):
    def __init__(self, reason, *, transient=False, auth=False, uncertain=False):
        super().__init__(reason)
        self.transient, self.auth, self.uncertain = transient, auth, uncertain


@dataclass(frozen=True)
class Decision:
    status: str
    parts: tuple[str, ...] = ()
    protocol: str = "decision"


@dataclass(frozen=True)
class Outcome:
    state: str
    receipt: dict


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_url(value, *, cloud=False):
    try:
        parsed = urlsplit(value)
        valid = parsed.hostname and parsed.scheme in ("http", "https") and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        if cloud and parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            valid = False
        if not valid or parsed.port == 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ReportError("report_endpoint_invalid") from None
    return value.rstrip("/")


class MsReporter:
    def __init__(self, secrets, *, timeout=25, proxy=None, transport=None):
        required = {"ms_url", "ms_api_key", "email", "slogan", "driver_name"}
        if not isinstance(secrets, dict) or not required <= secrets.keys() or set(secrets) - required - {"cloud_url"}:
            raise ReportError("report_credentials_invalid")
        for key in required:
            value = secrets[key]
            if not isinstance(value, str) or not value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
                raise ReportError("report_credentials_invalid")
        self.ms_url = check_url(secrets["ms_url"])
        self.cloud_url = check_url(secrets.get("cloud_url", CLOUD_URL), cloud=True)
        self.key, self.email, self.slogan, self.driver = (secrets[key] for key in ("ms_api_key", "email", "slogan", "driver_name"))
        self.timeout = timeout
        # Local MS identification always bypasses the container's external proxy.
        self.local = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.remote = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}), NoRedirect())
        self.transport = transport or self._http

    def _http(self, url, payload, headers, *, remote, write):
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        opener = self.remote if remote else self.local
        try:
            try:
                response = opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                if status in (401, 403):
                    raise ReportError("cloud_auth_failed" if remote else "ms_auth_failed", auth=True)
                if status == 429 or status >= 500:
                    raise ReportError(("cloud" if remote else "ms") + f"_http_{status}", transient=True, uncertain=write)
                if status != 200:
                    raise ReportError(("cloud" if remote else "ms") + f"_http_{status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ReportError("response_too_large", transient=True, uncertain=write)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ReportError("cloud_network_error" if remote else "ms_network_error", transient=True, uncertain=write) from None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ReportError("response_not_json", transient=True, uncertain=write) from None
        return body

    def request(self, path, payload, *, remote=True, write=False):
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "tg-ed2k-reporter/1.0"}
        if remote:
            headers.update({"X-Email": self.email, "X-Slogan": self.slogan})
        else:
            headers["Authorization"] = "Bearer " + self.key
        body = self.transport((self.cloud_url if remote else self.ms_url) + path, payload, headers, remote=remote, write=write)
        if not isinstance(body, dict) or type(body.get("code")) is not int:
            raise ReportError("response_business_code_missing", transient=True, uncertain=write)
        code = body["code"]
        if code != 20000:
            # Inspect server text for classification, but never save/print it.
            message = str(body.get("message", "")).lower()
            if any(word in message for word in ("unauthorized", "authentication", "access denied", "认证失败", "认证过期", "口令错误", "邮箱错误", "未授权", "无权限", "权限不足", "授权失败", "没有权限")):
                raise ReportError("cloud_auth_failed" if remote else "ms_auth_failed", auth=True)
            transient = code >= 50000 or code in (40005, 429, 42900)
            raise ReportError(("cloud" if remote else "ms") + f"_business_{code}", transient=transient, uncertain=write and transient)
        return body.get("data")

    def base_request(self, item):
        return {"tmdb_id": 0, "media_type": "", "file_name": item["name"], "file_size": item["size"], "driver_name": self.driver, "sample_hash": f"ed2k:{item['md4']}:{item['size']}"}

    @staticmethod
    def decision(data):
        if not isinstance(data, dict) or not isinstance(data.get("status"), str) or data["status"] not in COMPLETE | STOPPED | NEEDED:
            raise ReportError("cloud_decision_invalid", transient=True)
        parts = data.get("required_parts", [])
        if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
            raise ReportError("cloud_required_parts_invalid", transient=True)
        return Decision(data["status"], tuple(parts))

    def query(self, item):
        payload = self.base_request(item)
        data = self.request("/api/v1/cs_hash/query_by_unique", payload)
        if data is None or isinstance(data, dict) and type(data.get("id")) is int and data["id"] == 0 and "status" not in data:
            return Decision("need_payload", ("media", "md4"), "legacy")
        if isinstance(data, dict) and "status" in data:
            return self.decision(data)
        # Compatibility with an older server returning a complete resource row.
        if isinstance(data, dict) and type(data.get("id")) is int and data["id"] > 0:
            if data.get("sample_hash") != payload["sample_hash"] or data.get("file_size") != item["size"] or data.get("driver_name") != self.driver:
                raise ReportError("cloud_identity_conflict")
            return Decision("exists", protocol="legacy")
        raise ReportError("cloud_query_invalid", transient=True)

    def analyze(self, item):
        data = self.request("/api/v1/torrent/analysis", {"title": item["name"]}, remote=False)
        if not isinstance(data, dict) or not isinstance(data.get("tmdbMedia"), dict) or not isinstance(data.get("metadata"), dict):
            raise ReportError("media_unrecognized", transient=True)
        media, metadata = data["tmdbMedia"], data["metadata"]
        if type(media.get("id")) is not int or media["id"] <= 0 or media.get("mediaType") not in ("movie", "tv"):
            raise ReportError("media_unrecognized", transient=True)
        fields = {"tmdb_id": media["id"], "media_type": media["mediaType"]}
        if fields["media_type"] == "tv":
            season, episode = metadata.get("beginSeason"), metadata.get("beginEpisode")
            if type(season) is not int or type(episode) is not int or season < 0 or episode <= 0:
                raise ReportError("episode_unrecognized", transient=True)
            if metadata.get("endSeason", season) not in (0, season) or metadata.get("endEpisode", episode) not in (0, episode):
                raise ReportError("episode_range_unsupported")
            fields.update({"season": season, "episode": episode})
        return fields

    @staticmethod
    def outcome(decision, via):
        if decision.status not in COMPLETE | STOPPED:
            return None
        state = "existing" if decision.status == "exists" else "reported" if decision.status in ("created", "updated") else "failed" if decision.status == "conflict" else "blocked"
        return Outcome(state, {"via": via, "code": 20000, "status": decision.status})

    def process(self, item, *, stop=None):
        decision = self.query(item)
        result = self.outcome(decision, "query")
        if result:
            return result
        if set(decision.parts) - {"media", "md4"}:
            raise ReportError("missing_hash_material")
        payload = self.base_request(item)
        payload.update(self.analyze(item))
        payload["hash_info"] = {"md4": item["md4"]}
        if stop is not None and stop.is_set():
            raise InterruptedError("shutdown_before_write")
        if decision.protocol == "legacy":
            self.request("/api/v1/cs_hash/create", payload, write=True)
            # A legacy success envelope has no decision; confirm by querying.
            try:
                confirmed = self.query(item)
            except ReportError as exc:
                raise ReportError(str(exc), transient=True, auth=exc.auth, uncertain=True) from None
            if confirmed.status in COMPLETE:
                return Outcome("reported", {"via": "create_and_query", "code": 20000, "status": confirmed.status})
            result = self.outcome(confirmed, "query")
            if result:
                return result
            raise ReportError("create_not_confirmed", transient=True, uncertain=True)
        data = self.request("/api/v1/cs_hash/report", payload, write=True)
        try:
            submitted = self.decision(data)
        except ReportError as exc:
            raise ReportError(str(exc), transient=True, uncertain=True) from None
        result = self.outcome(submitted, "report")
        if result:
            return result
        raise ReportError("cloud_state_changed", transient=True)


def report_batch(store, reporter, settings, *, limit=None, stop=None, sleep=time.sleep, clock=time.time):
    stop = stop if stop is not None else threading.Event()
    limit = min(settings["max_reports_per_cycle"], limit) if limit is not None else settings["max_reports_per_cycle"]
    counts = {"processed": 0, "reported": 0, "existing": 0, "retry": 0, "failed": 0, "blocked": 0, "uncertain": 0, "rechecked": 0}
    if store.get("report_pause"):
        return {**counts, "paused": True}
    used = 0

    def spacing():
        nonlocal used
        if used:
            sleep(settings["report_spacing_seconds"])
        store.set("heartbeat", clock())
        used += 1

    for item in store.uncertain(clock(), limit):
        if stop.is_set():
            break
        spacing()
        try:
            decision = reporter.query(item)
            result = reporter.outcome(decision, "recovery_query")
            state = result.state if result else "failed" if item["attempts"] >= settings["max_attempts"] else "retry"
            error = result.receipt["status"] if result and state in ("failed", "blocked") else "unconfirmed_request" if not result else None
            store.resolve(item, state, clock(), receipt=result.receipt if result else None, error=error, retry_at=clock() + settings["retry_base_seconds"] if state == "retry" else 0)
            counts[state] += 1
        except ReportError as exc:
            store.resolve(item, "uncertain", clock(), error=str(exc), retry_at=clock() + settings["retry_cap_seconds"])
            counts["uncertain"] += 1
            if exc.auth:
                store.set("report_pause", {"reason": str(exc), "at": clock()})
        counts["rechecked"] += 1
        if store.get("report_pause"):
            break
    if store.get("report_pause") or stop.is_set():
        return {**counts, "paused": bool(store.get("report_pause"))}
    for item in store.due(clock(), max(0, limit - used)):
        if stop.is_set():
            break
        spacing()
        if stop.is_set():
            break
        if not store.begin(item, clock()):
            continue
        counts["processed"] += 1
        attempts = item["attempts"] + 1
        try:
            result = reporter.process(item, stop=stop)
            store.finish(item, result.state, clock(), receipt=result.receipt, error=result.receipt["status"] if result.state in ("failed", "blocked") else None)
            counts[result.state] += 1
        except InterruptedError:
            store.finish(item, "retry", clock(), error="shutdown_before_write", retry_at=clock())
            counts["retry"] += 1
            break
        except ReportError as exc:
            delay = min(settings["retry_cap_seconds"], settings["retry_base_seconds"] * 2 ** (attempts - 1))
            state = "uncertain" if exc.uncertain else "blocked" if exc.auth else "retry" if exc.transient and attempts < settings["max_attempts"] else "failed"
            store.finish(item, state, clock(), error=str(exc), retry_at=clock() + delay)
            counts[state] += 1
            if exc.auth:
                store.set("report_pause", {"reason": str(exc), "at": clock()})
                break
    store.set("heartbeat", clock())
    return {**counts, "paused": bool(store.get("report_pause"))}
