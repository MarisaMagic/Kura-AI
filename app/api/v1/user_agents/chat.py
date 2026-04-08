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
from app.controllers.user_agent_recent import list_recent_agents_public, touch_recent_agent
from app.models.user_agent import UserAgent
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
    """
    展示到分钟，不含秒。
    用于展示会话列表中的更新时间。
    :param iso_ts: 时间戳，格式为 ISO 8601 格式
    :return: 展示到分钟，不含秒的时间戳
    """
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
        await touch_recent_agent(user_id, request.agent_id)
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
    """
    智能体对话（SSE 流式）
    当用户在智能体对话页面发送消息时，会调用此 API。流式返回响应。
    :param request: 请求体
    :param current_user: 当前用户
    :return: StreamingResponse
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 获取用户配置的智能体信息
    ua = await user_agent_controller.get_owned(request.agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 获取会话ID
    session_id = (request.session_id or "default_session").strip() or "default_session"

    # 定义事件生成器
    async def event_generator():
        try:
            # 调用智能体异步对话函数，流式返回响应
            async for chunk in chat_with_agent_stream(ua, request.message.strip(), user_id, request.agent_id, session_id):
                yield chunk
            # 更新最近使用智能体
            await touch_recent_agent(user_id, request.agent_id)
        # 如果发生异常，则返回错误信息
        except Exception as e:
            err = {"type": "error", "content": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    # 返回流式响应
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _enrich_session_rows(ua, items: list[dict]) -> list[SessionInfo]:
    """
    补全会话列表信息。
    通过智能体名称、更新时间等字段补全会话列表信息。用于展示单个智能体会话列表中的元数据。
    :param ua: 智能体
    :param items: 会话列表
    :return: 补全会话列表信息
    """
    agent_name = (ua.name or "").strip()
    enriched = []
    for x in items:
        row = dict(x)
        row.setdefault("last_user_preview", "")
        row.setdefault("agent_id", ua.id)
        row["agent_name"] = agent_name
        row["updated_at_display"] = _session_updated_at_display(row.get("updated_at") or "")
        enriched.append(SessionInfo(**row))
    return enriched


async def _enrich_all_user_sessions(user_id: int, items: list[dict]) -> list[SessionInfo]:
    """
    跨智能体会话列表：按 agent_id 批量补全智能体名称。
    通过智能体名称、更新时间等字段补全会话列表信息。用于展示用户所有会话列表中的元数据。
    :param user_id: 用户ID
    :param items: 会话列表
    :return: 跨智能体会话列表
    """
    agent_ids = list({int(x["agent_id"]) for x in items if x.get("agent_id") is not None})
    names: dict[int, str] = {}
    if agent_ids:
        agents = await UserAgent.filter(user_id=user_id, id__in=agent_ids).all()
        for a in agents:
            names[a.id] = (a.name or "").strip()
    enriched = []
    for x in items:
        row = dict(x)
        row.setdefault("last_user_preview", "")
        aid = row.get("agent_id")
        if aid is not None:
            row["agent_id"] = int(aid)
            row["agent_name"] = names.get(int(aid), "")
        else:
            row["agent_name"] = ""
        row["updated_at_display"] = _session_updated_at_display(row.get("updated_at") or "")
        enriched.append(SessionInfo(**row))
    return enriched


@router.get("/chat/sessions", summary="当前用户在某智能体下的会话列表", tags=["智能体模块"])
async def list_chat_sessions(
    agent_id: int = Query(..., description="智能体 ID"),
    limit: int | None = Query(
        None,
        ge=1,
        le=100,
        description="分页条数；不传则返回全量（兼容旧客户端，走缓存）",
    ),
    offset: int = Query(0, ge=0, description="分页偏移"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    当前用户在某智能体下的会话列表
    当用户在智能体对话页面点击顶部智能体按钮查看弹窗，前端调用此 API 展示会话列表。
    :param agent_id: 智能体 ID
    :param limit: 分页条数
    :param offset: 分页偏移
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 获取用户配置的智能体信息
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")

    # 如果limit为None，则返回全量
    if limit is None:
        # 获取会话列表, 通过 PostgreSQL 和 Redis 缓存获取
        items = storage.list_session_infos(user_id, agent_id)
        # 补全会话列表信息
        enriched = _enrich_session_rows(ua, items)
        # 按更新时间倒序
        enriched.sort(key=lambda x: x.updated_at, reverse=True)
        body = SessionListResponse(
            sessions=enriched,
            total=len(enriched),
            has_more=False,
        )
        return Success(data=body.model_dump())

    # 如果limit不为None，则分页返回会话列表。通过 PostgreSQL 和 Redis 缓存获取
    items, total = storage.list_session_infos_paginated(user_id, agent_id, limit, offset)
    # 补全会话列表信息
    enriched = _enrich_session_rows(ua, items)
    # 是否有更多
    has_more = offset + len(enriched) < total
    # 返回会话列表响应
    body = SessionListResponse(
        sessions=enriched,
        total=total,
        has_more=has_more,
    )
    return Success(data=body.model_dump())


@router.get(
    "/chat/sessions/all",
    summary="当前用户全部智能体下的会话列表（按最近时间，分页）",
    tags=["智能体模块"],
)
async def list_chat_sessions_all(
    limit: int = Query(30, ge=1, le=100, description="分页条数"),
    offset: int = Query(0, ge=0, description="分页偏移"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    当前用户全部智能体下的会话列表（按最近时间，分页）
    用户侧边栏“最近对话”，前端调用此 API 展示最近会话列表。
    :param limit: 分页条数
    :param offset: 分页偏移
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 获取会话列表, 通过 PostgreSQL 和 Redis 缓存获取
    items, total = storage.list_session_infos_all_paginated(user_id, limit, offset)
    # 补全当前用户所有会话列表信息
    enriched = await _enrich_all_user_sessions(user_id, items)
    # 是否有更多
    has_more = offset + len(enriched) < total
    # 返回会话列表响应
    body = SessionListResponse(
        sessions=enriched,
        total=total,
        has_more=has_more,
    )
    return Success(data=body.model_dump())


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
    """
    获取某会话的全部消息
    用户打开会话页面，前端调用此 API 展示历史会话消息，展示用户和智能体历史对话内容。
    :param session_id: 会话ID
    :param agent_id: 智能体 ID
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 获取用户配置的智能体信息
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 获取会话消息，通过 PostgreSQL 和 Redis 缓存获取
    raw = storage.get_session_messages(user_id, agent_id, session_id)
    # 转换为消息信息
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
    """
    删除会话
    用户在会话页面点击删除会话按钮，前端调用此 API 删除会话。
    :param session_id: 会话ID
    :param agent_id: 智能体 ID
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 获取用户配置的智能体信息
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 删除会话，通过 PostgreSQL 和 Redis 缓存删除
    deleted = storage.delete_session(user_id, agent_id, session_id)
    # 如果删除失败，则返回404错误
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 返回会话删除响应
    body = SessionDeleteResponse(session_id=session_id, message="已删除会话").model_dump()
    return Success(data=body)


@router.get("/recent_agents", summary="最近使用的智能体（最多3个）", tags=["智能体模块"])
async def get_recent_agents(current_user: User = Depends(AuthControl.is_authed)):
    """
    最近使用的智能体（最多3个）
    用户侧边栏“最近对话”，前端调用此 API 展示最近使用智能体列表。
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 获取最近使用智能体列表
    agents = await list_recent_agents_public(user_id)
    return Success(data={"agents": agents})


@router.post("/recent_agents/touch", summary="记录使用某智能体（更新最近使用并裁剪为最多3条）", tags=["智能体模块"])
async def post_recent_agents_touch(
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    记录使用某智能体（更新最近使用并裁剪为最多3条）
    用户在智能体对话页面点击顶部智能体按钮查看弹窗，前端调用此 API 记录使用某智能体，并裁剪为最多3条。
    :param agent_id: 智能体 ID
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    # 更新最近使用智能体
    ok = await touch_recent_agent(user_id, agent_id)
    # 如果更新失败，则返回404错误
    if not ok:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 获取最近使用智能体列表
    agents = await list_recent_agents_public(user_id)
    # 返回最近使用智能体列表响应
    return Success(data={"agents": agents})
