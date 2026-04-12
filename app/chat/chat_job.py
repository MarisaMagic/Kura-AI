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
from app.controllers.user_agent import user_agent_controller
from app.settings import settings


def _meta_key(job_id: str) -> str:
    return f"chat_job:{job_id}:meta"


def _events_key(job_id: str) -> str:
    return f"chat_job:{job_id}:events"


def _active_key(user_id: int, agent_id: int, session_id: str) -> str:
    return f"chat_job_active:{user_id}:{agent_id}:{session_id}"


def _ttl() -> int:
    return int(getattr(settings, "CHAT_JOB_TTL_SECONDS", 86400))


async def _touch_meta_ttl(job_id: str) -> None:
    t = _ttl()
    await asyncio.to_thread(cache.expire, _meta_key(job_id), t)
    await asyncio.to_thread(cache.expire, _events_key(job_id), t)


async def _append_event(job_id: str, seq: int, data: dict[str, Any]) -> None:
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
) -> tuple[str, bool]:
    """
    创建 Job：若同会话已有 running 任务则返回 (existing_job_id, True)。
    否则返回 (new_job_id, False)。
    """
    ak = _active_key(user_id, agent_id, session_id)
    existing = await asyncio.to_thread(cache.get_json, ak)
    if isinstance(existing, dict) and existing.get("job_id"):
        ej = str(existing["job_id"])
        meta = await asyncio.to_thread(cache.get_json, _meta_key(ej))
        if meta and meta.get("status") == "running":
            return ej, True

    job_id = uuid.uuid4().hex
    meta = {
        "job_id": job_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "status": "running",
        "error": None,
    }
    await asyncio.to_thread(cache.set_json, _meta_key(job_id), meta, _ttl())
    await asyncio.to_thread(cache.set_json, ak, {"job_id": job_id}, _ttl())

    asyncio.create_task(
        _run_chat_job(
            job_id=job_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            message=message,
            use_knowledge_retrieval=use_knowledge_retrieval,
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
) -> None:
    from app.controllers.user_agent_recent import touch_recent_agent

    ak = _active_key(user_id, agent_id, session_id)
    seq = 0
    try:
        ua = await user_agent_controller.get_owned(agent_id, user_id)
        if not ua:
            await _append_event(job_id, seq, {"type": "error", "content": "智能体不存在或无权限"})
            seq += 1
            await _finish_meta(job_id, status="failed", error="智能体不存在")
            return

        async for ev in iter_chat_stream_events(
            ua,
            message,
            user_id,
            agent_id,
            session_id,
            use_knowledge_retrieval=use_knowledge_retrieval,
        ):
            await _append_event(job_id, seq, ev)
            seq += 1

        await _finish_meta(job_id, status="completed", error=None)
        try:
            await touch_recent_agent(user_id, agent_id)
        except Exception:
            pass
    except Exception as e:
        await _append_event(job_id, seq, {"type": "error", "content": str(e)})
        await _finish_meta(job_id, status="failed", error=str(e))
    finally:
        await asyncio.to_thread(cache.delete, ak)


async def _finish_meta(job_id: str, *, status: str, error: str | None) -> None:
    meta = await asyncio.to_thread(cache.get_json, _meta_key(job_id))
    if not isinstance(meta, dict):
        meta = {"job_id": job_id}
    meta["status"] = status
    meta["error"] = error
    await asyncio.to_thread(cache.set_json, _meta_key(job_id), meta, _ttl())


def get_job_meta(job_id: str) -> dict[str, Any] | None:
    raw = cache.get_json(_meta_key(job_id))
    return raw if isinstance(raw, dict) else None


async def iter_job_sse_events(
    job_id: str,
    *,
    since_seq: int,
) -> Any:
    """
    异步迭代 SSE 行（不含外层 StreamingResponse），从 Redis 列表下标 since_seq 起追更直至任务结束。
    """
    next_idx = max(0, since_seq)
    while True:
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
    meta = get_job_meta(job_id)
    if not meta:
        return False
    return int(meta.get("user_id", -1)) == int(user_id)
