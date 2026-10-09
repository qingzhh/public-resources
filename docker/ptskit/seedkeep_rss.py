"""Read the existing same-site seedkeep RSS without exposing private feed URLs."""
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import seedkeep_configuration as configuration

MAX_BYTES = 128 * 1024 * 1024
MAX_ITEMS = 100000
MAX_DEPTH = 32
MAX_NODE_BYTES = 1024 * 1024
CACHE_SECONDS = 600


class RssError(Exception):
    pass


def trusted_url(value, source, *, feed=False):
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        expected = urllib.parse.urlsplit(source.get('api_base', ''))
        parsed = urllib.parse.urlsplit(value)
        expected_port = expected.port or (443 if expected.scheme == 'https' else 80)
        actual_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return bool(expected.hostname and parsed.scheme == expected.scheme and parsed.scheme in ('http', 'https')
                    and actual_port == expected_port and parsed.hostname == expected.hostname and not parsed.username
                    and not parsed.password and not parsed.fragment
                    and (not feed or parsed.path.endswith('/seedkeeprss.php')))
    except ValueError:
        return False


def discover(source, registry):
    rows = registry.items()
    qb = next((row for row in rows if row['type'] == 'qb' and row['enabled'] and row['default']), None)
    qb = qb or next((row for row in rows if row['type'] == 'qb' and row['enabled']), None)
    if qb is None:
        raise RssError('RSS 需要一个启用的 qB 实例读取现有订阅')
    try:
        data = registry.api(qb['id']).qget('rss/items?withData=false')
    except Exception:
        raise RssError('RSS 订阅读取失败，请检查 qB 连接') from None
    feeds, stack, count = set(), [(data, 0)], 0
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > 16 or count > 5000:
            raise RssError('RSS 订阅结构超过读取上限')
        if isinstance(value, str) and trusted_url(value, source, feed=True):
            feeds.add(value)
        elif isinstance(value, dict):
            children = [value['url']] if isinstance(value.get('url'), str) else value.values()
            stack.extend((child, depth + 1) for child in children)
    if len(feeds) != 1:
        raise RssError('未找到唯一的同站保种 RSS 订阅')
    return next(iter(feeds))


class SameSiteRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_url(newurl, self.source, feed=True):
            raise RssError('RSS 重定向超出同站订阅范围')
        if req.full_url.startswith('https:') and not newurl.startswith('https:'):
            raise RssError('RSS 重定向降低了连接安全性')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class StreamReader:
    def __init__(self, response, *, max_bytes=MAX_BYTES, budget=45, clock=time.monotonic):
        self.response, self.max_bytes, self.clock = response, max_bytes, clock
        self.deadline = clock() + budget
        self.item_at = None
        self.total, self.progress_at, self.tail = 0, 0, b''

    def progress(self):
        self.progress_at = self.total

    def read(self, size=-1):
        if self.clock() >= self.deadline:
            raise RssError('RSS 读取超时，请稍后重试')
        raw = self.response.read(min(size if size > 0 else 16384, 16384))
        if self.clock() >= self.deadline:
            raise RssError('RSS 读取超时，请稍后重试')
        self.total += len(raw)
        if self.total > self.max_bytes:
            raise RssError('RSS 响应超过读取上限')
        if self.item_at is not None and self.total - self.item_at > MAX_NODE_BYTES:
            raise RssError('RSS 单个条目超过读取上限')
        if self.total - self.progress_at > MAX_NODE_BYTES:
            raise RssError('RSS 单个节点超过读取上限')
        # Removing NULs also recognizes declarations in UTF-16/32 byte streams.
        scanned = self.tail + raw.replace(b'\x00', b'')
        if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', scanned, re.I):
            raise RssError('RSS 包含不支持的 XML 声明')
        self.tail = scanned[-64:]
        return raw


def safe_text(value, maximum=500):
    value = value if isinstance(value, str) else ''
    value = html.unescape(re.sub(r'<[^>]*>', ' ', value))
    value = re.sub(r'https?://\S+', '[链接已隐藏]', value, flags=re.I)
    value = re.sub(r'(?i)\b(passkey|token|password|cookie|authorization)\s*[:=]\s*\S+', r'\1=[已隐藏]', value)
    return value.strip()[:maximum]


