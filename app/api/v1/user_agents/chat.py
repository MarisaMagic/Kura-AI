"""智能体对话：需登录；不校验菜单 API 权限（DependAuth）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.chat.agent_service import chat_with_agent_stream, chat_with_agent_sync
from app.chat.attachment_service import file_bytes_for_attachment, get_attachment_row, save_uploaded_file
from app.chat.chat_job import (
    cancel_active_session_job,
    create_chat_job,
    get_job_meta,
    iter_job_sse_events,
    request_chat_job_cancel,
    verify_job_owner,
)
from app.chat.preview_session import is_editor_preview_session
from app.chat.storage import storage
from app.controllers.user_agent import user_agent_controller
from app.controllers.user_agent_recent import list_recent_agents_public, touch_recent_agent
from app.models.user_agent import UserAgent
from app.core.dependency import AuthControl
from app.models import User
from app.mcp_client.tool_policy import approve_mcp_confirmation
from app.schemas.agent_chat import (
    ChatAttachmentUploadResponse,
    ChatJobCreateResponse,
    ChatRequest,
    ChatResponse,
    MessageInfo,
    McpConfirmRequest,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)
from app.schemas.base import Success
from app.settings import settings
from app.utils.rate_limit import check_user_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_chat_rate_limit(user_id: int) -> None:
    """对话生成入口统一限流（按用户计，覆盖 /chat、/chat/stream、/chat/jobs）。"""
    check_user_rate_limit(
        user_id,
        action="agent_chat",
        limit=int(getattr(settings, "CHAT_RATE_LIMIT_PER_MINUTE", 20)),
        window_seconds=60,
    )


@router.post("/chat/attachments/upload", summary="上传会话附件（先上传再发消息）", tags=["智能体模块"])
async def upload_chat_attachment(
    agent_id: int = Query(..., description="智能体 ID"),
    session_id: str = Query("default_session", description="会话 ID，与对话一致"),
    file: UploadFile = File(...),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    将文件保存到服务端并在本会话下登记，返回 attachment_id，供随后 Chat 请求的 attachment_ids 引用。
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID，与对话一致
    :param file: 上传文件
    :param current_user: 当前用户
    :return: Success
    """
    user_id = current_user.id
    ua = await user_agent_controller.get_accessible(agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    sid = (session_id or "default_session").strip() or "default_session"
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    try:
        # 保存上传文件, 返回附件ID、文件名、文件类型、文件大小
        data = save_uploaded_file( 
            user_id=user_id,
            agent_id=agent_id,
            session_id=sid,
            original_filename=file.filename or "file",
            raw=raw,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Success(data=ChatAttachmentUploadResponse(**data).model_dump())  # 返回附件上传结果


@router.get("/chat/attachments/preview", summary="预览会话附件（图片等，需登录）", tags=["智能体模块"])
async def preview_chat_attachment(
    agent_id: int = Query(..., description="智能体 ID"),
    session_id: str = Query(..., description="会话 ID"),
    attachment_id: str = Query(..., description="附件 ID（mg_chat_attachments.id）"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    按 attachment_id 读盘并返回字节流；供前端因 token 请求头无法用 img src 直连时通过 fetch+blob 展示。
    """
    user_id = current_user.id
    ua = await user_agent_controller.get_accessible(agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    sid = (session_id or "default_session").strip() or "default_session"
    aid = (attachment_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="attachment_id 不能为空")
    row = get_attachment_row(aid, user_id=user_id, agent_id=agent_id, session_id=sid)
    if not row:
        raise HTTPException(status_code=404, detail="附件不存在")
    raw = file_bytes_for_attachment(aid, user_id=user_id, agent_id=agent_id, session_id=sid)
    if not raw:
        raise HTTPException(status_code=404, detail="附件文件缺失或不可读")
    mime = (row.mime or "").strip() or "application/octet-stream"
    return Response(
        content=raw,
        media_type=mime,
        headers={
            "Cache-Control": "private, max-age=300",
        },
    )


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
    match = re.search(r"Error code:\s*([0-9][0-9][0-9])", message)
    if not match:
        return None
    code = int(match.group(1))
    logger.warning("upstream model service error code=%s: %s", code, message[:1000])
    if code == 400 + 29:
        return HTTPException(status_code=400 + 29, detail="Upstream model service rate limit or quota exceeded")
    if code in (401, 403):
        return HTTPException(status_code=code, detail="Upstream model service authentication or permission failed")
    return HTTPException(status_code=502, detail="Upstream model service unavailable")

@router.post("/chat", summary="智能体对话（非流式）", tags=["智能体模块"])
async def chat_sync_endpoint(request: ChatRequest, current_user: User = Depends(AuthControl.is_authed)):
    user_id = current_user.id
    _check_chat_rate_limit(user_id)
    ua = await user_agent_controller.get_accessible(request.agent_id, user_id)
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    session_id = (request.session_id or "default_session").strip() or "default_session"
    try:
        # 放到线程池执行：chat_with_agent_sync 内部会用 asyncio.run 加载 MCP 工具，
        # 需运行在无活动事件循环的线程中；同时避免同步 LLM 调用阻塞事件循环。
        resp = await asyncio.to_thread(
            chat_with_agent_sync,
            ua,
            request.message.strip(),
            user_id,
            request.agent_id,
            session_id,
            use_knowledge_retrieval=request.use_knowledge_retrieval,
            use_web_search=request.use_web_search,
            attachment_ids=request.attachment_ids or None,
            mcp_approved_pending_id=request.mcp_approved_pending_id,
        )
        if not is_editor_preview_session(session_id):
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
    _check_chat_rate_limit(user_id)
    # 获取用户配置的智能体信息
    ua = await user_agent_controller.get_accessible(request.agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 获取会话ID
    session_id = (request.session_id or "default_session").strip() or "default_session"

    # 定义事件生成器
    async def event_generator():
        try:
            # 调用智能体异步对话函数，流式返回响应
            async for chunk in chat_with_agent_stream(
                ua,
                request.message.strip(),
                user_id,
                request.agent_id,
                session_id,
                use_knowledge_retrieval=request.use_knowledge_retrieval,
                use_web_search=request.use_web_search,
                attachment_ids=request.attachment_ids or None,
                regenerate=request.regenerate,
                mcp_approved_pending_id=request.mcp_approved_pending_id,
            ):
                yield chunk
            # 更新最近使用智能体（编辑器试聊会话不置顶）
            if not is_editor_preview_session(session_id):
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


@router.post("/chat/jobs", summary="创建异步对话 Job（可刷新后重连 SSE）", tags=["智能体模块"])
async def create_chat_job_endpoint(request: ChatRequest, current_user: User = Depends(AuthControl.is_authed)):
    """
    创建异步对话 Job（可刷新后重连 SSE）
    :param request: 请求体
    :param current_user: 当前用户
    :return: Success
    """
    # 获取当前用户ID
    user_id = current_user.id
    _check_chat_rate_limit(user_id)
    # 获取用户配置的智能体信息
    ua = await user_agent_controller.get_accessible(request.agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 获取会话ID
    session_id = (request.session_id or "default_session").strip() or "default_session"
    # 创建异步对话 Job
    job_id, is_dup = await create_chat_job(
        user_id=user_id,
        agent_id=request.agent_id,
        session_id=session_id,
        message=request.message.strip(),
        use_knowledge_retrieval=request.use_knowledge_retrieval,
        use_web_search=request.use_web_search,
        attachment_ids=request.attachment_ids or None,
        regenerate=request.regenerate,
        mcp_approved_pending_id=request.mcp_approved_pending_id,
    )
    if is_dup:
        # 如果已有进行中的生成任务，则返回409错误
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该会话已有进行中的生成任务，请使用 existing_job_id 重连 SSE。",
                "existing_job_id": job_id,
            },
        )
    # 返回创建异步对话 Job 响应
    return Success(data=ChatJobCreateResponse(job_id=job_id).model_dump())


@router.get("/chat/jobs/{job_id}", summary="查询对话 Job 状态", tags=["智能体模块"])
async def get_chat_job_endpoint(job_id: str, current_user: User = Depends(AuthControl.is_authed)):
    """
    查询对话 Job 状态
    :param job_id: Job ID
    :param current_user: 当前用户
    :return: Success
    """
    # 获取 Job 元数据
    meta = get_job_meta(job_id)
    if not meta or int(meta.get("user_id", -1)) != int(current_user.id):
        raise HTTPException(status_code=404, detail="任务不存在或无权限")
    return Success(data=meta)


@router.post("/chat/jobs/{job_id}/cancel", summary="停止正在进行的对话生成", tags=["智能体模块"])
async def cancel_chat_job_endpoint(job_id: str, current_user: User = Depends(AuthControl.is_authed)):
    """
    用户主动停止生成：协作中断模型输出，不写入本轮助手消息（或仅保留已流式展示由前端决定）。
    :param job_id: Job ID
    :param current_user: 当前用户
    :return: Success
    """
    if not verify_job_owner(job_id, current_user.id):
        raise HTTPException(status_code=404, detail="任务不存在或无权限")
    meta = get_job_meta(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="任务不存在或无权限")
    if meta.get("status") != "running":
        return Success(data={"ok": True, "already_finished": True})
    await request_chat_job_cancel(job_id)
    return Success(data={"ok": True})


@router.post("/chat/active_job/cancel", summary="停止当前会话正在进行的对话生成（按会话）", tags=["智能体模块"])
async def cancel_active_chat_job_endpoint(
    agent_id: int = Query(..., description="智能体 ID"),
    session_id: str = Query(..., description="会话 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    按会话取消当前活动任务：用于前端停止生成时 job_id 未知的兜底场景（如创建请求在途被中断）。
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param current_user: 当前用户
    :return: Success
    """
    user_id = current_user.id
    sid = (session_id or "default_session").strip() or "default_session"
    cancelled = await cancel_active_session_job(user_id, agent_id, sid)
    return Success(data={"ok": True, "cancelled": cancelled})


@router.post("/chat/mcp/confirm", summary="确认或拒绝高危 MCP 工具调用", tags=["智能体模块"])
async def confirm_mcp_tool(request: McpConfirmRequest, current_user: User = Depends(AuthControl.is_authed)):
    ok = approve_mcp_confirmation(request.pending_id, current_user.id, request.approve)
    if not ok:
        raise HTTPException(status_code=404, detail="确认任务不存在或已过期")
    return Success(data={"ok": True, "approved": bool(request.approve)})


@router.get(
    "/chat/jobs/{job_id}/stream",
    summary="订阅对话 Job 的" + "SSE（支持 since_seq 断点续传）",
    tags=["智能体模块"],
)
async def chat_job_stream_endpoint(
    job_id: str,
    since_seq: int = Query(0, ge=0, description="从 Redis 事件列表的下标开始接收（重连时传入已收到条数）"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    订阅对话 Job 的 SSE（支持 since_seq 断点续传）
    :param job_id: Job ID
    :param since_seq: 从 Redis 事件列表的下标开始接收（重连时传入已收到条数）
    :param current_user: 当前用户
    :return: StreamingResponse
    """
    # 验证 Job 是否属于用户
    if not verify_job_owner(job_id, current_user.id):
        raise HTTPException(status_code=404, detail="任务不存在或无权限")

    # 定义事件生成器
    async def event_generator():
        # 异步迭代 SSE 行
        async for line in iter_job_sse_events(job_id, since_seq=since_seq):
            yield line

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


async def _enrich_all_user_sessions(items: list[dict]) -> list[SessionInfo]:
    """
    跨智能体会话列表：按 agent_id 批量补全智能体名称。
    通过智能体名称、更新时间等字段补全会话列表信息。用于展示用户所有会话列表中的元数据。
    会话均属于当前用户，按 id 直接回填即可（覆盖共享的已发布智能体与已下架智能体的历史会话）。
    :param items: 会话列表
    :return: 跨智能体会话列表
    """
    agent_ids = list({int(x["agent_id"]) for x in items if x.get("agent_id") is not None})
    names: dict[int, str] = {}
    if agent_ids:
        agents = await UserAgent.filter(id__in=agent_ids).all()
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
    ua = await user_agent_controller.get_accessible(agent_id, user_id)
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
    enriched = await _enrich_all_user_sessions(items)
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
    ua = await user_agent_controller.get_accessible(agent_id, user_id)
    # 如果智能体不存在或无权限访问，则返回404错误
    if not ua:
        raise HTTPException(status_code=404, detail="智能体不存在或无权限访问")
    # 获取会话消息，通过 PostgreSQL 和 Redis 缓存获取
    raw = storage.get_session_messages(user_id, agent_id, session_id)
    from app.utils.signed_media import resign_message_payload

    messages = [
        MessageInfo(
            type=m["type"],
            content=m.get("content", ""),
            content_json=m.get("content_json"),
            timestamp=m["timestamp"],
            rag_trace=m.get("rag_trace"),
            rag_steps=m.get("rag_steps"),
            error_text=m.get("error_text"),
            sources=m.get("sources"),
            thinking_text=m.get("thinking_text"),
        )
        for m in (resign_message_payload(x) for x in raw)
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
    user_id = current_user.id
    # 只校验会话属于当前用户（智能体已删除时仍允许清掉侧栏残留）
    deleted = storage.delete_session(user_id, agent_id, session_id)
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
