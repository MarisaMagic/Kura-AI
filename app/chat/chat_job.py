"""
智能体流式对话异步 Job：后台执行生成，事件写入 Redis，支持断线后按 seq 重连 SSE。
用于将对话任务放在后台执行，不会被用户的其它请求打断。
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


def is_job_cancel_requested(job_id: str) -> bool:
    """是否已请求取消该 Job（同步读缓存，供 iter 内 to_thread 调用）。"""
    raw = cache.get_json(_cancel_key(job_id))
    return bool(raw)


def get_running_session_job(user_id: int, agent_id: int, session_id: str) -> dict | None:
    """返回该会话当前 running 的 Job 元数据；无则 None（用于切分支前的冲突检查）。"""
    existing = cache.get_json(_active_key(user_id, agent_id, session_id))
    if not isinstance(existing, dict) or not existing.get("job_id"):
        return None
    meta = get_job_meta(str(existing["job_id"]))
    if meta and meta.get("status") == "running":
        return meta
    return None


def _release_active_key(user_id: int, agent_id: int, session_id: str, job_id: str) -> None:
    """
    释放会话占用锁：仅当锁仍指向该 job 时才删除，
    避免旧任务退出时误删新任务的占用锁。
    """
    cache.delete_if_job_matches(_active_key(user_id, agent_id, session_id), job_id)

async def request_chat_job_cancel(job_id: str) -> None:
    """
    标记 Job 为「用户请求停止」并即时终结：
    1. 写取消标记，供生成协程协作中断；
    2. 若任务仍 running，立即置为 cancelled 并释放会话占用锁，
       使用户停止后可立刻发起新任务，不必等待旧任务完全退出。
    """
    await asyncio.to_thread(cache.set_json, _cancel_key(job_id), {"v": 1}, _ttl())
    meta = await asyncio.to_thread(cache.get_json, _meta_key(job_id))
    if not isinstance(meta, dict) or meta.get("status") != "running":
        return
    meta["status"] = "cancelled"
    meta["error"] = None
    await asyncio.to_thread(cache.set_json, _meta_key(job_id), meta, _ttl())
    await asyncio.to_thread(
        _release_active_key,
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
    existing = await asyncio.to_thread(cache.get_json, _active_key(user_id, agent_id, session_id))
    if not isinstance(existing, dict) or not existing.get("job_id"):
        return False
    job_id = str(existing["job_id"])
    meta = await asyncio.to_thread(cache.get_json, _meta_key(job_id))
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


async def _touch_meta_ttl(job_id: str) -> None:
    """
    更新 Job 元数据和事件的过期时间
    """
    t = _ttl()
    await asyncio.to_thread(cache.expire, _meta_key(job_id), t)
    await asyncio.to_thread(cache.expire, _events_key(job_id), t)


async def _append_event(job_id: str, seq: int, data: dict[str, Any]) -> None:
    """
    追加 Job 事件
    """
    wrapped = {"seq": seq, "data": data}
    await asyncio.to_thread(cache.rpush_json, _events_key(job_id), wrapped)
    await _touch_meta_ttl(job_id)


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
    existing = await asyncio.to_thread(cache.get_json, ak)
    if isinstance(existing, dict) and existing.get("job_id"):
        ej = str(existing["job_id"])
        meta = await asyncio.to_thread(cache.get_json, _meta_key(ej))
        if meta and meta.get("status") == "running":
            if await asyncio.to_thread(is_job_cancel_requested, ej):
                # 僵尸任务：取消标记已存在但占用锁尚未释放，清理后继续创建新任务
                await asyncio.to_thread(_release_active_key, user_id, agent_id, session_id, ej)
            else:
                return ej, True

    # 创建新的 Job
    job_id = uuid.uuid4().hex
    # 创建 Job 元数据
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
    # 存储 Job 元数据，放入事件循环绑定的线程池里执行
    await asyncio.to_thread(cache.set_json, _meta_key(job_id), meta, _ttl())
    # 存储 Job 活动 key，放入事件循环绑定的线程池里执行
    await asyncio.to_thread(cache.set_json, ak, {"job_id": job_id}, _ttl())

    # 创建异步任务执行对话
    aids = attachment_ids or []
    asyncio.create_task(
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
        # 异步迭代流式事件
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
            # 追加事件
            await _append_event(job_id, seq, ev)
            seq += 1
            if ev.get("type") == "done" and ev.get("cancelled"):
                user_cancelled = True

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
        await asyncio.to_thread(_release_active_key, user_id, agent_id, session_id, job_id)


async def _finish_meta(job_id: str, *, status: str, error: str | None) -> None:
    """
    完成任务
    """
    meta = await asyncio.to_thread(cache.get_json, _meta_key(job_id))
    if not isinstance(meta, dict):
        meta = {"job_id": job_id}
    meta["status"] = status
    meta["error"] = error
    await asyncio.to_thread(cache.set_json, _meta_key(job_id), meta, _ttl())
    await asyncio.to_thread(cache.delete, _cancel_key(job_id))


def get_job_meta(job_id: str) -> dict[str, Any] | None:
    """
    获取 Job 元数据
    """
    raw = cache.get_json(_meta_key(job_id))
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
    # 循环直到任务结束
    while True:
        # 从 Redis 列表下标 next_idx 起获取一条数据
        chunk = await asyncio.to_thread(cache.lrange_str, _events_key(job_id), next_idx, next_idx)
        if chunk:
            try:
                wrapped = json.loads(chunk[0])
                data = wrapped.get("data") or {}
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            next_idx += 1
            continue

        meta = await asyncio.to_thread(cache.get_json, _meta_key(job_id))
        if not meta:
            break
        st = meta.get("status")
        if st != "running":
            break
        # 短轮询：在 Job 仍 running 且暂无新事件时等待；间隔过大会导致 SSE 观感像“整段输出”
        await asyncio.sleep(0.04)

    yield "data: [DONE]\n\n"


def verify_job_owner(job_id: str, user_id: int) -> bool:
    """
    验证 Job 是否属于用户
    """
    # 获取 Job 元数据
    meta = get_job_meta(job_id)
    # 如果 Job 元数据不存在，则返回 False
    if not meta:
        return False
    # 如果 Job 元数据中的 user_id 与传入的 user_id 不匹配，则返回 False
    # 否则返回 True
    return int(meta.get("user_id", -1)) == int(user_id)
