import asyncio
import logging

from fastapi import APIRouter, File, Query, UploadFile
from tortoise.expressions import Q

from app.chat.storage import storage
from app.controllers.user import user_controller
from app.controllers.user_agent import user_agent_controller
from app.core.ctx import CTX_USER_ID
from app.models.admin import User
from app.models.user_agent import UserAgent
from app.models.user_agent_share import UserAgentShare
from app.schemas.base import Fail, Success, SuccessExtra
from app.schemas.user_agent import (
    UserAgentCreate,
    UserAgentOfflineIn,
    UserAgentPublishIn,
    UserAgentShareIn,
    UserAgentUpdate,
)
from app.utils.api_key_crypto import encrypt_api_key
from app.kb.kb_scope import kb_scope_for
from app.kb.kb_service import purge_kb_for_scope
from app.utils.avatar import enrich_user_avatar
from app.utils.user_agent_avatar import (
    agent_avatar_url,
    remove_agent_avatar_file,
    save_uploaded_agent_avatar,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _public_agent_dict(obj: UserAgent, username: str) -> dict:
    """不包含密文，附带 has_api_key、属主用户名与共享人数（头像按属主目录存储）。"""
    d = await obj.to_dict(exclude_fields=["api_key_ciphertext"])
    d["has_api_key"] = bool(obj.api_key_ciphertext)
    d["avatar_url"] = agent_avatar_url(username, obj.avatar_filename)
    d["owner_username"] = username
    d["shared_count"] = await UserAgentShare.filter(agent_id=obj.id).count()
    return d


async def _valid_share_user_ids(user_ids: list[int], exclude_user_id: int) -> list[int]:
    """去重、剔除属主与不存在的/未激活用户，返回可共享的用户 ID 列表。"""
    ids = {int(uid) for uid in user_ids if str(uid).isdigit()}
    ids.discard(exclude_user_id)
    if not ids:
        return []
    return await User.filter(id__in=list(ids), is_active=True).values_list("id", flat=True)


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


@router.get("/shared", summary="分享给我的智能体（已发布且我在共享名单内）", tags=["智能体模块"])
async def list_shared_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    user_id = CTX_USER_ID.get()
    base = UserAgent.filter(is_published=True, shares__user_id=user_id).exclude(user_id=user_id)
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


@router.post("/publish", summary="共享智能体（仅指定用户可对话）", tags=["智能体模块"])
async def publish_user_agent(body: UserAgentPublishIn):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_owned(body.agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    if not obj.api_key_ciphertext:
        return Fail(code=400, msg="共享智能体前请先配置模型 API Key")
    ids = await _valid_share_user_ids(body.user_ids, exclude_user_id=user_id)
    if not ids:
        return Fail(code=400, msg="请选择至少 1 位有效共享用户")
    existing = set(await UserAgentShare.filter(agent_id=obj.id).values_list("user_id", flat=True))
    to_add = [uid for uid in ids if uid not in existing]
    if to_add:
        await UserAgentShare.bulk_create(
            [UserAgentShare(agent_id=obj.id, user_id=uid) for uid in to_add]
        )
    if not obj.is_published:
        obj.is_published = True
        await obj.save()
    user_obj = await user_controller.get(id=user_id)
    d = await _public_agent_dict(obj, user_obj.username)
    return Success(data=d, msg="共享成功")


@router.post("/offline", summary="取消共享（所有共享用户不可再用）", tags=["智能体模块"])
async def offline_user_agent(body: UserAgentOfflineIn):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_owned(body.agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    if obj.is_published:
        obj.is_published = False
        await obj.save()
    return Success(msg="已取消共享")


@router.get("/share/list", summary="智能体共享用户名单", tags=["智能体模块"])
async def list_agent_shares(agent_id: int = Query(..., description="智能体 ID")):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_owned(agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    shares = await UserAgentShare.filter(agent_id=agent_id).select_related("user").order_by("id")
    data = []
    for s in shares:
        u = s.user
        if not u:
            continue
        item = {"id": u.id, "username": u.username, "alias": u.alias, "email": u.email, "avatar": u.avatar}
        enrich_user_avatar(item)
        data.append(item)
    return Success(data=data)


@router.post("/share/add", summary="增量添加共享用户", tags=["智能体模块"])
async def add_agent_shares(body: UserAgentShareIn):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_owned(body.agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    ids = await _valid_share_user_ids(body.user_ids, exclude_user_id=user_id)
    if not ids:
        return Fail(code=400, msg="没有可添加的有效用户")
    existing = set(await UserAgentShare.filter(agent_id=obj.id).values_list("user_id", flat=True))
    to_add = [uid for uid in ids if uid not in existing]
    if to_add:
        await UserAgentShare.bulk_create(
            [UserAgentShare(agent_id=obj.id, user_id=uid) for uid in to_add]
        )
    return Success(msg="已添加")


@router.post("/share/remove", summary="移除共享用户", tags=["智能体模块"])
async def remove_agent_shares(body: UserAgentShareIn):
    user_id = CTX_USER_ID.get()
    obj = await user_agent_controller.get_owned(body.agent_id, user_id)
    if not obj:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    await UserAgentShare.filter(agent_id=obj.id, user_id__in=body.user_ids).delete()
    return Success(msg="已移除")


@router.get("/share/search_users", summary="搜索可共享用户（名称/邮箱模糊）", tags=["智能体模块"])
async def search_share_users(q: str = Query("", max_length=50, description="用户名/姓名/邮箱关键字")):
    user_id = CTX_USER_ID.get()
    keyword = q.strip()
    base = User.filter(is_active=True).exclude(id=user_id)
    if keyword:
        base = base.filter(
            Q(username__contains=keyword) | Q(alias__contains=keyword) | Q(email__contains=keyword)
        )
    users = await base.order_by("id").limit(20)
    data = []
    for u in users:
        item = {"id": u.id, "username": u.username, "alias": u.alias, "email": u.email, "avatar": u.avatar}
        enrich_user_avatar(item)
        data.append(item)
    return Success(data=data)


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
    try:
        await asyncio.to_thread(storage.purge_chat_data_for_agent, user_id, aid)
    except Exception:
        logger.exception("purge_chat_data_for_agent user_id=%s agent_id=%s", user_id, aid)
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
