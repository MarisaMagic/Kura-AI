from aerich import Command
from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from tortoise import Tortoise
from tortoise.expressions import Q

from app.api import api_router
from app.controllers.api import api_controller
from app.controllers.user import UserCreate, user_controller
from app.core.exceptions import (
    DoesNotExist,
    DoesNotExistHandle,
    HTTPException,
    HttpExcHandle,
    IntegrityError,
    IntegrityHandle,
    RequestValidationError,
    RequestValidationHandle,
    ResponseValidationError,
    ResponseValidationHandle,
)
from app.log import logger
from app.models.admin import Api, Menu, Role
from app.schemas.menus import MenuType
from app.settings.config import settings

from .middlewares import BackGroundTaskMiddleware, HttpAuditLogMiddleware


def make_middlewares():
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
            allow_methods=settings.CORS_ALLOW_METHODS,
            allow_headers=settings.CORS_ALLOW_HEADERS,
        ),
        Middleware(BackGroundTaskMiddleware),
        Middleware(
            HttpAuditLogMiddleware,
            methods=["GET", "POST", "PUT", "DELETE"],
            exclude_paths=[
                "/api/v1/base/access_token",
                "/api/v1/base/register",
                "/api/v1/base/registration_enabled",
                "/api/v1/base/upload_avatar",
                "/api/v1/user/reset_password",
                "/api/v1/user-agent/upload_avatar",
                r".*user-agent/chat/stream$",  # POST 直连 SSE，勿缓冲整包写入审计
                r".*user-agent/chat/jobs/[^/]+/stream$",  # Job 订阅 SSE（刷新可重连），勿缓冲
                r".*user-agent/kb/upload$",
                r".*user-agent/chat/attachments/upload$",
                "/api/v1/media/",  # 静态头像等二进制，勿写入审计 JSON
                "/docs",
                "/openapi.json",
            ],
        ),
    ]
    return middleware


def register_exceptions(app: FastAPI):
    app.add_exception_handler(DoesNotExist, DoesNotExistHandle)
    app.add_exception_handler(HTTPException, HttpExcHandle)
    app.add_exception_handler(IntegrityError, IntegrityHandle)
    app.add_exception_handler(RequestValidationError, RequestValidationHandle)
    app.add_exception_handler(ResponseValidationError, ResponseValidationHandle)


def register_routers(app: FastAPI, prefix: str = "/api"):
    app.include_router(api_router, prefix=prefix)


async def init_superuser():
    user = await user_controller.model.exists()
    if not user:
        password = settings.INITIAL_ADMIN_PASSWORD
        if not password:
            raise RuntimeError(
                "数据库尚无用户，请在 .env 中配置 INITIAL_ADMIN_PASSWORD 后重启（至少 8 位且含字母与数字）"
            )
        from app.utils.password import validate_password_strength

        try:
            validate_password_strength(password)
        except ValueError as exc:
            raise RuntimeError(f"INITIAL_ADMIN_PASSWORD 不符合要求: {exc}") from exc
        await user_controller.create_user(
            UserCreate(
                username=settings.INITIAL_ADMIN_USERNAME,
                email=settings.INITIAL_ADMIN_EMAIL.strip().lower(),
                password=password,
                is_active=True,
                is_superuser=True,
            )
        )
        logger.info("已创建初始管理员用户: %s", settings.INITIAL_ADMIN_USERNAME)


async def init_menus():
    menus = await Menu.exists()
    if not menus:
        parent_menu = await Menu.create(
            menu_type=MenuType.CATALOG,
            name="系统管理",
            path="/system",
            order=1,
            parent_id=0,
            icon="carbon:gui-management",
            is_hidden=False,
            component="Layout",
            keepalive=False,
            redirect="/system/user",
        )
        children_menu = [
            Menu(
                menu_type=MenuType.MENU,
                name="用户管理",
                path="user",
                order=1,
                parent_id=parent_menu.id,
                icon="material-symbols:person-outline-rounded",
                is_hidden=False,
                component="/system/user",
                keepalive=False,
            ),
            Menu(
                menu_type=MenuType.MENU,
                name="角色管理",
                path="role",
                order=2,
                parent_id=parent_menu.id,
                icon="carbon:user-role",
                is_hidden=False,
                component="/system/role",
                keepalive=False,
            ),
            Menu(
                menu_type=MenuType.MENU,
                name="菜单管理",
                path="menu",
                order=3,
                parent_id=parent_menu.id,
                icon="material-symbols:list-alt-outline",
                is_hidden=False,
                component="/system/menu",
                keepalive=False,
            ),
            Menu(
                menu_type=MenuType.MENU,
                name="API管理",
                path="api",
                order=4,
                parent_id=parent_menu.id,
                icon="ant-design:api-outlined",
                is_hidden=False,
                component="/system/api",
                keepalive=False,
            ),
            Menu(
                menu_type=MenuType.MENU,
                name="部门管理",
                path="dept",
                order=5,
                parent_id=parent_menu.id,
                icon="mingcute:department-line",
                is_hidden=False,
                component="/system/dept",
                keepalive=False,
            ),
            Menu(
                menu_type=MenuType.MENU,
                name="审计日志",
                path="auditlog",
                order=6,
                parent_id=parent_menu.id,
                icon="ph:clipboard-text-bold",
                is_hidden=False,
                component="/system/auditlog",
                keepalive=False,
            ),
        ]
        await Menu.bulk_create(children_menu)


