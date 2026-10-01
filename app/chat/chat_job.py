"""
智能体流式对话异步 Job：后台执行生成，事件写入 Redis，支持断线后按 seq 重连 SSE。
用于将对话任务放在后台执行，不会被用户的其它请求打断。

阶段 4：全异步 Redis（``cache.a*``）——事件追加/元数据读写/锁操作直接 await 异步客户端，
不再占用默认线程池（jobs 热路径每轮含 N 次事件写 + 高频状态轮询）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from app.chat.agent_service import iter_chat_stream_events
from app.chat.cache import cache
from app.chat.preview_session import is_editor_preview_session
from app.controllers.user_agent import user_agent_controller
from app.settings import settings
from app.utils.concurrency import LLMGateTimeout, get_llm_gate_stats, llm_slot

# 持有运行中的 Job Task 强引用：任务生命周期不依赖事件循环的弱引用，
# 避免调度间隙被 GC 回收导致 Job 静默消失。
_JOB_TASKS: set[asyncio.Task] = set()


def _meta_key(job_id: str) -> str:
    """
    获取 Job 元数据 key
    """
    return f"chat_job:{job_id}:meta"


def _events_key(job_id: str) -> str:
    """
    获取 Job 事件 key
    """
    return f"chat_job:{job_id}:events"


def _active_key(user_id: int, agent_id: int, session_id: str) -> str:
    """
    获取 Job 活动 key
    """
    return f"chat_job_active:{user_id}:{agent_id}:{session_id}"


def _cancel_key(job_id: str) -> str:
    """用户请求停止生成时写入的标记 key。"""
    return f"chat_job:{job_id}:cancel"


async def is_job_cancel_requested(job_id: str) -> bool:
    """是否已请求取消该 Job（异步读缓存）。"""
    raw = await cache.aget_json(_cancel_key(job_id))
    return bool(raw)


async def get_running_session_job(user_id: int, agent_id: int, session_id: str) -> dict | None:
    """返回该会话当前 running 的 Job 元数据；无则 None（用于切分支前的冲突检查）。"""
    existing = await cache.aget_json(_active_key(user_id, agent_id, session_id))
    if not isinstance(existing, dict) or not existing.get("job_id"):
        return None
    meta = await get_job_meta(str(existing["job_id"]))
    if meta and meta.get("status") == "running":
        return meta
    return None


async def _release_active_key(user_id: int, agent_id: int, session_id: str, job_id: str) -> None:
    """
    释放会话占用锁：仅当锁仍指向该 job 时才删除，
    避免旧任务退出时误删新任务的占用锁。
    """
    await cache.adelete_if_job_matches(_active_key(user_id, agent_id, session_id), job_id)

async def request_chat_job_cancel(job_id: str) -> None:
    """
    标记 Job 为「用户请求停止」并即时终结：
    1. 写取消标记，供生成协程协作中断；
    2. 若任务仍 running，立即置为 cancelled 并释放会话占用锁，
       使用户停止后可立刻发起新任务，不必等待旧任务完全退出。
    """
    await cache.aset_json(_cancel_key(job_id), {"v": 1}, _ttl())
    meta = await cache.aget_json(_meta_key(job_id))
    if not isinstance(meta, dict) or meta.get("status") != "running":
        return
    meta["status"] = "cancelled"
    meta["error"] = None
    await cache.aset_json(_meta_key(job_id), meta, _ttl())
    await _release_active_key(
        int(meta.get("user_id", 0)),
        int(meta.get("agent_id", 0)),
        str(meta.get("session_id", "")),
        job_id,
    )

async def cancel_active_session_job(user_id: int, agent_id: int, session_id: str) -> bool:
    """
    按会话取消当前活动任务（前端停止时 job_id 未知的兜底，如创建请求在途被中断）。
    :return: 是否实际取消了任务
    """
    existing = await cache.aget_json(_active_key(user_id, agent_id, session_id))
    if not isinstance(existing, dict) or not existing.get("job_id"):
        return False
    job_id = str(existing["job_id"])
    meta = await cache.aget_json(_meta_key(job_id))
    if not meta or int(meta.get("user_id", -1)) != int(user_id):
        return False
    if meta.get("status") != "running":
        return False
    await request_chat_job_cancel(job_id)
    return True


def _ttl() -> int:
    """
    获取 Job 过期时间
    """
    return int(getattr(settings, "CHAT_JOB_TTL_SECONDS", 86400))


def _done_ttl() -> int:
    """Job 终态后的过期时间：只需覆盖断线重连与迟到追更，远小于 running 期。"""
    return int(getattr(settings, "CHAT_JOB_DONE_TTL_SECONDS", 3600) or 3600)


async def _append_event(job_id: str, seq: int, data: dict[str, Any]) -> None:
    """
    追加 Job 事件：RPUSH + meta/events 双 EXPIRE 经单次 Lua 往返完成（异步客户端）。
    """
    wrapped = json.dumps({"seq": seq, "data": data}, ensure_ascii=False)
    await cache.aappend_event_atomic(
        _events_key(job_id),
        _meta_key(job_id),
        wrapped,
        _ttl(),
    )


async def create_chat_job(
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    message: str,
    use_knowledge_retrieval: bool,
    use_web_search: bool = False,
    attachment_ids: list[str] | None = None,
    regenerate: bool = False,
    target_message_id: int | None = None,
    mcp_approved_pending_id: str | None = None,
) -> tuple[str, bool]:
    """
    创建 Job：若同会话已有 running 任务则返回 (existing_job_id, True)。
    否则返回 (new_job_id, False)。
    """
    # 检查是否已有 running 任务
    # 如果已有 running 任务，则返回 (existing_job_id, True)
    # 否则返回 (new_job_id, False)
    ak = _active_key(user_id, agent_id, session_id)
    existing = await cache.aget_json(ak)
    if isinstance(existing, dict) and existing.get("job_id"):
        ej = str(existing["job_id"])
        meta = await cache.aget_json(_meta_key(ej))
        if meta and meta.get("status") == "running":
            if await is_job_cancel_requested(ej):
                await _release_active_key(user_id, agent_id, session_id, ej)
            else:
                return ej, True

    job_id = uuid.uuid4().hex
    lock_ok = await cache.aset_nx(ak, {"job_id": job_id}, _ttl())
    if not lock_ok:
        raced = await cache.aget_json(ak)
        if isinstance(raced, dict) and raced.get("job_id"):
            return str(raced["job_id"]), True
        raise RuntimeError("无法创建对话任务：会话锁不可用")

    meta = {
        "job_id": job_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "status": "running",
        "error": None,
        "regenerate": bool(regenerate),
        "target_message_id": target_message_id,
    }
    await cache.aset_json(_meta_key(job_id), meta, _ttl())

    # 创建异步任务执行对话；持有强引用防止被 GC 回收（任务生命周期不依赖事件循环弱引用）
    aids = attachment_ids or []
    task = asyncio.create_task(
        _run_chat_job(
            job_id=job_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            message=message,
            use_knowledge_retrieval=use_knowledge_retrieval,
            use_web_search=use_web_search,
            attachment_ids=aids,
            regenerate=regenerate,
            target_message_id=target_message_id,
            mcp_approved_pending_id=mcp_approved_pending_id,
        )
    )
    _JOB_TASKS.add(task)
    task.add_done_callback(_JOB_TASKS.discard)
    return job_id, False


async def _run_chat_job(
    *,
    job_id: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    message: str,
    use_knowledge_retrieval: bool,
    use_web_search: bool = False,
    attachment_ids: list[str] | None = None,
    regenerate: bool = False,
    target_message_id: int | None = None,
    mcp_approved_pending_id: str | None = None,
) -> None:
    from app.controllers.user_agent_recent import touch_recent_agent

    seq = 0
    try:
        # 获取智能体
        ua = await user_agent_controller.get_accessible(agent_id, user_id)
        # 如果智能体不存在或无权限，则返回错误
        if not ua:
            await _append_event(job_id, seq, {"type": "error", "content": "智能体不存在或无权限"})
            seq += 1
            await _finish_meta(job_id, status="failed", error="智能体不存在")
            return

        user_cancelled = False
        # 闸门已满/已有排队时先给前端一条排队事件（含等待人数），避免 running 状态下长时间零反馈
        gate_stats = get_llm_gate_stats()
        if gate_stats["waiting"] > 0 or gate_stats["inflight"] >= gate_stats["limit"]:
            await _append_event(job_id, seq, {"type": "queued", "waiting": gate_stats["waiting"] + 1})
            seq += 1
        try:
            async with llm_slot():
                async for ev in iter_chat_stream_events(
                    ua,
                    message,
                    user_id,
                    agent_id,
                    session_id,
                    use_knowledge_retrieval=use_knowledge_retrieval,
                    use_web_search=use_web_search,
                    attachment_ids=attachment_ids or [],
                    regenerate=regenerate,
                    target_message_id=target_message_id,
                    cancel_check=lambda jid=job_id: is_job_cancel_requested(jid),
                    mcp_approved_pending_id=mcp_approved_pending_id,
                ):
                    await _append_event(job_id, seq, ev)
                    seq += 1
                    if ev.get("type") == "done" and ev.get("cancelled"):
                        user_cancelled = True
        except LLMGateTimeout:
            await _append_event(job_id, seq, {"type": "error", "content": "服务繁忙，排队等待超时，请稍后重试"})
            seq += 1
            await _finish_meta(job_id, status="failed", error="排队等待超时")
            return

        if user_cancelled:
            await _finish_meta(job_id, status="cancelled", error=None)
        else:
            await _finish_meta(job_id, status="completed", error=None)
            # 更新最近使用智能体（编辑器试聊会话不置顶）
            if not is_editor_preview_session(session_id):
                try:
                    await touch_recent_agent(user_id, agent_id)
                except Exception:
                    pass
    except Exception as e:
        await _append_event(job_id, seq, {"type": "error", "content": str(e)})
        await _finish_meta(job_id, status="failed", error=str(e))
    finally:
        # 仅当占用锁仍指向本 job 时释放：取消即时终结后可能已有新任务持有该锁
        await _release_active_key(user_id, agent_id, session_id, job_id)


async def _finish_meta(job_id: str, *, status: str, error: str | None) -> None:
    """
    完成任务：终态后改用较短的 done TTL，避免事件列表在 Redis 中留存 24h。
    """
    meta = await cache.aget_json(_meta_key(job_id))
    if not isinstance(meta, dict):
        meta = {"job_id": job_id}
    meta["status"] = status
    meta["error"] = error
    ttl = _done_ttl()
    await cache.aset_json(_meta_key(job_id), meta, ttl)
    await cache.aexpire(_events_key(job_id), ttl)
    await cache.adelete(_cancel_key(job_id))


async def get_job_meta(job_id: str) -> dict[str, Any] | None:
    """
    获取 Job 元数据（异步）
    """
    raw = await cache.aget_json(_meta_key(job_id))
    return raw if isinstance(raw, dict) else None


async def iter_job_sse_events(
    job_id: str,
    *,
    since_seq: int,
) -> Any:
    """
    异步迭代 SSE 行（不含外层 StreamingResponse），从 Redis 列表下标 since_seq 起追更直至任务结束。
    前端收流：从 Redis 列表下标 since_seq 起追更直至任务结束
    """
    # 从 Redis 列表下标 since_seq 起追更直至任务结束
    next_idx = max(0, since_seq)
    # 空闲退避：有事件时保持 40ms 低延迟，持续空转时逐倍放宽到 0.3s，降低挂起连接的 Redis 底噪
    idle_delay = 0.04
    # 循环直到任务结束
    while True:
        # 批量取事件（下标即 seq，since_seq 续传语义不变）
        chunk = await cache.alrange_str(_events_key(job_id), next_idx, next_idx + 63)
        if chunk:
            idle_delay = 0.04
            for raw in chunk:
                try:
                    wrapped = json.loads(raw)
                    data = wrapped.get("data") or {}
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except Exception:
                    pass
                next_idx += 1
            continue

        meta = await cache.aget_json(_meta_key(job_id))
        if not meta:
            break
        st = meta.get("status")
        if st != "running":
            break
        # 短轮询：在 Job 仍 running 且暂无新事件时等待；间隔过大会导致 SSE 观感像“整段输出”
        await asyncio.sleep(idle_delay)
        idle_delay = min(idle_delay * 2, 0.3)

    yield "data: [DONE]\n\n"


async def verify_job_owner(job_id: str, user_id: int) -> bool:
    """
    验证 Job 是否属于用户（异步）
    """
    # 获取 Job 元数据
    meta = await get_job_meta(job_id)
    # 如果 Job 元数据不存在，则返回 False
    if not meta:
        return False
    # 如果 Job 元数据中的 user_id 与传入的 user_id 不匹配，则返回 False
    # 否则返回 True
    return int(meta.get("user_id", -1)) == int(user_id)