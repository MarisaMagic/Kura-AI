"""
上游配额保护层（针对个人账号配额有限的 embedding / rerank 上游）。

设计：
- ``QuotaGuard``：基于 Redis Lua 的全局闸门，**跨 API 副本与 kb-worker 进程共享**，
  同时约束「在途并发」与「QPS」两个维度；
- 熔断器：连续失败（429 / 限流）达到阈值后短路该上游调用，冷却期后放行探测；
- 快速降级：等待额度超时或熔断打开时立即抛错，由调用方降级（rerank 走向量分、
  检索返回「知识库繁忙」），不阻塞对话主链路；
- Redis 不可用时放行并告警（与限流同策略），避免知识库依赖单点。

使用：
    with embedding_quota().acquire_sync():   # worker / 线程内
        call_upstream()
    async with rerank_quota().acquire():      # 事件循环（内部 to_thread）
        ...
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator, Optional

from app.log import logger


class QuotaTimeoutError(RuntimeError):
    """等待上游配额超时（应快速降级）。"""


class QuotaBreakerOpenError(RuntimeError):
    """上游熔断已打开（连续失败后短路，应快速降级）。"""


# Redis 不可用时的进程内冷却：避免每个配额检查都踩连接超时（单次可长达数秒）
_redis_down_until = 0.0
_REDIS_DOWN_COOLDOWN_SECONDS = 30.0


def _redis_usable() -> bool:
    return time.monotonic() >= _redis_down_until


def _mark_redis_down() -> None:
    global _redis_down_until
    _redis_down_until = time.monotonic() + _REDIS_DOWN_COOLDOWN_SECONDS


# 原子获取配额：并发 + QPS 双维度检查通过才占用
_ACQUIRE_LUA = """
if redis.call('EXISTS', KEYS[3]) == 1 then
  return -1
end
local inflight = tonumber(redis.call('GET', KEYS[1]) or '0')
if inflight >= tonumber(ARGV[1]) then
  return 0
end
local now = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, now - tonumber(ARGV[4]))
local qps = tonumber(ARGV[2])
if qps > 0 and redis.call('ZCARD', KEYS[2]) >= qps then
  return 0
end
redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[5]))
if qps > 0 then
  redis.call('ZADD', KEYS[2], now, ARGV[6])
  redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[4]) + 5000)
