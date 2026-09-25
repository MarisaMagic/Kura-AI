"""对话落库后的收尾任务：token 校准 → 后台预压缩 → 记忆归档 → 淘汰回收。

为什么要有这一层（B/S 与 Claude Code 本地形态的关键差异）：

- Claude Code 在请求内同步压缩，用户能看见「Compacted」提示；本项目若照搬，
  触发压缩的那一轮首 token 要晚 3~15 秒，SSE 体验明显劣化。
- 本项目会话状态全在后端库里，**可以提前算**：本轮落库后就按软阈值把摘要算好写入，
  下一轮请求只读不算，用户侧零额外延迟。
- 多副本部署下同一会话可能被不同进程处理，故用 Redis 互斥锁串行化，
  避免重复的 LLM 调用与 metadata 丢更新。

安全：队列载荷**不含 API Key / 提示词**，worker 侧按 agent_id 自行解密派生。
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from app.chat.cache import cache
from app.settings import settings

logger = logging.getLogger(__name__)

_QUEUE_KEY = "chat_post_turn:queue"
_PROCESSING_KEY = "chat_post_turn:processing"
_QUEUE_MAX = 5000

# Tortoise 0.23 的连接存储是「共享可变默认值」的 ContextVar：并发线程/事件循环会互相
# 看到并复用对方的 asyncpg 连接池。worker 是多线程的，故所有异步 DB 访问串行化。
_tortoise_lock = threading.Lock()


def _lock_key(user_id: int, agent_id: int, session_id: str) -> str:
    return f"chat_mem_lock:{int(user_id)}:{int(agent_id)}:{session_id}"


def _lock_ttl() -> int:
    try:
        return max(10, int(getattr(settings, "CHAT_MEMORY_LOCK_TTL_SECONDS", 120) or 120))
    except (TypeError, ValueError):
        return 120


def _task_mode() -> str:
    return str(getattr(settings, "CHAT_MEMORY_TASK_MODE", "thread") or "thread").strip().lower()


@contextmanager
def session_lock(user_id: int, agent_id: int, session_id: str):
    """会话级互斥锁：同一会话的压缩/归档串行化，避免重复 LLM 调用与状态互相覆盖。

    :return: 上下文管理器，yield True 表示抢到锁；False 表示已有进程在处理本会话
    """
    token = uuid.uuid4().hex
    key = _lock_key(user_id, agent_id, session_id)
    acquired = cache.set_nx(key, {"job_id": token}, _lock_ttl())
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete_if_job_matches(key, token)


def queue_depth() -> int:
    """待处理 + 处理中的收尾任务数（worker 观测用）。"""
    return cache.llen(_QUEUE_KEY) + cache.llen(_PROCESSING_KEY)


def queue_pending() -> int:
    """仅待处理（未出队）的收尾任务数。

    worker 用它判断是否需要做阻塞出队：若把 processing 也算进来，
    别的线程正在处理时其余空闲线程仍会进阻塞出队空等，拖慢 KB 队列。
    """
    return cache.llen(_QUEUE_KEY)


def enqueue_task(payload: dict[str, Any]) -> bool:
    """入队（queue 模式）；队列过长时丢弃，收尾任务可安全重算，不值得无限堆积。"""
    if queue_depth() >= _QUEUE_MAX:
        logger.warning("chat_post_turn 队列已满（%s），丢弃本次收尾任务", _QUEUE_MAX)
        return False
    return cache.rpush_json(_QUEUE_KEY, payload) > 0


def dequeue_task(timeout: int = 5) -> dict[str, Any] | None:
    """可靠出队：先进 processing 列表，ack 后移除；worker 崩溃可由 recover_stale 重投。"""
    payload = cache.brpoplpush_json(_QUEUE_KEY, _PROCESSING_KEY, timeout)
    return payload if isinstance(payload, dict) else None


def ack_task(payload: dict[str, Any]) -> None:
    cache.lrem_json(_PROCESSING_KEY, payload)


def recover_stale_processing() -> int:
    """把 processing 列表里残留的任务重新入队（worker 重启后调用）。

    非 dict 的残留条目直接丢弃：重新入队只会让 worker 再失败一次。
    """
    import json

    n = 0
    for raw in cache.lrange_str(_PROCESSING_KEY, 0, _QUEUE_MAX - 1):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            cache.lrem_raw(_PROCESSING_KEY, raw)
            continue
        if cache.rpush_json(_QUEUE_KEY, payload) > 0 and cache.lrem_raw(_PROCESSING_KEY, raw):
            n += 1
    return n


def schedule_post_turn(
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    llm_config: dict[str, Any] | None = None,
    context_window: Any = None,
    system_prompt: str = "",
    system_chars: int = 0,
    tools_tokens: int = 0,
    use_knowledge_retrieval: bool = False,
    use_web_search: bool = False,
    usage: Any = None,
    estimated: int = 0,
    model_name: str = "",
) -> None:
    """对话落库后调用：按配置走后台线程或 Redis 队列。

    :param llm_config: 打杂模型配置（含 api_key）。queue 模式下忽略，由 worker 自行派生
    :param usage: 本轮真实 usage_metadata，用于反推 token 校准系数
    :param estimated: 本轮发送前对输入 token 的估算值，与 usage 配对使用
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return

    from app.chat.context_budget import extract_input_tokens

    input_tokens = extract_input_tokens(usage)

    if _task_mode() == "queue":
        enqueue_task(
            {
                "kind": "chat_post_turn",
                "user_id": int(user_id),
                "agent_id": int(agent_id),
                "session_id": str(session_id),
                "context_window": int(context_window or 0),
                "system_chars": int(system_chars or 0),
                "tools_tokens": int(tools_tokens or 0),
                "use_knowledge_retrieval": bool(use_knowledge_retrieval),
                "use_web_search": bool(use_web_search),
                "input_tokens": int(input_tokens or 0),
                "estimated": int(estimated or 0),
                "model_name": str(model_name or "")[:100],
            }
        )
        return

    def _run() -> None:
        try:
            run_post_turn(
                user_id,
                agent_id,
                session_id,
                llm_config=llm_config or {},
                context_window=context_window,
                system_prompt=system_prompt,
                system_chars=system_chars,
                tools_tokens=tools_tokens,
                input_tokens=input_tokens,
                estimated=estimated,
                model_name=model_name,
            )
        except Exception:  # noqa: BLE001
            logger.exception("schedule_post_turn")

    if getattr(settings, "CHAT_MEMORY_ARCHIVE_ASYNC", True):
        threading.Thread(target=_run, name="chat-post-turn", daemon=True).start()
    else:
        _run()


