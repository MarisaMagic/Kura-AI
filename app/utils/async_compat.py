"""异步存储的 Windows 兼容层。

psycopg 异步驱动仅支持 Selector 事件循环；Windows 默认 ProactorEventLoop 下会抛
``InterfaceError``（生产 Linux 无此限制）。

策略：
- ``async_pg_unsupported()``：检测当前事件循环是否不支持 psycopg 异步；
- ``sync_fallback(name)``：装饰 storage/附件等的异步方法——不支持时自动退化为
  ``asyncio.to_thread(同步实现)``（Windows 开发环境行为与旧版一致，Linux 走真异步）。

这样生产（Linux）使用全异步路径，Windows 本地开发/测试保持可用（线程池执行）。
"""

from __future__ import annotations

import asyncio
import functools
import sys
from typing import Any, Callable


def async_pg_unsupported() -> bool:
    """当前事件循环是否不支持 psycopg 异步（Windows Proactor 返回 True）。"""
    if sys.platform != "win32":
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return not isinstance(loop, asyncio.SelectorEventLoop)


def sync_fallback(sync_name: str) -> Callable:
    """
    异步方法装饰器：当前 loop 不支持 psycopg 异步时，在线程池执行同名同步实现。

    :param sync_name: 对应的同步方法名（须存在且签名兼容）
    """

    def _decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        async def _wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if async_pg_unsupported():
                return await asyncio.to_thread(getattr(self, sync_name), *args, **kwargs)
            return await func(self, *args, **kwargs)

        return _wrapper

    return _decorate


def sync_fallback_fn(sync_fn: Callable) -> Callable:
    """
    模块级异步函数装饰器：当前 loop 不支持 psycopg 异步时，在线程池执行同步实现。

    :param sync_fn: 对应的同步函数（签名兼容）
    """

    def _decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        async def _wrapper(*args: Any, **kwargs: Any) -> Any:
            if async_pg_unsupported():
                return await asyncio.to_thread(sync_fn, *args, **kwargs)
            return await func(*args, **kwargs)

        return _wrapper

    return _decorate