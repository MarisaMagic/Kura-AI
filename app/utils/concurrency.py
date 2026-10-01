"""
进程内并发基础设施（阶段 1）：

- 默认线程池扩容：``asyncio.to_thread`` 与 LangChain 同步工具（langchain-core 的
  ``run_in_executor`` 未显式指定 executor 时走事件循环默认执行器）共享同一线程池，
  需显式放大并防止被长任务占满；
- ``run_sync``：统一同步调用 offload，带超时与残留阻塞点日志；
- ``llm_slot``：LLM 并发闸门（/chat、/chat/stream、/chat/jobs 三入口共用），
  控制单进程内同时在途的对话生成数；
- ``tool_burst_slot``：工具长任务闸门，限制分钟级同步工具（RAG/联网搜索等）
  同时占用线程数，为 DB/Redis 等短任务保留线程。

注意：所有闸门均为进程内状态；多副本部署时全局上限 = 各副本之和，
需按副本数分摊配置（见 .env.example）。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional, TypeVar

from app.log import logger

T = TypeVar("T")

# ------------------------------------------------------------------ 默认线程池

_executor_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def setup_default_executor(max_workers: Optional[int] = None) -> ThreadPoolExecutor:
    """
    设置事件循环默认线程池（进程内幂等，仅首次生效）。

    必须在事件循环内调用（lifespan 启动期）。线程池由 ``asyncio.to_thread``、
    LangChain 同步工具执行与 ``run_sync`` 共用；大小按「同时在途流数 ×
    每流同步任务数 + 短任务余量」估算。

    :param max_workers: 线程数；缺省读 settings.DEFAULT_EXECUTOR_MAX_WORKERS
    :return: 线程池实例
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            return _executor
        if max_workers is None:
            try:
                from app.settings import settings

                max_workers = int(getattr(settings, "DEFAULT_EXECUTOR_MAX_WORKERS", 96) or 96)
            except Exception:  # pragma: no cover - settings 不可用时的兜底
                max_workers = 96
        max_workers = max(8, int(max_workers))
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="app-io")
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        _executor = executor
        logger.info("默认线程池已设置：max_workers={}", max_workers)
        return executor


def default_executor() -> Optional[ThreadPoolExecutor]:
    """当前默认线程池（未初始化返回 None，测试/诊断用）。"""
    return _executor


async def run_sync(
    fn: Callable[..., T],
    /,
    *args: Any,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> T:
    """
    在默认线程池执行同步函数（统一 offload 入口）。

    :param fn: 同步函数；须自行创建/关闭 DB/Redis 会话（不得跨线程传递 ORM 对象）
    :param timeout: 等待上限（秒）；超时仅返回控制权，线程内调用仍会继续至结束
    :raises TimeoutError: 超时（调用方应按失败处理并提示重试）
    """
    coro = asyncio.to_thread(fn, *args, **kwargs)
    if timeout is None:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=float(timeout))
    except asyncio.TimeoutError:
        logger.warning(
            "run_sync 超时（{:.1f}s）：{}.{}（线程内调用可能仍在执行）",
            float(timeout),
            getattr(fn, "__module__", "?"),
            getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn))),
        )
        raise


# ------------------------------------------------------------------ LLM 并发闸门


class LLMGateTimeout(RuntimeError):
    """等待 LLM 并发额度超时（服务繁忙）。"""


_llm_gate: Optional[asyncio.Semaphore] = None
_llm_gate_loop: Optional[asyncio.AbstractEventLoop] = None
_llm_gate_lock = threading.Lock()
# 正在等待闸门的调用数（仅用于排队提示，不参与调度）
_llm_gate_waiting = 0
# 已持有闸门、正在生成的数量
_llm_inflight = 0


def _llm_limit() -> int:
    from app.settings import settings

    return max(1, int(getattr(settings, "LLM_MAX_INFLIGHT", 8) or 8))


def _llm_semaphore() -> asyncio.Semaphore:
    """当前事件循环对应的闸门信号量（跨 loop 自动重建，兼容测试多 loop）。"""
    global _llm_gate, _llm_gate_loop
    loop = asyncio.get_running_loop()
    with _llm_gate_lock:
        if _llm_gate is None or _llm_gate_loop is not loop:
            _llm_gate = asyncio.Semaphore(_llm_limit())
            _llm_gate_loop = loop
        return _llm_gate


