from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request

from ed2k import parse_page

MAX_PAGE_BYTES = 4 * 1024 * 1024


class FetchError(RuntimeError):
    pass


class Telegram:
    def __init__(self, proxy, *, timeout=25, spacing=1, clock=time.time, sleep=time.sleep):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.timeout, self.spacing, self.clock, self.sleep = timeout, spacing, clock, sleep
        self.last_fetch = 0

    def fetch(self, channel, *, before=None, after=None):
        wait = self.spacing - (self.clock() - self.last_fetch)
        if wait > 0:
            self.sleep(wait)
        query = {}
        if before is not None:
            query["before"] = int(before)
        if after is not None:
            query["after"] = int(after)
        url = "https://t.me/s/" + channel
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"User-Agent": "tg-ed2k-reporter/1.0", "Accept": "text/html"})
        self.last_fetch = self.clock()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise FetchError(f"telegram_http_{response.status}")
                document = response.read(MAX_PAGE_BYTES + 1)
                if len(document) > MAX_PAGE_BYTES:
                    raise FetchError("telegram_page_too_large")
            return parse_page(document.decode("utf-8"), channel, allow_empty=after is not None)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"telegram_http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise FetchError("telegram_network_error") from None
        except (ValueError, UnicodeError):
            raise FetchError("telegram_invalid_page") from None


class Collector:
    def __init__(self, store, telegram, settings, *, clock=time.time):
        self.store, self.telegram, self.settings, self.clock = store, telegram, settings, clock

    def collect(self, channel):
        state = self.store.ensure_channel(channel, self.clock() - self.settings["initial_days"] * 86400)
        result = {"pages": 0, "new": 0, "valid": 0, "repaired": 0, "invalid": 0, "bootstrap_done": bool(state["bootstrap_done"])}

        def record(messages, **kw):
            counts = self.store.ingest(messages, self.clock(), channel=channel, **kw)
            for key, value in counts.items():
                result[key] += value
            self.store.set("heartbeat", self.clock())

        remaining = self.settings["max_pages_per_cycle"]
        while not state["bootstrap_done"] and remaining:
            self.store.set("heartbeat", self.clock())
            page = self.telegram.fetch(channel, before=state["bootstrap_before"])
            ids = [item.message_id for item in page.messages]
            if state["bootstrap_before"] is not None and min(ids) >= state["bootstrap_before"]:
                raise FetchError("telegram_history_no_progress")
            # Telegram IDs follow posting order; a valid timestamp is mandatory.
            selected = [item for item in page.messages if item.published_at >= state["since"]]
            done = min(item.published_at for item in page.messages) < state["since"] or page.before is None
            next_before = min(ids) if not done else None
            record(selected, cursor=max(ids), bootstrap_before=next_before, bootstrap_done=done)
            result["pages"] += 1
            remaining -= 1
            state = self.store.channel(channel)
        result["bootstrap_done"] = bool(state["bootstrap_done"])
        if not state["bootstrap_done"]:
            return result

        # After=X yields the next page, rather than the latest page, so an outage
        # spanning many posts is caught up without jumping the high-water mark.
        while remaining:
            self.store.set("heartbeat", self.clock())
            page = self.telegram.fetch(channel, after=state["cursor"])
            newer = [item for item in page.messages if item.message_id > state["cursor"]]
            result["pages"] += 1
            remaining -= 1
            if not newer:
                break
            record(newer, cursor=max(item.message_id for item in newer))
            state = self.store.channel(channel)
        # Re-read recent posts to catch added links in edited messages. This path
        # never advances the forward cursor and cannot bypass an unfinished gap.
        before = None
        for _ in range(min(self.settings["edit_pages"], remaining)):
            self.store.set("heartbeat", self.clock())
            page = self.telegram.fetch(channel, before=before)
            selected = [item for item in page.messages if item.published_at >= state["since"]]
            record(selected)
            result["pages"] += 1
            if page.before is None:
                break
            new_before = min(item.message_id for item in page.messages)
            if before is not None and new_before >= before:
                raise FetchError("telegram_edit_no_progress")
            before = new_before
        return result
