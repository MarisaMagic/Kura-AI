"""智能体知识库：上传、列表、删除。"""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.controllers.user_agent import user_agent_controller
from app.core.dependency import AuthControl
from app.kb import kb_service
from app.kb.kb_scope import kb_scope_for
from app.models import User
from app.schemas.base import Fail, Success
from app.schemas.kb import KbDeleteResponse, KbDocumentListResponse, KbDocumentItem, KbUploadResponse

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
    rows = kb_service.fetch_kb_document_list(scope)
    return Success(
        data=KbDocumentListResponse(documents=[KbDocumentItem(**r) for r in rows]).model_dump()
    )


@router.post("/kb/upload", summary="上传知识库文档", tags=["智能体模块"])
async def kb_upload(
    agent_id: int = Query(..., description="智能体 ID"),
    file: UploadFile = File(...),
    current_user: User = Depends(AuthControl.is_authed),
):
    user_id = current_user.id
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    raw_name = file.filename or ""
    display = kb_service.normalize_display_filename(raw_name)
    if not kb_service.allowed_upload_extension(display):
        return Fail(code=400, msg="仅支持 PDF、Word、Excel 文档")
    scope = kb_scope_for(user_id, agent_id)
    try:
        result = await kb_service.ingest_upload(scope, user_id, agent_id, display, file)
    except ValueError as e:
        return Fail(code=400, msg=str(e))
    except Exception as e:
        return Fail(code=500, msg=f"上传失败: {e}")
    return Success(
        data=KbUploadResponse(
            display_filename=result["display_filename"],
            chunk_count=result["chunk_count"],
            parent_chunks=result["parent_chunks"],
            message="上传并入库成功",
        ).model_dump(),
        msg="上传成功",
    )


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
        kb_service.delete_kb_document(scope, user_id, agent_id, display)
    except Exception as e:
        return Fail(code=500, msg=str(e))
    return Success(data=KbDeleteResponse(display_filename=display).model_dump(), msg="删除成功")
