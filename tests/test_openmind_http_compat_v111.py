"""Production HTTP exception stays opt-in, private-only, and end-to-end."""
import json
import socket
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.openmind import OpenMindClient, OpenMindError, validate_base_url
from app.openmind_auth import OpenMindAutoClient
from app.openmind_settings import validate_connection
from app.openmind_transport_policy import ensure_private_http_target
from scripts.init_fresh_env import generate_settings


@pytest.fixture(autouse=True)
def production_settings(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    for key in ('OPENMIND_ALLOW_INSECURE_HTTP', 'OPENMIND_ALLOWED_HOSTS',
                'OPENMIND_BASE_URL', 'OPENMIND_ACCESS_TOKEN'):
        monkeypatch.delenv(key, raising=False)


def authorize_http(monkeypatch, host='192.168.10.8'):
    monkeypatch.setenv('OPENMIND_ALLOW_INSECURE_HTTP', 'true')
    monkeypatch.setenv('OPENMIND_ALLOWED_HOSTS', host)


def test_https_default_still_works_and_http_is_denied(monkeypatch):
    assert validate_connection('https://openmind.internal', ['openmind.internal']) == 'https://openmind.internal'
    with pytest.raises(OpenMindError, match='HTTPS'):
        validate_connection('http://192.168.10.8:8080', ['192.168.10.8'])
    with pytest.raises(OpenMindError, match='HTTPS'):
        OpenMindClient('http://192.168.10.8:8080', 'secret', allowed_hosts=['192.168.10.8'])


def test_explicit_allowlist_and_private_http_work_for_save_and_real_client(monkeypatch):
    authorize_http(monkeypatch)
    url = 'http://192.168.10.8:8080'
    assert validate_connection(url, ['192.168.10.8']) == url
    calls = []
    def handle(request):
        calls.append((str(request.url), request.headers.get('authorization')))
        return httpx.Response(200, json={'items': [], 'total': 0})
    client = OpenMindClient(url, 'secret', allowed_hosts=['192.168.10.8'],
                            transport=httpx.MockTransport(handle))
    try:
        assert client.domains()['items'] == []
        assert calls == [(url + '/api/v1/domains', 'Bearer secret')]
        assert not client.client.follow_redirects
        assert client.client._trust_env is False
    finally:
        client.close()


def test_empty_or_wrong_deployment_host_does_not_grant_http(monkeypatch):
    monkeypatch.setenv('OPENMIND_ALLOW_INSECURE_HTTP', '1')
    with pytest.raises(OpenMindError, match='部署'):
        validate_connection('http://192.168.10.8', ['192.168.10.8'])
    monkeypatch.setenv('OPENMIND_ALLOWED_HOSTS', '192.168.10.9')
    with pytest.raises(OpenMindError, match='部署'):
        validate_connection('http://192.168.10.8', ['192.168.10.8'])
    authorize_http(monkeypatch)
    with pytest.raises(OpenMindError, match='白名单'):
        validate_base_url('http://192.168.10.8', ['192.168.10.9'])


@pytest.mark.parametrize('ip', [
    '8.8.8.8', '1.1.1.1', '169.254.169.254', '100.64.0.1',
    '0.0.0.0', '198.51.100.2', '::ffff:192.168.10.8',
])
def test_public_metadata_and_special_use_ip_are_rejected(monkeypatch, ip):
    authorize_http(monkeypatch, ip)
    url = f'http://[{ip}]:8080' if ':' in ip else f'http://{ip}:8080'
    with pytest.raises(OpenMindError, match='只允许|非私有'):
        validate_connection(url, [ip])


def test_private_dns_mixed_answers_are_blocked_and_rechecked(monkeypatch):
    authorize_http(monkeypatch, 'openmind.corp.lan')
    answers = ['10.25.3.5']
    def getaddrinfo(_host, _port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, 8080)) for ip in answers]
    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    url = 'http://openmind.corp.lan:8080'
    client = OpenMindClient(url, 'secret', allowed_hosts=['openmind.corp.lan'],
                            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'items': []})))
    try:
        assert client.domains()['items'] == []
        answers.append('8.8.8.8')
        with pytest.raises(OpenMindError, match='非私有'):
            client.domains()
    finally:
        client.close()


