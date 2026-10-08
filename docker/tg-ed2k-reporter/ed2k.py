from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs

MAX_SIZE = 2**63 - 1
START = re.compile(r"ed2k://\|file\|", re.IGNORECASE)
HASH = re.compile(r"[0-9a-fA-F]{32}\Z")


@dataclass(frozen=True)
class Link:
    name: str
    size: int
    md4: str
    normalized: str
    repaired: bool
    raw: str

    @property
    def report_hash(self) -> str:
        return f"ed2k:{self.md4}:{self.size}"


@dataclass(frozen=True)
class InvalidLink:
    raw: str
    reason: str


@dataclass(frozen=True)
class Batch:
    links: tuple[Link, ...]
    errors: tuple[InvalidLink, ...]


def normalize(text: str) -> Batch:
    text = html.unescape(str(text)).replace("\ufeff", "")
    starts = list(START.finditer(text))
    valid, errors = [], []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        raw = text[match.start():end].splitlines()[0].strip()
        # The first five fields are authoritative. Tail fields and log messages
        # cannot supply a missing size/hash or alter their contents.
        fields = raw.split("|", 5)
        if len(fields) < 5:
            errors.append(InvalidLink(raw[:4096], "missing_fields"))
            continue
        name, size_text, hash_text = fields[2], fields[3].strip(), fields[4].strip()
        if not name.strip() or len(name) > 4096 or re.search(r"[\x00-\x1f\x7f]", name):
            errors.append(InvalidLink(raw[:4096], "invalid_filename"))
            continue
        if not re.fullmatch(r"[0-9]{1,128}", size_text):
            errors.append(InvalidLink(raw[:4096], "invalid_size"))
            continue
        size = int(size_text)
        if not 0 < size <= MAX_SIZE:
            errors.append(InvalidLink(raw[:4096], "invalid_size"))
            continue
        if not HASH.fullmatch(hash_text):
            errors.append(InvalidLink(raw[:4096], "invalid_hash"))
            continue
        md4 = hash_text.lower()
        normalized = f"ed2k://|file|{name}|{size}|{md4}|/"
        tail = fields[5] if len(fields) > 5 else ""
        source = raw
        if "|/" in source:
            source = source[:source.index("|/") + 2]
        elif len(fields) > 5:
            # Store the link fields only, never the remainder of a copied log.
            source = "|".join(fields[:5]) + "|" + tail.split(" err=", 1)[0].strip()
        valid.append(Link(name, size, md4, normalized, source != normalized, source[:8192]))
    return Batch(tuple(valid), tuple(errors))


@dataclass(frozen=True)
class Message:
    channel: str
    message_id: int
    published_at: float
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Page:
    messages: tuple[Message, ...]
    before: int | None


class ChannelParser(HTMLParser):
    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.depth = 0
        self.current = None
        self.message_depth = None
        self.text_depth = None
        self.before = None
        self.messages = []
        self.channel_identity = False
        self.history = False
        self.html_closed = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = values.get("class", "").split()
        if tag == "meta" and values.get("property") == "al:ios:url" and values.get("content") == "tg://resolve?domain=" + self.channel:
            self.channel_identity = True
        if "tgme_channel_history" in classes:
            self.history = True
        if tag == "div":
            self.depth += 1
            if values.get("data-post", "").startswith(self.channel + "/"):
                if self.current is not None:
                    raise ValueError("nested_message")
                suffix = values["data-post"].split("/")[-1]
                if not suffix.isdigit() or int(suffix) <= 0:
                    raise ValueError("invalid_message_id")
                self.current = {"id": int(suffix), "time": None, "parts": [], "links": []}
                self.message_depth = self.depth
            if self.current is not None and "tgme_widget_message_text" in classes:
                self.text_depth = self.depth
        if self.current is not None:
            if tag == "time" and values.get("datetime"):
                try:
                    stamp = datetime.fromisoformat(values["datetime"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("missing_timezone")
                    self.current["time"] = stamp.timestamp()
                except ValueError as exc:
                    raise ValueError("invalid_message_time") from exc
            if self.text_depth is not None:
                if tag == "br":
                    self.current["parts"].append("\n")
                href = values.get("href", "")
                if START.match(href):
                    self.current["links"].append(href)
        if tag == "a" and "tme_messages_more" in classes:
            value = values.get("data-before")
            if not value:
                value = parse_qs(urlsplit(values.get("href", "")).query).get("before", [""])[0]
            if value and value.isdigit():
                self.before = int(value)

    def handle_data(self, data):
        if self.current is not None and self.text_depth is not None:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "html":
            self.html_closed = True
        if tag != "div":
            return
        if self.current is not None:
            if self.text_depth == self.depth:
                self.text_depth = None
            if self.message_depth == self.depth:
                if self.current["time"] is None:
                    raise ValueError("missing_message_time")
                text = "".join(self.current["parts"])
                for link in self.current["links"]:
                    if link not in text:
                        text += "\n" + link
                self.messages.append(Message(self.channel, self.current["id"], self.current["time"], text))
                self.current = None
                self.message_depth = None
                self.text_depth = None
        self.depth -= 1
        if self.depth < 0:
            raise ValueError("invalid_html_depth")


def parse_page(document: str, channel: str, *, allow_empty=False) -> Page:
    parser = ChannelParser(channel)
    parser.feed(document)
    parser.close()
    if parser.current is not None or not parser.messages and not (allow_empty and parser.channel_identity and parser.history and parser.html_closed and parser.depth == 0):
        raise ValueError("incomplete_channel_page")
    messages = tuple(sorted(parser.messages, key=lambda item: item.message_id))
    if len({item.message_id for item in messages}) != len(messages):
        raise ValueError("duplicate_message_ids")
    return Page(messages, parser.before)
