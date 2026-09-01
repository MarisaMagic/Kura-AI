import mimetypes
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, File, Request, UploadFile

from app.controllers.user import user_controller
from app.core import object_storage as obs
from app.core.ctx import CTX_USER_ID
from app.core.dependency import DependAuth
from app.models.admin import Api, Menu, Role, User
from app.schemas.base import Fail, Success
from app.schemas.login import *
from app.schemas.users import UpdatePassword
from app.settings import settings
from app.utils.auth_rate_limit import check_auth_rate_limit
from app.utils.avatar import ALLOWED_AVATAR_EXTENSIONS, avatar_url_from_filename, enrich_user_avatar, safe_avatar_extension
from app.utils.jwt_utils import create_access_token
from app.utils.password import get_password_hash, validate_password_strength, verify_password

router = APIRouter()


@router.get("/health", summary="存活检查", tags=["基础模块"])
async def health():
    return {"status": "ok"}


@router.get("/registration_enabled", summary="是否开放自助注册", tags=["基础模块"])
async def registration_enabled():
    return Success(data={"enabled": settings.ALLOW_PUBLIC_REGISTRATION})


@router.post("/register", summary="邮箱注册", tags=["基础模块"])
async def register_user(body: RegisterSchema, request: Request):
    if not settings.ALLOW_PUBLIC_REGISTRATION:
        return Fail(code=403, msg="当前未开放注册")
    check_auth_rate_limit(
        request,
        action="register",
        limit=settings.AUTH_REGISTER_RATE_LIMIT,
        window_seconds=settings.AUTH_REGISTER_RATE_WINDOW_SECONDS,
    )
    user = await user_controller.register_user(body)
    return Success(msg="注册成功", data={"username": user.username, "email": user.email})


@router.post("/access_token", summary="获取token", tags=["基础模块"])
async def login_access_token(credentials: CredentialsSchema, request: Request):
    check_auth_rate_limit(
        request,
        action="login",
        limit=settings.AUTH_LOGIN_RATE_LIMIT,
        window_seconds=settings.AUTH_LOGIN_RATE_WINDOW_SECONDS,
    )
    user: User = await user_controller.authenticate(credentials)
    await user_controller.update_last_login(user.id)
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.now(timezone.utc) + access_token_expires

    data = JWTOut(
        access_token=create_access_token(
            data=JWTPayload(
                user_id=user.id,
                username=user.username,
                is_superuser=user.is_superuser,
                exp=expire,
            )
        ),
        username=user.username,
    )
    return Success(data=data.model_dump())


@router.get("/userinfo", summary="查看用户信息", dependencies=[DependAuth])
async def get_userinfo():
    user_id = CTX_USER_ID.get()
    user_obj = await user_controller.get(id=user_id)
    data = await user_obj.to_dict(exclude_fields=["password"])
    enrich_user_avatar(data)
    return Success(data=data)


@router.post("/upload_avatar", summary="上传头像", dependencies=[DependAuth])
async def upload_avatar(file: UploadFile = File(...)):
    user_id = CTX_USER_ID.get()
    user_obj = await user_controller.get(id=user_id)
    ext = safe_avatar_extension(file.filename)
    if not ext:
        return Fail(
            msg=f"仅支持以下格式：{', '.join(sorted(ALLOWED_AVATAR_EXTENSIONS))}",
        )
    contents = await file.read()
    max_bytes = 2 * 1024 * 1024
    if len(contents) > max_bytes:
        return Fail(msg="文件大小不能超过 2MB")
    try:
        from app.utils.upload_sniff import assert_upload_magic

        assert_upload_magic(file.filename or f"avatar{ext}", contents)
    except ValueError as e:
        return Fail(msg=str(e))
    new_name = f"{uuid.uuid4().hex}{ext}"
    if user_obj.avatar:
        obs.delete_key(obs.join_key(settings.USER_AVATAR_ROOT, user_obj.avatar))
    mime = mimetypes.guess_type(file.filename or new_name)[0] or "application/octet-stream"
    obs.save_bytes(obs.join_key(settings.USER_AVATAR_ROOT, new_name), contents, content_type=mime)
    user_obj.avatar = new_name
    await user_obj.save()
    return Success(data={"avatar": avatar_url_from_filename(new_name)})


@router.get("/usermenu", summary="查看用户菜单", dependencies=[DependAuth])
async def get_user_menu():
    user_id = CTX_USER_ID.get()
    user_obj = await User.filter(id=user_id).first()
    menus: list[Menu] = []
    if user_obj.is_superuser:
        menus = await Menu.all()
    else:
        role_objs: list[Role] = await user_obj.roles
        for role_obj in role_objs:
            menu = await role_obj.menus
            menus.extend(menu)
        menus = list(set(menus))
    parent_menus: list[Menu] = []
    for menu in menus:
        if menu.parent_id == 0:
            parent_menus.append(menu)
    res = []
    for parent_menu in parent_menus:
        parent_menu_dict = await parent_menu.to_dict()
        parent_menu_dict["children"] = []
        for menu in menus:
            if menu.parent_id == parent_menu.id:
                parent_menu_dict["children"].append(await menu.to_dict())
        res.append(parent_menu_dict)
    return Success(data=res)


@router.get("/userapi", summary="查看用户API", dependencies=[DependAuth])
async def get_user_api():
    user_id = CTX_USER_ID.get()
    user_obj = await User.filter(id=user_id).first()
    if user_obj.is_superuser:
        api_objs: list[Api] = await Api.all()
        apis = [api.method.lower() + api.path for api in api_objs]
        return Success(data=apis)
    role_objs: list[Role] = await user_obj.roles
    apis = []
    for role_obj in role_objs:
        api_objs: list[Api] = await role_obj.apis
        apis.extend([api.method.lower() + api.path for api in api_objs])
    apis = list(set(apis))
    return Success(data=apis)


@router.post("/update_password", summary="修改密码", dependencies=[DependAuth])
async def update_user_password(req_in: UpdatePassword):
    user_id = CTX_USER_ID.get()
    user = await user_controller.get(user_id)
    verified = verify_password(req_in.old_password, user.password)
    if not verified:
        return Fail(msg="旧密码验证错误！")
    try:
        validate_password_strength(req_in.new_password)
    except ValueError as exc:
        return Fail(msg=str(exc))
    user.password = get_password_hash(req_in.new_password)
    await user.save()
    return Success(msg="修改成功")
