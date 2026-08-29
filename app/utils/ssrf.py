"""拒绝用户提供的 http(s) URL 指向内网 / 本机 / 云元数据（SSRF 防护）。"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.google.com",
        "metadata.internal",
        "metadata.azure.internal",
        "instance-data.ec2.internal",
    }
)

# 明确内网/回环/链路本地/组播/CGNAT/文档保留段。不包含 198.18.0.0/15：Clash 等 Fake-IP 会把公网域名解析到该段。
_BLOCKED_V4 = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.0.0.0/24"),
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("224.0.0.0/4"),
    ipaddress.IPv4Network("240.0.0.0/4"),
    ipaddress.IPv4Network("255.255.255.255/32"),
)
_BLOCKED_V6 = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
    ipaddress.IPv6Network("2001:db8::/32"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("fec0::/10"),
    ipaddress.IPv6Network("ff00::/8"),
)


class UnsafeUrlError(ValueError):
    """URL 协议非法或解析到不可达的内部地址。"""


@dataclass(frozen=True)
class ValidatedUpstream:
    """已通过 SSRF 校验的出站地址；ips 供 pinned egress 直接连接。"""

    url: str
    scheme: str
    host: str
    port: int
    ips: tuple[str, ...]
    allow_private: bool


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


def _resolve_host_ips(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("无法解析 URL 主机名") from exc
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        ips.append(str(ip))
    return tuple(dict.fromkeys(ips))


def validate_public_http_url(url: str, *, allow_private: bool | None = None) -> ValidatedUpstream:
    """
    校验 URL 为 http(s)，且主机名不解析到私网/回环/链路本地/元数据地址。
    allow_private 为 None 时读取 settings.ALLOW_PRIVATE_UPSTREAM_URLS。
    """
    from app.settings import settings

    if allow_private is None:
        allow_private = bool(getattr(settings, "ALLOW_PRIVATE_UPSTREAM_URLS", False))

    s = (url or "").strip()
    parsed = urlparse(s)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("URL 仅支持 http(s) 协议")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeUrlError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL 不得包含用户名或密码")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    literal = _hostname_is_literal_ip(host)
    if literal is not None:
        if _is_blocked_ip(literal) and not allow_private:
            raise UnsafeUrlError("禁止指向内网或本机的 URL")
        return ValidatedUpstream(s, parsed.scheme, host, port, (str(literal),), allow_private)

    if host in _BLOCKED_HOSTS and not allow_private:
        raise UnsafeUrlError("禁止指向内网或本机的 URL")

    ips = _resolve_host_ips(host, port)
    if not ips:
        raise UnsafeUrlError("无法解析 URL 主机名")
    if not allow_private:
        for raw in ips:
            if _is_blocked_ip(ipaddress.ip_address(raw)):
                raise UnsafeUrlError("禁止指向内网或本机的 URL")
    return ValidatedUpstream(s, parsed.scheme, host, port, ips, allow_private)


def assert_public_http_url(url: str, *, allow_private: bool | None = None) -> str:
    return validate_public_http_url(url, allow_private=allow_private).url