def run_post_turn(
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    llm_config: dict[str, Any] | None = None,
    context_window: Any = None,
    system_prompt: str = "",
    system_chars: int = 0,
    tools_tokens: int = 0,
    input_tokens: int = 0,
    estimated: int = 0,
    model_name: str = "",
) -> dict[str, Any]:
    """收尾任务主体（持锁串行执行）。

    顺序有依赖：校准 → 预压缩（产出新段，决定原文窗口）→ 记忆归档（按窗口归档离窗轮次）。
    """
    info: dict[str, Any] = {"lock": False, "calibrated": False, "precompact": None, "archived": False}
    with session_lock(user_id, agent_id, session_id) as acquired:
        if not acquired:
            # 已有进程在处理同一会话：收尾任务可安全跳过，下一轮会重新评估
            logger.debug("post_turn 跳过（未抢到锁） session=%s", session_id)
            return info
        info["lock"] = True
        try:
            if input_tokens > 0 and estimated > 0:
                from app.chat.compact import record_usage_calibration

                record_usage_calibration(
                    user_id,
                    agent_id,
                    session_id,
                    usage={"input_tokens": int(input_tokens)},
                    estimated=int(estimated),
                    model_name=str(model_name or (llm_config or {}).get("model_name") or ""),
                )
                info["calibrated"] = True

            if getattr(settings, "CHAT_COMPACT_ENABLED", True):
                from app.chat.compact import precompute_compaction

                info["precompact"] = precompute_compaction(
                    user_id,
                    agent_id,
                    session_id,
                    llm_config=llm_config or {},
                    context_window=context_window,
                    system_prompt=system_prompt,
                    system_chars=system_chars,
                    tools_tokens=tools_tokens,
                )

            from app.chat.memory_archive import archive_session_memory

            archive_session_memory(user_id, agent_id, session_id)
            info["archived"] = True
        except Exception:  # noqa: BLE001
            logger.exception("run_post_turn failed")
    return info


def run_task_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """queue 模式下 worker 的执行入口：按 agent_id 派生 LLM 配置后调 run_post_turn。"""
    import asyncio

    user_id = int(payload.get("user_id") or 0)
    agent_id = int(payload.get("agent_id") or 0)
    session_id = str(payload.get("session_id") or "")
    if not (user_id and agent_id and session_id):
        return {"ok": False, "reason": "bad_payload"}

    from app.chat.agent_prompt import _compose_system_prompt
    from app.chat.agent_service import _sub_llm_config_from_ua
    from app.models.user_agent import UserAgent

    async def _load_and_close():
        """查一次 UserAgent；务必在同一循环内关掉连接池。

        asyncpg 连接池绑定创建它的事件循环：worker 每个任务都新起/关闭一个循环，
        若不关闭，下一个循环会复用绑定在已关闭循环上的池，第二个任务起就报错。
        （本函数只在 queue 模式的 worker 进程里被调用，关闭连接不会影响 API 进程。）
        """
        from tortoise import Tortoise

        try:
            return await UserAgent.filter(id=agent_id).first()
        finally:
            try:
                await Tortoise.close_connections()
            except Exception:  # noqa: BLE001
                logger.debug("Tortoise.close_connections 失败", exc_info=True)

    try:
        # 串行化并只在锁内起/关事件循环：避免多线程 worker 之间跨循环复用连接池
        with _tortoise_lock:
            try:
                ua = asyncio.run(_load_and_close())
            except RuntimeError:
                # 已处于运行中的事件循环（asyncio.run 拒绝重入）：另起一个循环跑完即关，
                # 不用 asyncio.get_event_loop()——它在 3.12+ 的非主线程里会直接报错。
                loop = asyncio.new_event_loop()
                try:
                    ua = loop.run_until_complete(_load_and_close())
                finally:
                    loop.close()
    except Exception:  # noqa: BLE001
        logger.exception("加载智能体配置失败 agent_id=%s", agent_id)
        return {"ok": False, "reason": "agent_load_failed"}
    if ua is None:
        return {"ok": False, "reason": "agent_missing"}

    system_prompt = _compose_system_prompt(
        ua,
        use_knowledge_retrieval=bool(payload.get("use_knowledge_retrieval")),
        use_web_search=bool(payload.get("use_web_search")),
    )
    return run_post_turn(
        user_id,
        agent_id,
        session_id,
        llm_config=_sub_llm_config_from_ua(ua),
        context_window=payload.get("context_window"),
        system_prompt=system_prompt,
        system_chars=len(system_prompt),
        tools_tokens=int(payload.get("tools_tokens") or 0),
        input_tokens=int(payload.get("input_tokens") or 0),
        estimated=int(payload.get("estimated") or 0),
        model_name=str(payload.get("model_name") or ""),
    )
