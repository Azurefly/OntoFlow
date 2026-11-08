"""Server-side model connection settings (Fernet in standard mode, plaintext in Demo).

The saved configuration overrides the legacy LLM_* environment defaults. GET never
returns the decrypted secret. Application startup does not require an LLM.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .store import Store, now
from .demo_mode import enabled as demo_enabled, connection_policy


class ModelSettingsError(ValueError):
    pass


def validate_endpoint(base_url: str) -> str:
    url = base_url.strip().rstrip('/')
    if not url:
        return ''
    if len(url) > 1024:
        raise ModelSettingsError('模型地址不能超过 1024 个字符。')
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ModelSettingsError('模型服务地址格式无效。') from exc
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ModelSettingsError('模型地址必须是完整的 http:// 或 https:// URL。')
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ModelSettingsError('模型地址不能包含账号、密码、查询参数或片段。')
    try:
        port = parsed.port
    except ValueError as e:
        raise ModelSettingsError('模型地址的端口不合法。') from e
    if port is not None and not (1 <= port <= 65535):
        raise ModelSettingsError('模型地址的端口不合法。')
    if demo_enabled():
        return url
    allowed = {h.strip().lower() for h in os.getenv('MODEL_ALLOWED_HOSTS', '').split(',') if h.strip()}
    if os.getenv('APP_ENV') == 'production' and not allowed:
        raise ModelSettingsError('生产模式下通过页面配置模型，必须先设置 MODEL_ALLOWED_HOSTS 主机白名单。')
    if allowed and parsed.hostname.lower() not in allowed:
        raise ModelSettingsError('该模型服务地址不在 MODEL_ALLOWED_HOSTS 白名单中。')
    return url


DEMO_PLAINTEXT_PREFIX = "demo-plain-v1:"


class ModelSettings:
    def __init__(self, store: Store):
        self.store = store
        self.key_path = Path(store.root) / '.model-settings.key'

    def _fernet(self, create: bool = False):
        key = os.getenv('MODEL_SETTINGS_KEY', '').strip()
        if not key and self.key_path.exists():
            if os.getenv('APP_ENV') == 'production':
                raise ModelSettingsError('生产环境请配置 MODEL_SETTINGS_KEY；不能自动读取开发环境密钥文件。')
            key = self.key_path.read_text(encoding='utf-8').strip()
        if not key and create:
            if os.getenv('APP_ENV') == 'production':
                raise ModelSettingsError('生产模式保存模型密钥前，必须设置 MODEL_SETTINGS_KEY（Fernet 密钥）。')
            key = Fernet.generate_key().decode()
            try:
                fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(key)
            except FileExistsError:
                key = self.key_path.read_text(encoding='utf-8').strip()
        if not key:
            return None
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise ModelSettingsError('MODEL_SETTINGS_KEY 无效；请提供 Fernet 格式的密钥。') from e

    def _encrypt(self, value: str) -> str:
        if not value:
            return ''
        if demo_enabled():
            # Demo only: explicitly marked reversible storage, NOT encryption.
            return DEMO_PLAINTEXT_PREFIX + value
        return self._fernet(create=True).encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        if not value:
            return ''
        if value.startswith(DEMO_PLAINTEXT_PREFIX):
            if not demo_enabled():
                raise ModelSettingsError('检测到 Demo 明文凭据；切换安全模式前须重新配置密钥。')
            return value[len(DEMO_PLAINTEXT_PREFIX):]
        try:
            f = self._fernet()
            if f is None:
                raise ModelSettingsError('找不到模型密钥加密主密钥，请恢复 MODEL_SETTINGS_KEY。')
            return f.decrypt(value.encode()).decode()
        except (InvalidToken, ModelSettingsError, UnicodeDecodeError) as e:
            if demo_enabled():
                # Lost historical Fernet keys cannot be recovered. Let the UI
                # overwrite only those credential fields with new values.
                return ''
            if isinstance(e, InvalidToken):
                raise ModelSettingsError('模型 API 密钥无法解密；请检查 MODEL_SETTINGS_KEY 是否变化。') from e
            raise

    def _unreadable_legacy(self, raw: str) -> bool:
        return bool(demo_enabled() and raw and not raw.startswith(DEMO_PLAINTEXT_PREFIX)
                    and not self._decrypt(raw))

    def _row(self):
        return self.store.one('SELECT * FROM model_settings WHERE id=1')

    def effective(self) -> dict:
        row = self._row()
        if row:
            return {'base_url': row['base_url'], 'model': row['model'],
                    'api_key': self._decrypt(row['api_key_ciphertext']), 'source': 'saved',
                    'updated_at': row['updated_at']}
        return {'base_url': os.getenv('LLM_BASE_URL', '').strip().rstrip('/'),
                'model': os.getenv('LLM_MODEL', '').strip(),
                'api_key': os.getenv('LLM_API_KEY', ''), 'source': 'environment',
                'updated_at': None}

    def public(self) -> dict:
        conf = self.effective()
        row = self._row()
        return {'credential_reset_required': self._unreadable_legacy(row['api_key_ciphertext']) if row else False,
                'demo_mode': demo_enabled(), 'connection_policy':connection_policy(),
                'base_url': conf['base_url'], 'model': conf['model'],
                'enabled': bool(conf['base_url'] and conf['model']),
                'mode': 'LLM' if conf['base_url'] and conf['model'] else 'RULE_DEMO',
                'has_api_key': bool(conf['api_key']), 'source': conf['source'],
                'updated_at': conf['updated_at'],
                'api_key': None}

    def save(self, base_url: str, model: str, new_api_key: str = '', clear_api_key: bool = False):
        base_url = validate_endpoint(base_url)
        model = model.strip()
        if len(model) > 180 or len(new_api_key) > 8000:
            raise ModelSettingsError('模型名称或密钥长度超出限制。')
        if bool(base_url) != bool(model):
            raise ModelSettingsError('模型地址与模型名称需要同时填写；或同时清空以停用模型。')
        old = self.effective()
        # Do not forward old credentials to an unrelated destination.
        if not base_url or clear_api_key or (base_url != old['base_url'] and not new_api_key):
            key = ''
        else:
            key = new_api_key if new_api_key else old['api_key']
        cipher = self._encrypt(key)
        with self.store.tx() as c:
            c.execute('''INSERT INTO model_settings(id,base_url,model,api_key_ciphertext,updated_at)
                         VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         base_url=excluded.base_url,model=excluded.model,
                         api_key_ciphertext=excluded.api_key_ciphertext,updated_at=excluded.updated_at''',
                      (base_url, model, cipher, now()))
        return self.public()

    def reset(self):
        with self.store.tx() as c:
            c.execute('DELETE FROM model_settings WHERE id=1')
        return self.public()