async def remove_legacy_top_menu_demo():
    """已移除的演示侧栏项 /top-menu，旧库启动时清理。"""
    await Menu.filter(path="/top-menu", component="/top-menu").delete()


async def init_apis():
    """同步 OpenAPI 路由到 api 表（新增路由后需执行以出现在权限管理中）。"""
    await api_controller.refresh_api()


async def init_db():
    command = Command(tortoise_config=settings.TORTOISE_ORM)
    try:
        await command.init_db(safe=True)
    except FileExistsError:
        pass

    await command.init()
    # 不在启动时调用 command.migrate()：会对比模型生成迁移，SQLite 对「改列」会报 NotSupportError。
    # 变更模型请在开发机执行 `aerich migrate` 并提交迁移；启动时只执行已有迁移。
    await command.upgrade(run_in_transaction=True)
    await ensure_user_avatar_column()
    await ensure_user_agent_base_url_column()
    await ensure_user_agent_supports_vision_column()


async def ensure_user_avatar_column() -> None:
    """旧库可能缺少 avatar 列；aerich 未覆盖时补齐（SQLite）。"""
    try:
        conn = Tortoise.get_connection("sqlite")
    except Exception:
        return
    try:
        await conn.execute_query('ALTER TABLE "user" ADD COLUMN "avatar" VARCHAR(255)')
        logger.info('Added column "user"."avatar"')
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return
        logger.warning('ensure_user_avatar_column: %s', e)


async def ensure_user_agent_base_url_column() -> None:
    """旧库可能缺少 base_url 列；aerich 未覆盖时补齐（SQLite）。"""
    try:
        conn = Tortoise.get_connection("sqlite")
    except Exception:
        return
    try:
        await conn.execute_query('ALTER TABLE "user_agent" ADD COLUMN "base_url" VARCHAR(512)')
        logger.info('Added column "user_agent"."base_url"')
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return
        logger.warning('ensure_user_agent_base_url_column: %s', e)


async def ensure_user_agent_supports_vision_column() -> None:
    """旧库可能缺少 supports_vision 列；aerich 未覆盖时补齐（SQLite）。"""
    try:
        conn = Tortoise.get_connection("sqlite")
    except Exception:
        return
    try:
        await conn.execute_query(
            'ALTER TABLE "user_agent" ADD COLUMN "supports_vision" INTEGER NOT NULL DEFAULT 0'
        )
        logger.info('Added column "user_agent"."supports_vision"')
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return
        logger.warning('ensure_user_agent_supports_vision_column: %s', e)


async def ensure_agent_menus():
    """智能体：一级菜单「智能体中心」；创建/编辑为全屏路由，不在侧栏。"""
    legacy = await Menu.filter(path="/agents", parent_id=0).first()
    if legacy:
        await Menu.filter(parent_id=legacy.id).delete()
        legacy.name = "智能体中心"
        legacy.path = "/agent-hub"
        legacy.menu_type = MenuType.MENU
        legacy.component = "/agent-hub"
        legacy.redirect = None
        legacy.icon = legacy.icon or "material-symbols:smart-toy-outline"
        await legacy.save()
    hub = await Menu.filter(path="/agent-hub", parent_id=0).first()
    if not hub:
        hub = await Menu.create(
            menu_type=MenuType.MENU,
            name="智能体中心",
            path="/agent-hub",
            order=3,
            parent_id=0,
            icon="material-symbols:smart-toy-outline",
            is_hidden=False,
            component="/agent-hub",
            keepalive=False,
            redirect=None,
        )
        # 仅在新菜单首次落库时授予已有角色；不在每次启动时把「全库菜单」补回各角色，否则会覆盖管理员在角色里撤掉的菜单。
        for role in await Role.all():
            existing = await role.menus.all()
            have = {m.id for m in existing}
            if hub.id not in have:
                await role.menus.add(hub)


async def ensure_user_agent_apis_for_roles():
    """智能体模块 API 同步到所有角色（新接口上线后自动补授权）。"""
    agent_apis = await Api.filter(tags="智能体模块")
    if not agent_apis:
        return
    for role in await Role.all():
        existing = await role.apis.all()
        have = {a.id for a in existing}
        for a in agent_apis:
            if a.id not in have:
                await role.apis.add(a)


async def init_roles():
    roles = await Role.exists()
    if not roles:
        admin_role = await Role.create(
            name="管理员",
            desc="管理员角色",
        )
        user_role = await Role.create(
            name="普通用户",
            desc="普通用户角色",
        )

        # 分配所有API给管理员角色
        all_apis = await Api.all()
        await admin_role.apis.add(*all_apis)
        # 分配所有菜单给管理员和普通用户
        all_menus = await Menu.all()
        await admin_role.menus.add(*all_menus)
        await user_role.menus.add(*all_menus)

        # 为普通用户分配基本API
        basic_apis = await Api.filter(Q(method__in=["GET"]) | Q(tags="基础模块"))
        await user_role.apis.add(*basic_apis)


async def init_data():
    await init_db()
    await init_superuser()
    await init_menus()
    await ensure_agent_menus()
    await remove_legacy_top_menu_demo()
    await init_apis()
    await init_roles()
    await ensure_user_agent_apis_for_roles()
