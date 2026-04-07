"""智能体对话：需登录；不校验菜单 API 权限（DependAuth）。"""

from __future__ import annotations

import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.chat.agent_service import chat_with_agent_stream, chat_with_agent_sync
from app.chat.storage import storage
from app.controllers.user_agent import user_agent_controller
from app.core.dependency import AuthControl
from app.models import User
from app.schemas.agent_chat import (
    ChatRequest,
    ChatResponse,
    MessageInfo,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)
from app.schemas.base import Success

router = APIRouter()


def _session_updated_at_display(iso_ts: str) -> str:
    """展示到分钟，不含秒。"""
    if not iso_ts:
        return ""
    try:
        s = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_ts[:16] if len(iso_ts) >= 16 else iso_ts


def _upstream_http_exception(exc: Exception) -> HTTPException | None:
    message = str(exc)
    match = re.search(r"Error code:\s*(\d{3})", message)
    if not match:
        return None
    code = int(match.group(1))
    if code == 429:
        return HTTPException(
            status_code=429,
            detail=(
                "上游模型服务触发限流或额度限制（429）。请检查账号额度与模型状态。\n"
                f"原始错误：{message}"
            ),
        )
    if code in (401, 403):
        return HTTPException(status_code=code, detail=message)
    return HTTPException(status_code=502, detail=message)


@router.post("/chat", summary="智能体对话（非流式）", tags=["智能体模块"])
async def chat_sync_endpoint(request: ChatRequest, current_user: User = Depends(AuthControl.is_authed)):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(request.agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    session_id = (request.session_id or "default_session").strip() or "default_session"
    try:
        resp = chat_with_agent_sync(ua, request.message.strip(), user_id, request.agent_id, session_id)
        return Success(data=ChatResponse(**resp).model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        mapped = _upstream_http_exception(e)
        if mapped:
            raise mapped from e
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/chat/stream", summary="智能体对话（SSE 流式）", tags=["智能体模块"])
async def chat_stream_endpoint(request: ChatRequest, current_user: User = Depends(AuthControl.is_authed)):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(request.agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    session_id = (request.session_id or "default_session").strip() or "default_session"

    async def event_generator():
        try:
            async for chunk in chat_with_agent_stream(ua, request.message.strip(), user_id, request.agent_id, session_id):
                yield chunk
        except Exception as e:
            err = {"type": "error", "content": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/chat/sessions", summary="当前用户在某智能体下的会话列表", tags=["智能体模块"])
async def list_chat_sessions(
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    items = storage.list_session_infos(user_id, agent_id)
    agent_name = (ua.name or "").strip()
    enriched = []
    for x in items:
        row = dict(x)
        row.setdefault("last_user_preview", "")
        row["agent_name"] = agent_name
        row["updated_at_display"] = _session_updated_at_display(row.get("updated_at") or "")
        enriched.append(SessionInfo(**row))
    sessions = enriched
    sessions.sort(key=lambda x: x.updated_at, reverse=True)
    return Success(data=SessionListResponse(sessions=sessions).model_dump())


@router.get(
    "/chat/sessions/{session_id}",
    summary="获取某会话的全部消息",
    tags=["智能体模块"],
)
async def get_chat_session_messages(
    session_id: str,
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    raw = storage.get_session_messages(user_id, agent_id, session_id)
    messages = [
        MessageInfo(
            type=m["type"],
            content=m["content"],
            timestamp=m["timestamp"],
            rag_trace=m.get("rag_trace"),
        )
        for m in raw
    ]
    return Success(data=SessionMessagesResponse(messages=messages).model_dump())


@router.delete(
    "/chat/sessions/{session_id}",
    summary="删除会话",
    tags=["智能体模块"],
)
async def delete_chat_session(
    session_id: str,
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    deleted = storage.delete_session(user_id, agent_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    body = SessionDeleteResponse(session_id=session_id, message="已删除会话").model_dump()
    return Success(data=body)
