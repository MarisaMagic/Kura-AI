"""用户可配置上游的 pinned egress：先校验，再用校验所得 IP 建连，保留 Host/TLS SNI。"""

from __future__ import annotations

import socket
from typing import Iterable

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOStream
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream, NetworkBackend, NetworkStream
from httpcore._backends.sync import SyncBackend, SyncStream
from httpcore._exceptions import ConnectError

from app.utils.ssrf import ValidatedUpstream, validate_public_http_url

SOCKET_OPTION = tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]


def _socket_options(sock: socket.socket, socket_options: Iterable[SOCKET_OPTION] | None) -> None:
    for option in socket_options or ():
        sock.setsockopt(*option)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


class _PinnedNetworkBackend(NetworkBackend):
    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips
        self._delegate = SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        if not self._ips:
            return self._delegate.connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        source_address = None if local_address is None else (local_address, 0)
        last_exc: OSError | None = None
        for ip in self._ips:
            try:
                sock = socket.create_connection((ip, port), timeout, source_address=source_address)
                _socket_options(sock, socket_options)
                return SyncStream(sock)
            except OSError as exc:
                last_exc = exc
        raise ConnectError(str(last_exc or "no pinned address available")) from last_exc

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        return self._delegate.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class _PinnedAsyncNetworkBackend(AsyncNetworkBackend):
    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips
        self._delegate = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        if not self._ips:
            return await self._delegate.connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        last_exc: Exception | None = None
        for ip in self._ips:
            try:
                with anyio.fail_after(timeout):
                    stream = await anyio.connect_tcp(remote_host=ip, remote_port=port, local_host=local_address)
                raw = getattr(stream, "_raw_socket", None)
                if raw is not None:
                    _socket_options(raw, socket_options)
                return AnyIOStream(stream)
            except Exception as exc:
                last_exc = exc
        raise ConnectError(str(last_exc or "no pinned address available")) from last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, upstream: ValidatedUpstream, **kwargs) -> None:
        kwargs.setdefault("trust_env", False)
        super().__init__(**kwargs)
        pool = getattr(self, "_pool", None)
        if pool is not None and hasattr(pool, "_network_backend"):
            pool._network_backend = _PinnedNetworkBackend(upstream.ips)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, upstream: ValidatedUpstream, **kwargs) -> None:
        kwargs.setdefault("trust_env", False)
        super().__init__(**kwargs)
        pool = getattr(self, "_pool", None)
        if pool is not None and hasattr(pool, "_network_backend"):
            pool._network_backend = _PinnedAsyncNetworkBackend(upstream.ips)


def build_pinned_sync_client(
    url: str,
    *,
    timeout: httpx.Timeout | float | None = None,
    verify: bool | str = True,
) -> httpx.Client:
    """同步 pinned client；读页等同步路径不要创建 AsyncClient。"""
    upstream = validate_public_http_url(url)
    from app.settings import settings

    if not bool(getattr(settings, "EGRESS_PIN_DNS", True)):
        return httpx.Client(timeout=timeout, verify=verify, trust_env=False, follow_redirects=False)
    return httpx.Client(
        transport=PinnedHTTPTransport(upstream, verify=verify),
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


def build_pinned_clients(
    url: str,
    *,
    timeout: httpx.Timeout | float | None = None,
    verify: bool | str = True,
) -> tuple[httpx.Client, httpx.AsyncClient]:
    upstream = validate_public_http_url(url)
    from app.settings import settings

    if not bool(getattr(settings, "EGRESS_PIN_DNS", True)):
        return (
            httpx.Client(timeout=timeout, verify=verify, trust_env=False, follow_redirects=False),
            httpx.AsyncClient(timeout=timeout, verify=verify, trust_env=False, follow_redirects=False),
        )
    sync_transport = PinnedHTTPTransport(upstream, verify=verify)
    async_transport = PinnedAsyncHTTPTransport(upstream, verify=verify)
    sync_client = httpx.Client(
        transport=sync_transport,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )
    async_client = httpx.AsyncClient(
        transport=async_transport,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )
    return sync_client, async_client


def build_mcp_httpx_client_factory(url: str):
    upstream = validate_public_http_url(url)
    from app.settings import settings

    pin_enabled = bool(getattr(settings, "EGRESS_PIN_DNS", True))

    def _factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(upstream) if pin_enabled else None,
            headers=headers,
            timeout=timeout,
            auth=auth,
            trust_env=False,
            follow_redirects=False,
        )

    return _factory


def pinned_llm_client_kwargs(base_url: str | None) -> dict:
    if not base_url:
        return {}
    sync_client, async_client = build_pinned_clients(base_url)
    return {"http_client": sync_client, "http_async_client": async_client}


def assert_httpx_response_public(response: httpx.Response) -> None:
    if response.is_redirect:
        location = response.headers.get("location")
        if location:
            validate_public_http_url(str(response.url.join(location)))
