"""Persistent OpenMind connection configuration.

Standard-mode secrets use the existing Fernet key; Demo uses the explicitly
marked plaintext format. The optional local admin passphrase is PBKDF2 hashed
outside Demo. Decrypted remote credentials are never returned to the browser.
Demo skips old host allowlists; standard mode retains their original policy.
"""
from __future__ import annotations
import hashlib
import hmac
import os
import re
import secrets
from urllib.parse import urlsplit

from .model_settings import ModelSettings, ModelSettingsError
from .demo_mode import enabled as demo_enabled, connection_policy
from .openmind import OpenMindError
from .openmind_transport_policy import enforce_transport_policy, insecure_http_enabled
from .store import Store, now

_HOST = re.compile(r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$', re.I)


def normalize_hosts(value):
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, list) or len(value)>50:
        raise OpenMindError('主机白名单必须是最多 50 个主机名')
    result=[]
    for x in value:
        if not isinstance(x,str):raise OpenMindError('主机名必须是字符串')
        host=x.strip().lower()
        if not host:continue
        if not _HOST.fullmatch(host) and host not in ('localhost',):
            # bare IPv4 addresses are accepted by the URL validator, too
            try:
                import ipaddress
                ipaddress.ip_address(host)
            except ValueError as e:raise OpenMindError('白名单只能填写主机名或 IP（不带协议、端口或通配符）') from e
        if host not in result:result.append(host)
    return result


def validate_connection(url, hosts):
    url=url.strip().rstrip('/')
    if len(url)>1024:raise OpenMindError('OpenMind 服务地址过长')
    try: p=urlsplit(url)
    except ValueError as exc:raise OpenMindError('OpenMind 服务地址格式无效') from exc
    if not url or p.scheme not in ('http','https') or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment or p.path not in ('','/'):
        raise OpenMindError('OpenMind 地址必须是服务器根地址，不包含账号、密码、子路径或查询参数')
    try: port=p.port
    except ValueError as e:raise OpenMindError('OpenMind 端口无效') from e
    if port is not None and not 1<=port<=65535:raise OpenMindError('OpenMind 端口无效')
    # Demo ignores BOTH page and deployment allowlists. Validate only the
    # URL contract before returning, not stale host entries or DNS/subnets.
    if demo_enabled():
        return url
    if not hosts or p.hostname.lower() not in hosts:
        raise OpenMindError('请在页面白名单中加入当前 OpenMind 服务的主机名')
    boundary=normalize_hosts(os.getenv('OPENMIND_ALLOWED_HOSTS',''))
    if boundary and p.hostname.lower() not in boundary:
        raise OpenMindError('OpenMind 主机不在部署层允许范围内')
    enforce_transport_policy(url, hosts)
    return url


def hash_admin(token):
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',token.encode(),salt,300_000)
    return salt.hex()+':'+digest.hex()


def matches_admin(stored, token):
    if not token:return False
    try:
        salt, digest=stored.split(':',1)
        candidate=hashlib.pbkdf2_hmac('sha256',token.encode(),bytes.fromhex(salt),300_000)
        return hmac.compare_digest(candidate,bytes.fromhex(digest))
    except (ValueError,TypeError):return False


