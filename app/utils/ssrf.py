"""拒绝用户提供的 http(s) URL 指向内网 / 本机 / 云元数据（SSRF 防护）。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.google.com",
        "metadata.internal",
    }
)

# 明确内网/回环/链路本地/组播。不包含 198.18.0.0/15：Clash 等 Fake-IP 会把公网域名解析到该段。
_BLOCKED_V4 = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("224.0.0.0/4"),
    ipaddress.IPv4Network("255.255.255.255/32"),
)
_BLOCKED_V6 = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("ff00::/8"),
)


class UnsafeUrlError(ValueError):
    """URL 协议非法或解析到不可达的内部地址。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _BLOCKED_V4)
    return any(ip in net for net in _BLOCKED_V6)


def _hostname_is_literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def assert_public_http_url(url: str, *, allow_private: bool | None = None) -> str:
    """
    校验 URL 为 http/https，且主机名不解析到私网/回环/链路本地/元数据地址。
    allow_private 为 None 时读取 settings.ALLOW_PRIVATE_UPSTREAM_URLS。
    """
    from app.settings import settings

    if allow_private is None:
        allow_private = bool(getattr(settings, "ALLOW_PRIVATE_UPSTREAM_URLS", False))

    s = (url or "").strip()
    parsed = urlparse(s)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("URL 仅支持 http/https 协议")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeUrlError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL 不允许包含用户名或密码")

    if host in _BLOCKED_HOSTS:
        if not allow_private:
            raise UnsafeUrlError("不允许指向内网或本机的 URL")
        return s

    literal = _hostname_is_literal_ip(host)
    if literal is not None:
        if _is_blocked_ip(literal) and not allow_private:
            raise UnsafeUrlError("不允许指向内网或本机的 URL")
        return s

    if allow_private:
        return s

    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("无法解析 URL 主机名") from exc
    if not infos:
        raise UnsafeUrlError("无法解析 URL 主机名")

    for info in infos:
        addr = info[4][0]
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise UnsafeUrlError("不允许指向内网或本机的 URL")
    return s
