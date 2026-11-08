"""OpenMind backend-only FULL session, CSRF cookie refresh and single-flight renewal.

Auth contract: POST /api/v1/auth/login -> FULL accessToken in JSON +
om_refresh/om_csrf Set-Cookie; POST /auth/refresh with the current cookies
and X-CSRF-Token. Never expose a session credential to the UI.
"""
from __future__ import annotations

import threading
import time

import httpx

from .openmind import OpenMindClient, OpenMindError


class OpenMindAutoClient(OpenMindClient):
    def __init__(self, base_url, username, password, transport=None, allowed_hosts=None):
        if not username or not password:
            raise OpenMindError('后台登录模式尚未配置 OpenMind 账号和密码')
        self.username, self._password = username, password
        self._expires_at = 0
        self._session_lock = threading.RLock()
        self._session_user = None
        super().__init__(base_url, token='INITIALIZING', transport=transport, allowed_hosts=allowed_hosts)
        self.token = ''

    def _auth_call(self, path, data=None, headers=None):
        if self.url.startswith('http://'):
            from .openmind import validate_base_url
            validate_base_url(self.url,self.allowed_hosts)
        try:
            return self.client.post(path, json=data, headers=headers or {})
        except httpx.HTTPError as exc:
            raise OpenMindError(f'OpenMind 认证网络错误（{type(exc).__name__}），请检查服务地址与网络；刷新请求不能盲目重放') from exc

    @staticmethod
    def _error(response):
        try:
            body=response.json()
        except ValueError:
            body={}
        code=body.get('code','') if isinstance(body,dict) else ''
        return f'OpenMind 认证失败 HTTP {response.status_code}'+(f'（{code}）' if code else '')

    def _install(self, response):
        try: value=response.json()
        except ValueError: raise OpenMindError('OpenMind 认证没有返回有效 JSON')
        if not isinstance(value,dict):raise OpenMindError('OpenMind 登录接口没有返回 JSON 对象')
        if value.get('sessionType') != 'FULL' or not value.get('accessToken') or not isinstance(value.get('user'),dict):
            raise OpenMindError('OpenMind 当前账号不是 FULL 正式会话（可能实名待审核），停止读取本体')
        if value['user'].get('username') != self.username:
            raise OpenMindError('OpenMind 登录返回的账号与配置账号不一致')
        try: expires=int(value['expiresAt'])
        except (TypeError,ValueError,KeyError): raise OpenMindError('OpenMind 认证缺少 expiresAt Unix 秒时间戳')
        if expires <= time.time():raise OpenMindError('OpenMind 认证返回的访问令牌已经过期')
        self.token=value['accessToken']
        self._expires_at=expires
        self._session_user={k:value['user'].get(k) for k in ('id','username','displayName','role')}
        if self.url.startswith('http://'):
            if any(c.name=='om_refresh' and c.secure for c in self.client.cookies.jar):
                self.token='';self._expires_at=0
                raise OpenMindError('OpenMind 在 HTTP 连接上设置了 Secure Refresh Cookie，无法自动续期；请在受控内网关闭 OpenMind 的 auth.cookieSecure，或改用 HTTPS')

    def _login(self):
        # New login replaces potentially stale cookies; do not replay a consumed refresh token.
        self.client.cookies.clear()
        response=self._auth_call('/api/v1/auth/login',{'username':self.username,'password':self._password})
        if response.status_code != 200:
            raise OpenMindError(self._error(response))
        self._install(response)
        if self.url.startswith('http://'):
            from .openmind import validate_base_url
            validate_base_url(self.url,self.allowed_hosts)
        try:
            me=self.client.get('/api/v1/auth/me',headers={'Authorization':f'Bearer {self.token}'})
        except httpx.HTTPError as exc:
            raise OpenMindError(f'OpenMind 当前账号确认请求失败（{type(exc).__name__}）') from exc
        if me.status_code!=200 or me.json().get('username')!=self.username:
            raise OpenMindError('OpenMind /auth/me 未确认接入账号身份或账号已失效')

    def _refresh(self):
        csrf=None
        for cookie in self.client.cookies.jar:
            if cookie.name=='om_csrf':csrf=cookie.value
        has_refresh=any(c.name=='om_refresh' for c in self.client.cookies.jar)
        if not has_refresh or not csrf:
            self._login();return
        try:
            response=self._auth_call('/api/v1/auth/refresh',headers={'X-CSRF-Token':csrf})
        except OpenMindError:
            # Unknown whether the old rotating refresh token was consumed.
            # Do not send another refresh with the same cookie.
            self.token='';self._expires_at=0
            raise
        if response.status_code==200:
            self._install(response)
            return
        try: body=response.json()
        except ValueError: body={}
        code=body.get('code','') if isinstance(body,dict) else ''
        if code in ('REFRESH_REUSED','SESSION_REVOKED','USER_DISABLED'):
            self.token='';self._expires_at=0
            raise OpenMindError(self._error(response)+'；停止自动重登，请管理员检查会话或账号')
        if code in ('REFRESH_REQUIRED','SESSION_IDLE_TIMEOUT','AUTH_REQUIRED','INVALID_TOKEN') or response.status_code==401:
            # Refresh expired; login once with configured credentials.
            self._login();return
        raise OpenMindError(self._error(response))

    def _ensure_session(self):
        with self._session_lock:
            if not self.token:
                self._login()
            elif time.time()+45>=self._expires_at:
                self._refresh()

    def request(self,method,path,params=None,data=None):
        self._ensure_session()
        used_token=self.token
        try:
            return super().request(method,path,params=params,data=data)
        except OpenMindError as exc:
            message=str(exc)
            if method.upper()=='GET' and 'HTTP 401' in message:
                with self._session_lock:
                    # May have been refreshed by another thread while GET in flight.
                    if self.token==used_token:
                        self._expires_at=0
                        self._refresh()
                return super().request(method,path,params=params,data=data)
            if method.upper() not in ('GET','HEAD','OPTIONS') and 'HTTP 401' in message:
                raise OpenMindError('OpenMind 写入请求收到 401；不会自动重放非幂等操作，请核对认证并回读目标对象') from exc
            if 'HTTP 403' in message:
                raise OpenMindError('OpenMind HTTP 403：后台接入账号没有该领域或分支所需的权限；不会通过刷新令牌或切换管理员身份绕过') from exc
            raise

    def domains(self):
        return self.request('GET','/api/v1/domains')

    def session_account(self):
        self._ensure_session()
        return dict(self._session_user)
