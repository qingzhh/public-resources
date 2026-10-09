"""Private downloader registry; edits return values and never persist configuration."""
import copy
import hashlib
import json
import re
import uuid
import seedkeep_configuration as configuration
from seedkeep_downloaders import API, ManagementError

ID = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
FIELDS = {'id', 'type', 'name', 'url', 'username', 'password', 'clear_password', 'enabled',
          'default', 'download_path', 'category', 'tag', 'keep_torrent', 'use_proxy', 'proxy_url'}


class SingleAPI:
    """A legacy-shaped API that only reads its selected downloader."""
    def __init__(self, api, kind):
        self.wrapped, self.kind = api, kind

    def __getattr__(self, name):
        if (self.kind == 'qb' and name == 'rpc') or (self.kind == 'tr' and name.startswith('q') and callable(getattr(self.wrapped, name, None))):
            raise ManagementError('实例类型不匹配')
        return getattr(self.wrapped, name)

    def inventory_one(self, kind=None):
        if kind is not None and kind != self.kind:
            raise ManagementError('实例类型不匹配')
        return self.wrapped.inventory_one(self.kind)

    def inventory(self):
        try:
            rows = self.inventory_one()
            return (rows, [], {}) if self.kind == 'qb' else ([], rows, {})
        except Exception:
            return [], [], {self.kind: '下载器读取失败，请检查连接'}

    def limits(self):
        return self.wrapped.limits(only=self.kind)

    def set_limits(self, values):
        if values.get('client') != self.kind:
            raise ManagementError('实例类型不匹配')
        return self.wrapped.set_limits(values, only=self.kind)


