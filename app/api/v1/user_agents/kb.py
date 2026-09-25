"""智能体知识库：上传、列表、删除、上传任务状态。"""

from __future__ import annotations

import asyncio
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.controllers.user_agent import user_agent_controller
from app.core.dependency import AuthControl
from app.kb import kb_job, kb_service
from app.kb.kb_scope import kb_scope_for
from app.models import User
from app.schemas.base import Fail, Success
from app.schemas.kb import KbDeleteResponse, KbDocumentListResponse, KbDocumentItem, KbUploadTaskResponse
from app.settings import settings
from app.utils.document_types import SUPPORTED_UPLOAD_HINT, reject_reason
from app.utils.upload_accept import prepare_document_upload

router = APIRouter()


@router.get("/kb/documents", summary="知识库已上传文件列表", tags=["智能体模块"])
async def kb_list_documents(
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    scope = kb_scope_for(user_id, agent_id)
    # SQLAlchemy/对象存储为同步阻塞调用，放线程执行避免卡住事件循环
    rows = await asyncio.to_thread(kb_service.fetch_kb_document_list, scope)
    return Success(
        data=KbDocumentListResponse(documents=[KbDocumentItem(**r) for r in rows]).model_dump()
    )


@router.post("/kb/upload", summary="上传知识库文档", tags=["智能体模块"])
async def kb_upload(
    agent_id: int = Query(..., description="智能体 ID"),
    file: UploadFile = File(...),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    上传知识库文档：只校验并受理，立即返回 task_id；解析/向量化由队列 worker（或内联线程池）执行。
    进度轮询：GET /kb/upload/status/batch?task_ids=...；取消：POST /kb/upload/cancel?task_id=...
    """
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    raw_name = file.filename or ""
    display = kb_service.normalize_display_filename(raw_name)
    legacy = reject_reason(display)
    if legacy:
        return Fail(code=400, msg=legacy)
    if not kb_service.allowed_upload_extension(display):
        return Fail(code=400, msg=SUPPORTED_UPLOAD_HINT)
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return Fail(code=400, msg="未配置 EMBEDDING_API_KEY，无法生成向量")

    scope = kb_scope_for(user_id, agent_id)
    task_id, error = await prepare_document_upload(
        file=file,
        display_filename=display,
        user_id=user_id,
        agent_id=agent_id,
        kb_scope=scope,
    )
    if error:
        code, msg = error
        return Fail(code=code, msg=msg)
    return Success(
        data=KbUploadTaskResponse(task_id=task_id).model_dump(),
        msg="上传已受理，正在后台处理",
    )


@router.get("/kb/upload/status", summary="查询知识库上传任务进度", tags=["智能体模块"])
async def kb_upload_status(
    task_id: str = Query(..., description="上传任务 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    meta = await asyncio.to_thread(kb_job.get_kb_upload_job_meta, task_id)
    if not meta or int(meta.get("user_id") or -1) != int(current_user.id):
        return Fail(code=404, msg="上传任务不存在或已过期")
    return Success(data=meta)


@router.get("/kb/upload/status/batch", summary="批量查询知识库上传任务进度", tags=["智能体模块"])
async def kb_upload_status_batch(
    task_ids: str = Query(..., description="上传任务 ID，逗号分隔（<=200 个）"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """一次返回多个任务快照（MGET），替代逐任务轮询；只返回属于当前用户的任务。"""
    ids = [t.strip() for t in str(task_ids).split(",") if t.strip()][:200]
    if not ids:
        return Fail(code=400, msg="task_ids 不能为空")
    uid = int(current_user.id)

    def _read_owned() -> dict:
        metas = kb_job.get_kb_upload_job_meta_many(ids)
        return {tid: meta for tid, meta in metas.items() if int(meta.get("user_id") or -1) == uid}

    return Success(data={"items": await asyncio.to_thread(_read_owned)})


@router.post("/kb/upload/cancel", summary="取消知识库上传任务", tags=["智能体模块"])
async def kb_upload_cancel(
    task_id: str = Query(..., description="上传任务 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    meta = await asyncio.to_thread(kb_job.get_kb_upload_job_meta, task_id)
    if not meta or int(meta.get("user_id") or -1) != int(current_user.id):
        return Fail(code=404, msg="上传任务不存在或已过期")
    if meta.get("status") not in ("queued", "running"):
        return Fail(code=400, msg="任务已结束，无法取消")
    await kb_job.request_kb_upload_cancel(task_id)
    return Success(data={"task_id": task_id}, msg="已请求取消处理")


@router.delete("/kb/document", summary="删除知识库中的单个文件", tags=["智能体模块"])
async def kb_delete_document(
    agent_id: int = Query(..., description="智能体 ID"),
    filename: str = Query(..., description="展示文件名（原始上传名）"),
    current_user: User = Depends(AuthControl.is_authed),
):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    display = kb_service.normalize_display_filename(unquote(filename))
    scope = kb_scope_for(user_id, agent_id)
    try:
        # Milvus/对象存储/PG 均为同步阻塞调用，放线程执行避免卡住事件循环
        await asyncio.to_thread(kb_service.delete_kb_document, scope, user_id, agent_id, display)
    except Exception as e:  # noqa: BLE001
        return Fail(code=500, msg=str(e))
    return Success(data=KbDeleteResponse(display_filename=display).model_dump(), msg="删除成功")