end
return 1
"""

# 记录一次失败：连续失败达阈值即打开熔断
_FAIL_LUA = """
local n = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
if n >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
end
return n
"""


class QuotaGuard:
    """Redis 全局配额闸门（同步核心 + 异步包装）。线程安全。"""

    def __init__(
        self,
        name: str,
        *,
        max_concurrency: int,
        qps: int,
        wait_timeout: float,
        breaker_fails: int,
        breaker_open_seconds: float,
    ) -> None:
        self.name = name
        self.max_concurrency = max(1, int(max_concurrency))
        self.qps = max(0, int(qps))
        self.wait_timeout = max(0.5, float(wait_timeout))
        self.breaker_fails = max(1, int(breaker_fails))
        self.breaker_open_seconds = max(1.0, float(breaker_open_seconds))
        self._held = threading.local()

    # ---------------------------------------------------------------- keys helper

    def _keys(self) -> tuple[str, str, str, str]:
        from app.settings import settings

        prefix = f"{settings.REDIS_KEY_PREFIX}:quota:{self.name}"
        return (
            f"{prefix}:inflight",
            f"{prefix}:qps",
            f"{prefix}:breaker",
            f"{prefix}:fails",
        )

    def _client(self):
        from app.chat.cache import get_redis_client

        return get_redis_client()

    # ---------------------------------------------------------------- sync core

    def _try_acquire(self) -> int:
        """单次原子尝试：1 成功；0 满/限流；-1 熔断打开。Redis 异常时返回 1（放行）。"""
        if not _redis_usable():
            return 1
        inflight_key, qps_key, breaker_key, _ = self._keys()
        try:
            result = self._client().eval(
                _ACQUIRE_LUA,
                3,
                inflight_key,
                qps_key,
                breaker_key,
                str(self.max_concurrency),
                str(self.qps),
                str(int(time.time() * 1000)),
                "1000",
                "30",
                uuid.uuid4().hex,
            )
            return int(result)
        except Exception as exc:
            _mark_redis_down()
            logger.warning("quota[{}] acquire 失败（Redis 不可用，放行并冷却 30s）: {}", self.name, exc)
            return 1

    def acquire_sync(self) -> None:
        """
        阻塞式获取配额；等待超过 wait_timeout 抛 QuotaTimeoutError；
        熔断打开立即抛 QuotaBreakerOpenError。获取成功须在 finally 中 release_sync()。
        """
        deadline = time.monotonic() + self.wait_timeout
        delay = 0.05
        while True:
            state = self._try_acquire()
            if state == 1:
                return
            if state == -1:
                raise QuotaBreakerOpenError(
                    f"{self.name} 上游已熔断（连续失败），请稍后重试"
                )
            if time.monotonic() >= deadline:
                raise QuotaTimeoutError(
                    f"等待 {self.name} 配额超过 {self.wait_timeout:.0f} 秒"
                )
            time.sleep(delay)
            delay = min(delay * 1.6, 0.4)

    def release_sync(self) -> None:
        inflight_key, _, _, _ = self._keys()
        if not _redis_usable():
            return
        try:
            client = self._client()
            value = int(client.get(inflight_key) or 0)
            if value > 0:
                client.decr(inflight_key)
        except Exception as exc:
            _mark_redis_down()
            logger.debug("quota[{}] release 失败: {}", self.name, exc)

    @contextmanager
    def hold_sync(self) -> Iterator[None]:
        """上下文管理器：获取配额，退出（含异常）时释放。"""
        self.acquire_sync()
        try:
            yield
        finally:
            self.release_sync()

    def record_failure(self) -> None:
        """记录一次上游限流/失败；连续达到阈值时打开熔断。"""
        if not _redis_usable():
            return
        _, _, breaker_key, fails_key = self._keys()
        try:
            self._client().eval(
                _FAIL_LUA,
                2,
                fails_key,
                breaker_key,
                str(max(60, int(self.breaker_open_seconds * 4))),
                str(self.breaker_fails),
                str(int(self.breaker_open_seconds)),
            )
            logger.warning("quota[{}] 记录一次上游失败（达 {} 次将熔断）", self.name, self.breaker_fails)
        except Exception as exc:
            _mark_redis_down()
            logger.debug("quota[{}] record_failure 失败: {}", self.name, exc)

    def record_success(self) -> None:
        """成功后清零连续失败计数。"""
        if not _redis_usable():
            return
        _, _, _, fails_key = self._keys()
        try:
            self._client().delete(fails_key)
        except Exception:
            _mark_redis_down()

    def usage(self) -> dict[str, Any]:
        """当前用量（status 端点用）。"""
        inflight_key, qps_key, breaker_key, _ = self._keys()
        if not _redis_usable():
            return self._usage_unavailable()
        try:
            client = self._client()
            pipe = client.pipeline()
            pipe.get(inflight_key)
            pipe.zcard(qps_key)
            pipe.exists(breaker_key)
            inflight, qps_used, breaker = pipe.execute()
            return {
                "name": self.name,
                "max_concurrency": self.max_concurrency,
                "qps_limit": self.qps,
                "inflight": int(inflight or 0),
                "qps_window_used": int(qps_used or 0),
                "breaker_open": bool(breaker),
            }
        except Exception:
            _mark_redis_down()
            return self._usage_unavailable()

    def _usage_unavailable(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_concurrency": self.max_concurrency,
            "qps_limit": self.qps,
            "inflight": None,
            "qps_window_used": None,
            "breaker_open": None,
        }

    # ---------------------------------------------------------------- async API

    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None) -> AsyncIterator[None]:
        """
        异步获取配额（内部每次尝试走线程池，等待期间让出事件循环）。
        获取成功退出上下文时自动释放。
        """
        deadline = time.monotonic() + (self.wait_timeout if timeout is None else max(0.5, float(timeout)))
        delay = 0.05
        while True:
            state = await asyncio.to_thread(self._try_acquire)
            if state == 1:
                break
            if state == -1:
                raise QuotaBreakerOpenError(f"{self.name} 上游已熔断（连续失败），请稍后重试")
            if time.monotonic() >= deadline:
                raise QuotaTimeoutError(f"等待 {self.name} 配额超过 {self.wait_timeout:.0f} 秒")
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 0.4)
        try:
            yield
        finally:
            await asyncio.to_thread(self.release_sync)


# ------------------------------------------------------------------ 模块级单例

_guards_lock = threading.Lock()
_guards: dict[str, QuotaGuard] = {}


def get_quota_guard(name: str, *, concurrency_setting: str, qps_setting: str) -> QuotaGuard:
    guard = _guards.get(name)
    if guard is not None:
        return guard
    from app.settings import settings

    with _guards_lock:
        guard = _guards.get(name)
        if guard is not None:
            return guard
        guard = QuotaGuard(
            name,
            max_concurrency=int(getattr(settings, concurrency_setting, 2) or 2),
            qps=int(getattr(settings, qps_setting, 0) or 0),
            wait_timeout=float(getattr(settings, "UPSTREAM_QUOTA_WAIT_SECONDS", 8.0) or 8.0),
            breaker_fails=int(getattr(settings, "UPSTREAM_BREAKER_FAILS", 3) or 3),
            breaker_open_seconds=float(getattr(settings, "UPSTREAM_BREAKER_OPEN_SECONDS", 60.0) or 60.0),
        )
        _guards[name] = guard
        return guard


def embedding_quota() -> QuotaGuard:
    """嵌入上游全局配额（跨 API 副本与 kb-worker 共享）。"""
    return get_quota_guard(
        "embedding",
        concurrency_setting="EMBEDDING_GLOBAL_CONCURRENCY",
        qps_setting="EMBEDDING_GLOBAL_QPS",
    )


def rerank_quota() -> QuotaGuard:
    """重排上游全局配额。"""
    return get_quota_guard(
        "rerank",
        concurrency_setting="RERANK_GLOBAL_CONCURRENCY",
        qps_setting="RERANK_GLOBAL_QPS",
    )


def all_quota_usage() -> list[dict[str, Any]]:
    """全部已初始化的配额用量（status 端点用）。"""
    return [g.usage() for g in list(_guards.values())]