def test_http_backend_login_cookie_refresh_and_me(monkeypatch):
    authorize_http(monkeypatch)
    requests = []
    def tokens(token):
        return httpx.Response(200, json={
            'sessionType': 'FULL', 'accessToken': token,
            'expiresAt': int(time.time()) + 3600, 'idleTimeoutSeconds': 10800,
            'user': {'username': 'integration', 'id': 'id'}
        }, headers=[
            ('Set-Cookie', f'om_refresh=r-{token}; Path=/api/v1/auth; HttpOnly'),
            ('Set-Cookie', f'om_csrf=c-{token}; Path=/')
        ])
    def handle(req):
        requests.append((req.url.path, req.headers.get('x-csrf-token'), req.headers.get('cookie', '')))
        if req.url.path.endswith('/login'):
            assert json.loads(req.content)['username'] == 'integration'
            return tokens('one')
        if req.url.path.endswith('/me'):
            assert req.headers['authorization'] == 'Bearer one'
            return httpx.Response(200, json={'username': 'integration'})
        if req.url.path.endswith('/refresh'):
            assert req.headers['x-csrf-token'] == 'c-one'
            assert 'om_refresh=r-one' in req.headers['cookie']
            return tokens('two')
        return httpx.Response(200, json={'items': [], 'total': 0})
    client = OpenMindAutoClient('http://192.168.10.8:8080', 'integration', 'password',
                                allowed_hosts=['192.168.10.8'], transport=httpx.MockTransport(handle))
    try:
        assert client.domains()['items'] == []
        client._expires_at = time.time() + 1
        assert client.domains()['items'] == []
        assert client.token == 'two'
        assert sum(p.endswith('/login') for p,*_ in requests) == 1
        assert sum(p.endswith('/refresh') for p,*_ in requests) == 1
    finally:
        client.close()


def test_secure_refresh_cookie_over_http_fails_with_actionable_message(monkeypatch):
    authorize_http(monkeypatch)
    def handle(req):
        return httpx.Response(200, json={
            'sessionType': 'FULL', 'accessToken': 'one',
            'expiresAt': int(time.time()) + 3600,
            'user': {'username': 'integration'}
        }, headers=[('Set-Cookie', 'om_refresh=secure-example; Path=/api/v1/auth; HttpOnly; Secure'),
                    ('Set-Cookie', 'om_csrf=example; Path=/; Secure')])
    client = OpenMindAutoClient('http://192.168.10.8:8080', 'integration', 'password',
                                allowed_hosts=['192.168.10.8'], transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(OpenMindError, match=r'auth.cookieSecure'):
            client.domains()
    finally:
        client.close()


def test_disabling_flag_blocks_existing_http_client_before_network(monkeypatch):
    authorize_http(monkeypatch)
    traffic = []
    client = OpenMindClient('http://192.168.10.8:8080', 'secret',
                            allowed_hosts=['192.168.10.8'], transport=httpx.MockTransport(
                                lambda req: (traffic.append(req.url.path), httpx.Response(200, json={'items': []}))[1]))
    try:
        monkeypatch.setenv('OPENMIND_ALLOW_INSECURE_HTTP', 'false')
        with pytest.raises(OpenMindError, match='HTTPS'):
            client.domains()
        assert traffic == []
    finally:
        client.close()


def test_api_saved_config_http_and_public_status(tmp_path, monkeypatch):
    authorize_http(monkeypatch)
    monkeypatch.setenv('API_TOKEN', 'front-token')
    monkeypatch.setenv('MODEL_ADMIN_TOKEN', 'admin-token')
    monkeypatch.setenv('MODEL_SETTINGS_KEY', Fernet.generate_key().decode())
    with TestClient(create_app(tmp_path / 'test', testing=True)) as client:
        result = client.put('/api/settings/openmind', headers={
            'X-API-Key': 'front-token', 'X-Model-Admin-Key': 'admin-token'
        }, json={'base_url': 'http://192.168.10.8:8080',
                 'allowed_hosts': ['192.168.10.8'], 'auth_mode': 'login',
                 'username': 'integration', 'password': 'password-placeholder'})
        assert result.status_code == 200, result.text
        assert result.json()['http_compat_enabled'] is True
        assert 'password-placeholder' not in result.text
    assert 'ONTOFLOW_DEMO_MODE=true' in generate_settings()
    assert 'OPENMIND_ALLOW_INSECURE_HTTP=' not in generate_settings()
    assert 'OPENMIND_ALLOWED_HOSTS=' not in generate_settings()
