import logging

from fastapi import APIRouter, Body, Depends, Query
from tortoise.expressions import Q

from app.controllers.dept import dept_controller
from app.controllers.user import user_controller
from app.core.dependency import AuthControl
from app.models.admin import User
from app.schemas.base import Fail, Success, SuccessExtra
from app.schemas.users import *
from app.utils.avatar import enrich_user_avatar

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/list", summary="查看用户列表")
async def list_user(
    page: int = Query(1, description="页码"),
    page_size: int = Query(10, description="每页数量"),
    username: str = Query("", description="用户名称，用于搜索"),
    email: str = Query("", description="邮箱地址"),
    dept_id: int = Query(None, description="部门ID"),
):
    q = Q()
    if username:
        q &= Q(username__contains=username)
    if email:
        q &= Q(email__contains=email)
    if dept_id is not None:
        q &= Q(dept_id=dept_id)
    total, user_objs = await user_controller.list(page=page, page_size=page_size, search=q)
    data = [await obj.to_dict(m2m=True, exclude_fields=["password"]) for obj in user_objs]
    for item in data:
        enrich_user_avatar(item)
        dept_id = item.pop("dept_id", None)
        item["dept"] = await (await dept_controller.get(id=dept_id)).to_dict() if dept_id else {}

    return SuccessExtra(data=data, total=total, page=page, page_size=page_size)


@router.get("/get", summary="查看用户")
async def get_user(
    user_id: int = Query(..., description="用户ID"),
):
    user_obj = await user_controller.get(id=user_id)
    user_dict = await user_obj.to_dict(exclude_fields=["password"])
    enrich_user_avatar(user_dict)
    return Success(data=user_dict)


@router.post("/create", summary="创建用户")
async def create_user(
    user_in: UserCreate,
):
    user = await user_controller.get_by_email(user_in.email)
    if user:
        return Fail(code=400, msg="The user with this email already exists in the system.")
    new_user = await user_controller.create_user(obj_in=user_in)
    await user_controller.update_roles(new_user, user_in.role_ids)
    return Success(msg="Created Successfully", data={"id": new_user.id})


@router.post("/update", summary="更新用户")
async def update_user(
    user_in: UserUpdate,
):
    existing = await user_controller.get(id=user_in.id)
    was_active = bool(existing.is_active)
    payload = user_in.model_dump(exclude_unset=True, exclude={"id", "role_ids", "is_superuser"})
    user = await user_controller.update(id=user_in.id, obj_in=payload)
    await user_controller.update_roles(user, user_in.role_ids)
    if was_active and user_in.is_active is False:
        await user_controller.bump_auth_epoch(user)
    return Success(msg="Updated Successfully")


@router.post("/set_superuser", summary="设置超级管理员（仅现有超管）")
async def set_superuser(
    body: SetSuperuserRequest,
    current_user: User = Depends(AuthControl.is_authed),
):
    if not current_user.is_superuser:
        return Fail(code=403, msg="仅超级管理员可设置该标记")
    if int(body.user_id) == int(current_user.id):
        return Fail(code=400, msg="不能修改自己的超级管理员标记")
    target = await user_controller.get(id=body.user_id)
    if target.is_superuser and not body.is_superuser:
        others = await User.filter(is_superuser=True).exclude(id=target.id).count()
        if others < 1:
            return Fail(code=400, msg="不能取消最后一个超级管理员")
    target.is_superuser = bool(body.is_superuser)
    await user_controller.bump_auth_epoch(target)
    return Success(msg="Updated Successfully")


@router.delete("/delete", summary="删除用户")
async def delete_user(
    user_id: int = Query(..., description="用户ID"),
):
    await user_controller.remove(id=user_id)
    return Success(msg="Deleted Successfully")


@router.post("/reset_password", summary="重置密码")
async def reset_password(
    user_id: int = Body(..., description="用户ID", embed=True),
    new_password: str = Body(..., min_length=8, max_length=128, description="新密码（至少 8 位且同时包含字母与数字）", embed=True),
):
    await user_controller.reset_password(user_id, new_password)
    return Success(msg="密码已重置")