class OpenMindSettings:
    def __init__(self,store:Store):
        self.store=store
        self.crypto=ModelSettings(store)

    def _row(self):
        return self.store.one('SELECT * FROM openmind_settings WHERE id=1')

    def _decrypt(self, ciphertext):
        if not ciphertext:return ''
        return self.crypto._decrypt(ciphertext)

    def effective(self):
        row=self._row()
        if row:
            return dict(base_url=row['base_url'],access_token=self._decrypt(row['access_token_ciphertext']),
                        auth_mode=row['auth_mode'],username=row['username'],password=self._decrypt(row['password_ciphertext']),
                        allowed_hosts=([] if demo_enabled() else normalize_hosts(__import__('json').loads(row['allowed_hosts']))),
                        source='saved',updated_at=row['updated_at'])
        url=os.getenv('OPENMIND_BASE_URL','').strip().rstrip('/')
        hosts=[] if demo_enabled() else normalize_hosts(os.getenv('OPENMIND_ALLOWED_HOSTS',''))
        if url and not hosts and not demo_enabled():
            p=urlsplit(url)
            if p.hostname and os.getenv('APP_ENV')!='production':hosts=[p.hostname.lower()]
        return dict(base_url=url,access_token=os.getenv('OPENMIND_ACCESS_TOKEN',''),
                    auth_mode=os.getenv('OPENMIND_AUTH_MODE','token'),username=os.getenv('OPENMIND_USERNAME',''),
                    password=os.getenv('OPENMIND_PASSWORD',''),allowed_hosts=hosts,
                    source='environment',updated_at=None)

    def public(self):
        row=self._row()
        if row:
            conf=self.effective()
            lost=(bool(row['access_token_ciphertext']) and not conf['access_token']) or (bool(row['password_ciphertext']) and not conf['password'])
            return dict(base_url=row['base_url'],allowed_hosts=conf['allowed_hosts'],
                        connection_policy=connection_policy(),
                        demo_mode=demo_enabled(),credential_reset_required=lost,
                        has_access_token=bool(conf['access_token']),
                        auth_mode=row['auth_mode'],username=row['username'],has_password=bool(conf['password']),
                        has_admin_token=False if demo_enabled() else bool(row['admin_token_hash'] or os.getenv('OPENMIND_ADMIN_TOKEN')),
                        source='saved',updated_at=row['updated_at'],access_token=None,admin_token=None,
                        http_compat_enabled=insecure_http_enabled(),
                        configured=bool(row['base_url'] and (row['username'] and conf['password'] if row['auth_mode']=='login' else conf['access_token'])))
        conf=self.effective()
        return dict(base_url=conf['base_url'],allowed_hosts=conf['allowed_hosts'],
                    connection_policy=connection_policy(),
                    demo_mode=demo_enabled(),credential_reset_required=False,
                    has_access_token=bool(conf['access_token']),auth_mode=conf['auth_mode'],username=conf['username'],has_password=bool(conf['password']),has_admin_token=False if demo_enabled() else bool(os.getenv('OPENMIND_ADMIN_TOKEN')),
                    source='environment',updated_at=None,access_token=None,admin_token=None,
                    http_compat_enabled=insecure_http_enabled(),
                    configured=bool(conf['base_url'] and (conf['username'] and conf['password'] if conf['auth_mode']=='login' else conf['access_token'])))

    def verify_admin(self,provided):
        row=self._row()
        if row and row['admin_token_hash']:
            return matches_admin(row['admin_token_hash'],provided)
        legacy=os.getenv('OPENMIND_ADMIN_TOKEN','')
        return bool(legacy and secrets.compare_digest(legacy,provided))

    def has_admin(self):
        row=self._row()
        return bool((row and row['admin_token_hash']) or os.getenv('OPENMIND_ADMIN_TOKEN'))

    def save(self,base_url,access_token='',allowed_hosts=None,clear_access_token=False,new_admin_token='',clear_admin_token=False,auth_mode=None,username='',password='',clear_password=False):
        # Deprecated in Demo. Do not validate obsolete whitelist values sent
        # by an old browser tab, nor let them block a connection.
        hosts=[] if demo_enabled() else normalize_hosts(allowed_hosts if allowed_hosts is not None else [])
        base_url=validate_connection(base_url,hosts)
        if auth_mode is not None and auth_mode not in ('login','token'):raise OpenMindError('OpenMind 认证模式必须是 login 或 token')
        if len(password)>512:raise OpenMindError('OpenMind 密码超过 512 字符')
        if len(username)>128:raise OpenMindError('OpenMind 账号超过 128 字符')
        if len(access_token)>8000:raise OpenMindError('访问令牌过长')
        if not demo_enabled():
            if len(new_admin_token)>2048:raise OpenMindError('管理密钥过长')
            if new_admin_token and len(new_admin_token)<12:raise OpenMindError('管理密钥至少需要 12 个字符')
        previous=self.effective()
        if clear_access_token or (base_url!=previous['base_url'] and not access_token):
            token=''
        else:
            token=access_token or previous['access_token']
        mode=auth_mode or previous.get('auth_mode','token')
        account=username.strip() or previous.get('username','')
        if mode=='login' and not account:raise OpenMindError('后台自动认证需要配置 OpenMind 账号')
        if clear_password or base_url!=previous['base_url'] or (username.strip() and username.strip()!=previous.get('username','')):
            secret=password
        else:
            secret=password or previous.get('password','')
        if mode=='login' and not secret:raise OpenMindError('后台自动认证需要配置 OpenMind 密码')
        row=self._row()
        admin_hash='' if demo_enabled() else (row['admin_token_hash'] if row else '')
        if new_admin_token and not demo_enabled():admin_hash=hash_admin(new_admin_token)
        if clear_admin_token and not demo_enabled():
            if os.getenv('APP_ENV')=='production' and not (os.getenv('MODEL_ADMIN_TOKEN') or os.getenv('OPENMIND_ADMIN_TOKEN')):
                raise OpenMindError('生产环境没有备用管理员身份，禁止清除最后一个管理密钥')
            admin_hash=''
        cipher=self.crypto._encrypt(token)
        secret_cipher=self.crypto._encrypt(secret)
        import json
        # Keep the dormant strict-mode list for reversible mode switching; it is
        # neither consulted nor changed by Demo saves, and need not parse here.
        serialized_hosts=row['allowed_hosts'] if demo_enabled() and row else json.dumps(hosts)
        with self.store.tx() as c:
            c.execute('''INSERT INTO openmind_settings
                         (id,base_url,access_token_ciphertext,allowed_hosts,admin_token_hash,updated_at,auth_mode,username,password_ciphertext)
                         VALUES (1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         base_url=excluded.base_url,access_token_ciphertext=excluded.access_token_ciphertext,
                         allowed_hosts=excluded.allowed_hosts,admin_token_hash=excluded.admin_token_hash,
                         updated_at=excluded.updated_at,auth_mode=excluded.auth_mode,username=excluded.username,
                         password_ciphertext=excluded.password_ciphertext''',
                      (base_url,cipher,serialized_hosts,admin_hash,now(),mode,account,secret_cipher))
        return self.public()

    def reset(self):
        if not demo_enabled() and os.getenv('APP_ENV')=='production' and not (os.getenv('MODEL_ADMIN_TOKEN') or os.getenv('OPENMIND_ADMIN_TOKEN')):
            raise OpenMindError('生产环境没有备用管理员身份，禁止恢复默认设置')
        with self.store.tx() as c:c.execute('DELETE FROM openmind_settings WHERE id=1')
        return self.public()
