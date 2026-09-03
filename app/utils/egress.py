"""用户可配置上游的 pinned egress：先校验，再用校验所得 IP 建连，保留 Host/TLS SNI。"""

from __future__ import annotations

import socket
import threading
from collections import OrderedDict
from typing import Iterable

import anyio
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


def llm_http_timeout() -> httpx.Timeout:
    """LLM 上游显式超时，禁止 timeout=None。"""
    from app.settings import settings

    return httpx.Timeout(
        connect=float(getattr(settings, "LLM_HTTP_CONNECT_TIMEOUT", 10) or 10),
        read=float(getattr(settings, "LLM_HTTP_READ_TIMEOUT", 120) or 120),
        write=float(getattr(settings, "LLM_HTTP_WRITE_TIMEOUT", 30) or 30),
        pool=float(getattr(settings, "LLM_HTTP_POOL_TIMEOUT", 10) or 10),
    )


# 进程级 LLM 客户端注册表：同一 base_url 复用 (sync, async) httpx 客户端，
# 避免每次 init_chat_model 都新建连接池（9 处调用点）。api_key 由 OpenAI SDK
# 每请求注入，共享客户端安全；经核实 OpenAI/LangChain 均无 __del__/close 钩子，
# 不会在使用中关闭共享客户端。
_LLM_CLIENT_CACHE: OrderedDict[str, tuple[httpx.Client, httpx.AsyncClient]] = OrderedDict()
_LLM_CLIENT_CACHE_LOCK = threading.Lock()
_LLM_CLIENT_CACHE_MAX = 64


def _build_pinned_llm_clients(base_url: str) -> tuple[httpx.Client, httpx.AsyncClient]:
    return build_pinned_clients(base_url, timeout=llm_http_timeout())


def get_or_build_pinned_llm_clients(base_url: str) -> tuple[httpx.Client, httpx.AsyncClient]:
    key = base_url
    with _LLM_CLIENT_CACHE_LOCK:
        cached = _LLM_CLIENT_CACHE.get(key)
        if cached is not None:
            _LLM_CLIENT_CACHE.move_to_end(key)
            return cached
    # 建连（含 DNS 校验）放在锁外，避免阻塞其它调用
    sync_client, async_client = _build_pinned_llm_clients(base_url)
    with _LLM_CLIENT_CACHE_LOCK:
        # 双重检查：并发下可能已有其它线程插入同一 key
        cached = _LLM_CLIENT_CACHE.get(key)
        if cached is not None:
            _LLM_CLIENT_CACHE.move_to_end(key)
            try:
                sync_client.close()
            except Exception:
                pass
            # 未启用的 AsyncClient 无活跃连接，交给 GC 回收即可
            return cached
        _LLM_CLIENT_CACHE[key] = (sync_client, async_client)
        _LLM_CLIENT_CACHE.move_to_end(key)
        while len(_LLM_CLIENT_CACHE) > _LLM_CLIENT_CACHE_MAX:
            _, (old_sync, _old_async) = _LLM_CLIENT_CACHE.popitem(last=False)
            try:
                old_sync.close()
            except Exception:
                pass
    return sync_client, async_client


async def close_pinned_llm_clients() -> None:
    """关停时统一关闭缓存的 LLM 客户端（挂到 lifespan 收尾）。"""
    with _LLM_CLIENT_CACHE_LOCK:
        items = list(_LLM_CLIENT_CACHE.values())
        _LLM_CLIENT_CACHE.clear()
    for sync_client, async_client in items:
        try:
            sync_client.close()
        except Exception:
            pass
        try:
            await async_client.aclose()
        except Exception:
            pass


def pinned_llm_client_kwargs(base_url: str | None) -> dict:
    if not base_url:
        return {}
    sync_client, async_client = get_or_build_pinned_llm_clients(base_url)
    return {"http_client": sync_client, "http_async_client": async_client}


def assert_httpx_response_public(response: httpx.Response) -> None:
    if response.is_redirect:
        location = response.headers.get("location")
        if location:
            validate_public_http_url(str(response.url.join(location)))
