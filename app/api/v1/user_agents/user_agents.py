from fastapi import APIRouter, File, Query, UploadFile
from tortoise.expressions import Q

from app.controllers.user import user_controller
from app.controllers.user_agent import user_agent_controller
from app.core.ctx import CTX_USER_ID
from app.models.user_agent import UserAgent
from app.schemas.base import Fail, Success, SuccessExtra
from app.schemas.user_agent import UserAgentCreate, UserAgentUpdate
from app.utils.api_key_crypto import encrypt_api_key
from app.kb.kb_scope import kb_scope_for
from app.kb.kb_service import purge_kb_for_scope
from app.utils.user_agent_avatar import (
    agent_avatar_url,
    remove_agent_avatar_file,
    save_uploaded_agent_avatar,
)

router = APIRouter()


async def _public_agent_dict(obj: UserAgent, username: str) -> dict:
    """不包含密文，附带 has_api_key 与属主用户名（头像按属主目录存储）。"""
    d = await obj.to_dict(exclude_fields=["api_key_ciphertext"])
    d["has_api_key"] = bool(obj.api_key_ciphertext)
    d["avatar_url"] = agent_avatar_url(username, obj.avatar_filename)
    d["owner_username"] = username
    return d


@router.get("/list", summary="我的智能体列表", tags=["智能体模块"])
async def list_user_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    user_id = CTX_USER_ID.get()
    user_obj = await user_controller.get(id=user_id)
    username = user_obj.username
    q = Q(user_id=user_id)
    total, objs = await user_agent_controller.list(page=page, page_size=page_size, search=q, order=["-id"])
    data = []
    for obj in objs:
        data.append(await _public_agent_dict(obj, username))
    return SuccessExtra(data=data, total=total, page=page, page_size=page_size)


@router.get("/public", summary="智能体广场（已发布，不含自己的）", tags=["智能体模块"])
async def list_public_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    user_id = CTX_USER_ID.get()
    base = UserAgent.filter(is_published=True).exclude(user_id=user_id)
    total = await base.count()
    objs = (
        await base.select_related("user")
        .order_by("-updated_at")
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    data = []
    for obj in objs:
        owner = obj.user
        data.append(await _public_agent_dict(obj, owner.username if owner else ""))
    return SuccessExtra(data=data, total=total, page=page, page_size=page_size)


@router.get("/get", summary="智能体详情", tags=["智能体模块"])
async def get_user_agent(agent_id: int = Query(..., description="智能体 ID")):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_accessible(agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    owner = await obj.user
    d = await _public_agent_dict(obj, owner.username)
    if int(obj.user_id or 0) != int(user_id):
        d["has_api_key"] = False
    return Success(data=d)


@router.post("/create", summary="创建智能体", tags=["智能体模块"])
async def create_user_agent(body: UserAgentCreate):
    user_id = CTX_USER_ID.get()
    payload = body.model_dump()
    plain = payload.pop("api_key")
    payload["api_key_ciphertext"] = encrypt_api_key(plain)
    payload["user_id"] = user_id
    obj = await UserAgent.create(**payload)
    user_obj = await user_controller.get(id=user_id)
    d = await _public_agent_dict(obj, user_obj.username)
    return Success(data=d, msg="创建成功")


@router.post("/update", summary="更新智能体", tags=["智能体模块"])
async def update_user_agent(body: UserAgentUpdate):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_owned(body.id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    payload = body.model_dump(exclude={"id"})
    api_key = payload.pop("api_key", None)
    if api_key and api_key.strip():
        obj.api_key_ciphertext = encrypt_api_key(api_key.strip())
    obj = obj.update_from_dict(payload)
    if obj.is_published and not obj.api_key_ciphertext:
        return Fail(code=400, msg="发布智能体前请先配置模型 API Key")
    await obj.save()
    user_obj = await user_controller.get(id=user_id)
    d = await _public_agent_dict(obj, user_obj.username)
    return Success(data=d, msg="更新成功")


@router.delete("/delete", summary="删除智能体", tags=["智能体模块"])
async def delete_user_agent(agent_id: int = Query(..., description="智能体 ID")):
    user_id = CTX_USER_ID.get()
    user_obj = await user_controller.get(id=user_id)
    obj = await user_agent_controller.get_owned(agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    fn = obj.avatar_filename
    aid = obj.id
    await obj.delete()
    remove_agent_avatar_file(user_obj.username, fn)
    try:
        purge_kb_for_scope(kb_scope_for(user_id, aid), user_id, aid)
    except Exception:
        pass
    return Success(msg="删除成功")


@router.post("/upload_avatar", summary="上传智能体头像", tags=["智能体模块"])
async def upload_agent_avatar(
    agent_id: int = Query(..., description="智能体 ID"),
    file: UploadFile = File(...),
):
    user_id = CTX_USER_ID.get()
    user_obj = await user_controller.get(id=user_id)
    username = user_obj.username
    obj = await user_agent_controller.get_owned(agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    new_name, err = await save_uploaded_agent_avatar(username, file)
    if err:
        return Fail(msg=err)
    old_fn = obj.avatar_filename
    obj.avatar_filename = new_name
    await obj.save()
    remove_agent_avatar_file(username, old_fn)
    return Success(
        data={
            "avatar_url": agent_avatar_url(username, new_name),
            "avatar_filename": new_name,
        },
        msg="上传成功",
    )