def metadata(node, source):
    children = {child.tag.rsplit('}', 1)[-1]: child for child in node}
    enclosure = children.get('enclosure')
    if enclosure is None or not trusted_url(enclosure.get('url'), source):
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(enclosure.get('url')).query, max_num_fields=20)
        values = query.get('id', [])
        tid = values[0] if len(values) == 1 else ''
        length = enclosure.get('length', '')
        if not (tid.isascii() and tid.isdigit() and 0 < len(tid) <= 20 and int(tid) > 0
                and length.isascii() and length.isdigit() and 0 < len(length) <= 20 and int(length) > 0):
            return None
        description = children.get('description')
        description = ''.join(description.itertext()) if description is not None else ''
        description = html.unescape(re.sub(r'<[^>]*>', ' ', description))
        seeders = re.search(r'做种\s*[:：]?\s*([0-9]{1,10})(?![0-9])', description)
        if seeders is None:
            return None
        def field(key):
            child = children.get(key)
            return ''.join(child.itertext()) if child is not None else ''
        tid = str(int(tid))
        return {'id': tid, 'name': safe_text(field('title')) or '种子 #' + tid,
                'small_descr': '', 'category': safe_text(field('category'), 100),
                'size': int(length), 'seeders': int(seeders[1]), 'leechers': None}
    except (ValueError, TypeError, OverflowError):
        return None


def parse(response, source, *, max_bytes=MAX_BYTES, max_items=MAX_ITEMS, budget=45, clock=time.monotonic):
    reader = StreamReader(response, max_bytes=max_bytes, budget=budget, clock=clock)
    rows, stack, scanned, events = {}, [], 0, 0
    try:
        for event, node in ET.iterparse(reader, events=('start', 'end')):
            reader.progress()
            events += 1
            if events > 2000000:
                raise RssError('RSS 节点数量超过读取上限')
            if event == 'start':
                if not stack and node.tag.rsplit('}', 1)[-1].lower() not in ('rss', 'rdf'):
                    raise RssError('RSS 响应不是有效订阅文档')
                stack.append(node)
                if node.tag.rsplit('}', 1)[-1] == 'item':
                    reader.item_at = reader.total
                if len(stack) > MAX_DEPTH:
                    raise RssError('RSS 嵌套超过读取上限')
                continue
            if node.tag.rsplit('}', 1)[-1] == 'item':
                scanned += 1
                row = metadata(node, source)
                reader.item_at = None
                if row:
                    rows[row['id']] = row
                node.clear()
                if len(stack) > 1:
                    stack[-2].remove(node)
                if scanned >= max_items:
                    return {'items': list(rows.values()), 'total': len(rows), 'truncated': True}
            elif not any(parent.tag.rsplit('}', 1)[-1] == 'item' for parent in stack):
                node.clear()
                if len(stack) > 1:
                    stack[-2].remove(node)
            stack.pop()
        return {'items': list(rows.values()), 'total': len(rows), 'truncated': False}
    except ET.ParseError:
        raise RssError('RSS 返回了无法识别的 XML 数据') from None


def fetch(source, settings, registry):
    url = discover(source, registry)
    opener = configuration.opener(source)
    opener.add_handler(SameSiteRedirect(source))
    timeout = settings.get('site_timeout_seconds', 15)
    try:
        with opener.open(url, timeout=timeout) as response:
            if not trusted_url(response.geturl(), source, feed=True):
                raise RssError('RSS 响应超出同站订阅范围')
            return parse(response, source, budget=min(90, max(30, timeout * 3)))
    except RssError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise RssError('RSS 认证或访问失败，请检查现有订阅') from None
    except Exception:
        raise RssError('RSS 连接失败，请稍后重试') from None


def sanitize(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list) or len(payload['items']) > MAX_ITEMS:
        raise RssError('RSS 候选数据格式无效')
    rows = {}
    for item in payload['items']:
        if not isinstance(item, dict):
            continue
        tid = item.get('id')
        tid = str(tid) if type(tid) is int and tid > 0 else tid
        if not isinstance(tid, str) or not tid.isascii() or not tid.isdigit() or not 0 < len(tid) <= 20 or int(tid) == 0:
            continue
        size, seeders = item.get('size'), item.get('seeders')
        if type(size) is not int or size <= 0 or type(seeders) is not int or seeders < 0:
            continue
        tid = str(int(tid))
        leechers = item.get('leechers')
        rows[tid] = {'id': tid, 'name': safe_text(item.get('name')) or '种子 #' + tid,
                     'small_descr': safe_text(item.get('small_descr')), 'category': safe_text(item.get('category'), 100),
                     'size': size, 'seeders': seeders,
                     'leechers': leechers if type(leechers) is int and leechers >= 0 else None}
    total = payload.get('total')
    return {'items': list(rows.values()), 'total': max(total, len(rows)) if type(total) is int and total >= 0 else len(rows),
            'truncated': payload.get('truncated') is True}