def _queue_timeout() -> float:
    from app.settings import settings

    return max(1.0, float(getattr(settings, "LLM_QUEUE_TIMEOUT_SECONDS", 120) or 120))


def get_llm_gate_stats() -> dict[str, int]:
    """闸门状态（status 端点/排队提示用）。优先读信号量真实剩余额度。"""
    sem = _llm_gate
    limit = _llm_limit()
    if sem is None:
        return {"limit": limit, "inflight": 0, "waiting": 0}
    free = getattr(sem, "_value", None)
    if isinstance(free, int):
        inflight = max(0, limit - free)
    else:  # pragma: no cover - 实现细节变化时的兜底
        inflight = _llm_inflight
    return {"limit": limit, "inflight": inflight, "waiting": _llm_gate_waiting}


def _reset_llm_gate_for_tests(sem: Optional[asyncio.Semaphore] = None) -> None:
    """
    测试专用：重置闸门状态；传入自定义信号量时绑定当前事件循环。
    （生产代码不应调用。）
    """
    global _llm_gate, _llm_gate_loop, _llm_gate_waiting, _llm_inflight
    _llm_gate = sem
    try:
        _llm_gate_loop = asyncio.get_running_loop() if sem is not None else None
    except RuntimeError:
        _llm_gate_loop = None
    _llm_gate_waiting = 0
    _llm_inflight = 0


@asynccontextmanager
async def llm_slot(*, timeout: Optional[float] = None) -> AsyncIterator[None]:
    """
    LLM 并发闸门上下文：获取额度后才能开始生成，退出（含取消）时释放。

    :param timeout: 排队等待上限；缺省用 settings.LLM_QUEUE_TIMEOUT_SECONDS
    :raises LLMGateTimeout: 排队超时
    """
    global _llm_gate_waiting, _llm_inflight
    wait = _queue_timeout() if timeout is None else max(1.0, float(timeout))
    sem = _llm_semaphore()
    _llm_gate_waiting += 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=wait)
            acquired = True
        except asyncio.TimeoutError as e:
            raise LLMGateTimeout(f"服务繁忙，排队等待超过 {wait:.0f} 秒") from e
        _llm_inflight += 1
        yield
    finally:
        _llm_gate_waiting -= 1
        if acquired:
            _llm_inflight -= 1
            sem.release()


# ------------------------------------------------------------------ 工具长任务闸门


class ToolBurstTimeout(RuntimeError):
    """等待工具并发额度超时。"""


_tool_gate: Optional[asyncio.Semaphore] = None
_tool_gate_loop: Optional[asyncio.AbstractEventLoop] = None
_tool_gate_lock = threading.Lock()
_tool_gate_waiting = 0
_tool_inflight = 0


def _tool_limit() -> int:
    from app.settings import settings

    return max(1, int(getattr(settings, "TOOL_MAX_INFLIGHT", 24) or 24))


def _tool_semaphore() -> asyncio.Semaphore:
    global _tool_gate, _tool_gate_loop
    loop = asyncio.get_running_loop()
    with _tool_gate_lock:
        if _tool_gate is None or _tool_gate_loop is not loop:
            _tool_gate = asyncio.Semaphore(_tool_limit())
            _tool_gate_loop = loop
        return _tool_gate


def get_tool_gate_stats() -> dict[str, int]:
    return {"limit": _tool_limit(), "inflight": _tool_inflight, "waiting": _tool_gate_waiting}


@asynccontextmanager
async def tool_burst_slot(*, timeout: Optional[float] = None) -> AsyncIterator[None]:
    """
    工具长任务闸门：包住耗时工具调用（RAG 检索等），防止占满默认线程池。

    注意：LangChain 内部直接执行同步工具，无法从外部逐调用包装；
    该闸门用于本项目显式调用工具链的路径（如 KB 预选、手动压缩等），
    并作为后续工具 async 化的限制器。
    """
    global _tool_gate_waiting, _tool_inflight
    wait = _queue_timeout() if timeout is None else max(1.0, float(timeout))
    sem = _tool_semaphore()
    _tool_gate_waiting += 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=wait)
            acquired = True
        except asyncio.TimeoutError as e:
            raise ToolBurstTimeout(f"工具并发额度等待超过 {wait:.0f} 秒") from e
        _tool_inflight += 1
        yield
    finally:
        _tool_gate_waiting -= 1
        if acquired:
            _tool_inflight -= 1
            sem.release()
