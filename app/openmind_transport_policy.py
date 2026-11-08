"""OpenMind transport policy for standard and explicitly enabled Demo modes.

Standard production: HTTPS and deployment-approved private HTTP exceptions.
Demo: no host allowlist, mandatory HTTPS, private-range or preflight-DNS policy.
Callers still validate the HTTP(S) URL contract and remote auth remains active.
Used consistently by saved-settings validation and actual HTTP clients.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit
from .demo_mode import enabled as demo_enabled


# Private infrastructure ranges only. `ip_address.is_private` by itself is
# broader (e.g. reserved / special-use ranges) and is not suitable here.
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    'fc00::/7', '::1/128',
))


def insecure_http_enabled() -> bool:
    return demo_enabled() or os.getenv('OPENMIND_ALLOW_INSECURE_HTTP', '').strip().lower() in ('true', '1')


def _allowed_private_address(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # IPv4-mapped IPv6 may otherwise appear in special IPv6 ranges.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return False
    return any(ip in network for network in _PRIVATE_NETWORKS)


def ensure_private_http_target(host: str) -> None:
    """Verify literal IP or *all* DNS A/AAAA answers are private.

    The DNS answer is also rechecked before each outgoing call. This prevents
    obvious public targets and most configuration mistakes but is NOT DNS
    pinning. For strict trust boundaries, configure a literal private IP.
    """
    from .openmind import OpenMindError

    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError) as exc:
            raise OpenMindError('内网 HTTP 主机无法解析；请检查容器 DNS，或直接使用私有 IP 地址') from exc
        addresses = {item[4][0] for item in answers}
        if not addresses or not all(_allowed_private_address(ip) for ip in addresses):
            raise OpenMindError('内网 HTTP 主机解析到非私有地址，禁止发送 OpenMind 凭据；请改用私有 IP 或 HTTPS')
    else:
        if not _allowed_private_address(host):
            raise OpenMindError('HTTP 兼容模式只允许 RFC1918/回环/IPv6 ULA 内网地址；禁止公网、链路本地或元数据地址')


def enforce_transport_policy(url: str, allowed_hosts: list[str] | set[str] | None) -> None:
    """Invoke *after* URL syntax / userinfo / path checks, with normalized hosts."""
    from .openmind import OpenMindError

    # Callers already enforce a valid HTTP(S) endpoint. Demo deliberately skips
    # the deployment whitelist, mandatory TLS, and private-address DNS gate.
    # Do not resolve DNS here; the actual network connection reports failures.
    if demo_enabled():
        return
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if os.getenv('APP_ENV') != 'production':
        return
    if p.scheme == 'https':
        if not allowed_hosts:
            raise OpenMindError('生产环境 OpenMind 必须配置目标主机白名单')
        return
    if p.scheme != 'http':
        raise OpenMindError('生产环境 OpenMind 地址协议必须是 HTTPS 或经授权的内网 HTTP')
    if not insecure_http_enabled():
        raise OpenMindError('生产环境 OpenMind 必须使用 HTTPS；可信内网 HTTP 需在 .env 明确设置 OPENMIND_ALLOW_INSECURE_HTTP=true')
    deployment_hosts = {x.strip().lower() for x in os.getenv('OPENMIND_ALLOWED_HOSTS', '').split(',') if x.strip()}
    # A saved UI whitelist cannot grant permission to a new HTTP destination.
    if not deployment_hosts or host not in deployment_hosts:
        raise OpenMindError('内网 HTTP 目标必须事先写入部署 .env 的 OPENMIND_ALLOWED_HOSTS 白名单')
    if not allowed_hosts or host not in {x.strip().lower() for x in allowed_hosts}:
        raise OpenMindError('OpenMind 页面白名单也必须包含内网 HTTP 目标主机')
    ensure_private_http_target(host)