class Registry:
    def __init__(self, source_callable, settings_callable, api_factory=API):
        self.source, self.settings, self.api_factory = source_callable, settings_callable, api_factory

    def _legacy_credentials(self, kind):
        settings = self.settings()
        if kind == 'qb':
            source = configuration.resolve_source(self.source(), settings)
            return {'username': source.get('username', ''), 'password': source.get('password', '')}
        return configuration.tr_credentials(settings)

    def items(self):
        settings = self.settings()
        if 'downloaders' in settings:
            rows = settings['downloaders']
            if not isinstance(rows, list) or len(rows) > 100:
                raise ManagementError('下载器注册表格式无效')
            seen, defaults = set(), 0
            result = []
            for row in rows:
                if not isinstance(row, dict) or 'id' not in row or set(row) - FIELDS - {'credential_ref'}:
                    raise ManagementError('下载器注册表字段无效')
                candidate = self._validate(row, stored=True)
                if candidate['id'] in seen:
                    raise ManagementError('下载器实例标识重复')
                seen.add(candidate['id'])
                defaults += bool(candidate['default'])
                result.append(candidate)
            if defaults > 1:
                raise ManagementError('默认 qB 下载器只能有一个')
            return result
        source = configuration.resolve_source(self.source(), settings)
        resolved = configuration.resolve_settings(settings)
        try:
            qb_url = configuration.qb_base(source)
        except (KeyError, TypeError, ValueError, AttributeError):
            qb_url = ''
        try:
            tr_user = self._legacy_credentials('tr').get('username', '')
        except configuration.ConfigurationError:
            tr_user = ''
        common = {'enabled': True, 'use_proxy': bool(source.get('bt_use_proxy')),
                  'proxy_url': source.get('bt_proxy_url', ''), 'download_path': source.get('download_path', ''),
                  'category': source.get('category', ''), 'tag': source.get('tag', ''),
                  'keep_torrent': bool(source.get('keep_torrent'))}
        return [{**common, 'id': 'qb', 'type': 'qb', 'name': 'qBittorrent', 'default': True,
                 'url': qb_url, 'username': source.get('username', ''), 'credential_ref': 'legacy-qb'},
                {**common, 'id': 'tr', 'type': 'tr', 'name': 'Transmission', 'default': False,
                 'url': resolved.get('tr_url', ''), 'username': tr_user, 'credential_ref': 'legacy-tr'}]

    def _validate(self, instance, stored=False):
        if not isinstance(instance, dict) or set(instance) - FIELDS - ({'credential_ref'} if stored else set()):
            raise ManagementError('下载器实例字段无效')
        row = dict(instance)
        row.setdefault('id', uuid.uuid4().hex)
        if not isinstance(row['id'], str) or not ID.fullmatch(row['id']):
            raise ManagementError('下载器实例标识无效')
        if row.get('type') not in ('qb', 'tr'):
            raise ManagementError('下载器类型应为 qb 或 tr')
        defaults = {'name': 'qBittorrent' if row['type'] == 'qb' else 'Transmission', 'username': '',
                    'enabled': True, 'default': False, 'download_path': '', 'category': '', 'tag': '',
                    'keep_torrent': False, 'use_proxy': False, 'proxy_url': ''}
        for key, default in defaults.items():
            row.setdefault(key, default)
        for key in ('enabled', 'default', 'keep_torrent', 'use_proxy', 'clear_password'):
            if key in row and type(row[key]) is not bool:
                raise ManagementError('下载器开关格式无效')
        if row['default'] and (row['type'] != 'qb' or not row['enabled']):
            raise ManagementError('默认补量目的必须是启用的 qB 下载器')
        for key, maximum in (('name', 100), ('username', 2048), ('password', 8192), ('category', 100), ('tag', 500)):
            value = row.get(key, '')
            if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
                raise ManagementError('下载器文本字段格式无效')
        if not row['name'].strip():
            raise ManagementError('下载器名称不能为空')
        if row.get('clear_password') and row.get('password'):
            raise ManagementError('不能同时填写新密码和清空密码')
        if row.get('credential_ref') not in (None, 'legacy-' + row['type']):
            raise ManagementError('下载器凭据引用无效')
        try:
            row['url'] = configuration.url(row.get('url'))
            row['proxy_url'] = configuration.url(row['proxy_url'], empty=True)
            row['download_path'] = configuration.directory(row['download_path'], empty=True)
        except configuration.ConfigurationError as error:
            raise ManagementError(str(error)) from None
        return row

    def _draft(self, instance):
        if not isinstance(instance, dict) or set(instance) - FIELDS:
            raise ManagementError('下载器实例字段无效')
        for key in ('enabled', 'default', 'keep_torrent', 'use_proxy', 'clear_password'):
            if key in instance and type(instance[key]) is not bool:
                raise ManagementError('下载器开关格式无效')
        if 'password' in instance:
            password = instance['password']
            if not isinstance(password, str) or len(password) > 8192 or any(ord(c) < 32 for c in password):
                raise ManagementError('下载器密码格式无效')
        if instance.get('clear_password') and instance.get('password'):
            raise ManagementError('不能同时填写新密码和清空密码')
        old = next((row for row in self.items() if row['id'] == instance.get('id')), None)
        if instance.get('id') and old is None:
            raise ManagementError('下载器实例不存在', 404)
        if old and instance.get('type', old['type']) != old['type']:
            raise ManagementError('已有实例不能切换下载器类型')
        row = {**(old or {}), **instance}
        row.setdefault('id', uuid.uuid4().hex)
        if old and not instance.get('password') and not instance.get('clear_password'):
            if 'password' in old:
                row['password'] = old['password']
            else:
                row.pop('password', None)
        if instance.get('password') or instance.get('clear_password'):
            row.pop('credential_ref', None)
            row['password'] = '' if instance.get('clear_password') else instance['password']
        row.pop('clear_password', None)
        return self._validate(row, stored=True)

    def public(self):
        rows = self.items()
        # Include current referenced credentials in the digest, without exposing them.
        revision_rows = []
        public = []
        for row in rows:
            secret = self._password(row)
            revision_rows.append({**row, 'password': secret})
            public.append({**{key: copy.deepcopy(row.get(key)) for key in FIELDS - {'password', 'clear_password'}},
                           'password_configured': bool(secret)})
        revision = hashlib.sha256(json.dumps(revision_rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {'items': public, 'revision': revision, 'login_independent': True}

    def _password(self, row):
        if 'password' in row:
            return row['password']
        if row.get('credential_ref'):
            try:
                return self._legacy_credentials(row['type']).get('password', '')
            except configuration.ConfigurationError:
                return ''
        return ''

    def updated(self, instance):
        row = self._draft(instance)
        rows = self.items()
        result = []
        found = False
        for old in rows:
            if old['id'] == row['id']:
                result.append(row)
                found = True
            else:
                result.append({**old, 'default': False} if row['default'] and old['default'] else old)
        if not found:
            if len(result) >= 100:
                raise ManagementError('下载器实例最多 100 个')
            result.append(row)
        return result

    def removed(self, instance_id):
        rows = self.items()
        if not any(row['id'] == instance_id for row in rows):
            raise ManagementError('下载器实例不存在', 404)
        return [row for row in rows if row['id'] != instance_id]

    def primary(self):
        return next((row for row in self.items() if row['type'] == 'qb' and row['enabled'] and row['default']), None)

    def get(self, instance_id, *, enabled=True):
        row = next((row for row in self.items() if row['id'] == instance_id), None)
        if row is None:
            raise ManagementError('下载器实例不存在', 404)
        if enabled and not row['enabled']:
            raise ManagementError('下载器实例已停用', 409)
        return row

    def _connection(self, qb=None, tr=None):
        settings = configuration.resolve_settings(copy.deepcopy(self.settings()))
        source = configuration.resolve_source(self.source(), settings)
        # Already resolved site fields survive; old downloader overrides must not replace this pair.
        settings['connections'] = {}
        source['qb_url'] = qb['url'] if qb else 'http://unused.invalid'
        source['username'] = qb['username'] if qb else ''
        source['password'] = self._password(qb) if qb else ''
        connection = qb or tr
        source['bt_use_proxy'], source['bt_proxy_url'] = connection['use_proxy'], connection['proxy_url']
        if qb:
            source.update({key: qb[key] for key in ('download_path', 'category', 'tag', 'keep_torrent')})
            source['instance_id'] = qb['id']
        if tr:
            settings['tr_url'] = tr['url']
            settings['connections'] = {'tr_username': tr['username'], 'tr_password': self._password(tr)}
            settings['tr_proxy_source'] = {'bt_use_proxy': tr['use_proxy'], 'bt_proxy_url': tr['proxy_url']}
            settings['tr_labels'] = list(dict.fromkeys(v.strip() for v in (tr['category'] + ',' + tr['tag']).split(',') if v.strip()))
        return source, settings

    def api(self, instance_id, *, enabled=True):
        row = self.get(instance_id, enabled=enabled)
        source, settings = self._connection(qb=row if row['type'] == 'qb' else None,
                                            tr=row if row['type'] == 'tr' else None)
        return SingleAPI(self.api_factory(source, settings), row['type'])

    def pair(self, qb_id, tr_id):
        qb, tr = self.get(qb_id), self.get(tr_id)
        if qb['type'] != 'qb' or tr['type'] != 'tr':
            raise ManagementError('转种须选择 qB 来源和 TR 目的')
        return self._connection(qb, tr)

    def check(self, instance):
        try:
            row = self._draft(instance)
            source, settings = self._connection(qb=row if row['type'] == 'qb' else None,
                                                tr=row if row['type'] == 'tr' else None)
            self.api_factory(source, settings).inventory_one(row['type'])
            return {'connected': True, 'error': None, 'type': row['type']}
        except Exception:
            return {'connected': False, 'error': '下载器配置或连接检查失败，请检查地址、账号和权限',
                    'type': instance.get('type') if isinstance(instance, dict) and instance.get('type') in ('qb', 'tr') else None}
