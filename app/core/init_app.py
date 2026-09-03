from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from tortoise import Tortoise

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

from .middlewares import BackGroundTaskMiddleware, HttpAuditLogMiddleware, SecurityHeadersMiddleware
from .schema_patches import apply_schema_patches


def make_middlewares():
    middleware = [
        Middleware(SecurityHeadersMiddleware),
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
                "/api/v1/base/refresh",
                "/api/v1/base/logout",
                "/api/v1/base/register",
                "/api/v1/base/registration_enabled",
                "/api/v1/base/health",
                "/api/v1/base/upload_avatar",
                "/api/v1/base/update_password",
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
        admin = await user_controller.create_user(
            UserCreate(
                username=settings.INITIAL_ADMIN_USERNAME,
                email=settings.INITIAL_ADMIN_EMAIL.strip().lower(),
                password=password,
                is_active=True,
            )
        )
        admin.is_superuser = True
        await admin.save()
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
    # 直接按当前模型建表（IF NOT EXISTS，幂等）。aerich 已弃用；
    # 结构/数据补丁统一走版本化 schema_patches（按 schema_patch_log 去重、逐次启动追加）。
    await Tortoise.init(config=settings.TORTOISE_ORM)
    # 列级补丁须先于 generate_schemas 执行：它会为带 description 的新列生成 COMMENT ON COLUMN
    # （PG 无 IF EXISTS），旧库缺列时直接报错，事后补丁来不及兜底。
    # 全新库上表尚不存在，补丁内 ALTER 失败会被记录为未应用（下次启动重试），随后由建表覆盖新列。
    await apply_schema_patches(before_schemas=True)
    await Tortoise.generate_schemas(safe=True)
    await apply_schema_patches(before_schemas=False)
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

        # 为普通用户分配基本API（仅基础模块；智能体模块由 ensure_user_agent_apis_for_roles 补齐。
        # 不得按 GET 方法批量下放，否则 auditlog/user/role 等管理接口会泄露给普通用户）
        basic_apis = await Api.filter(tags="基础模块")
        await user_role.apis.add(*basic_apis)


async def restrict_normal_role_api_grants():
    """收回「普通用户」角色越权持有的系统管理类 API。

    历史版本曾按 GET 方法批量下放全部查询接口（含审计日志、用户列表等管理接口），
    此处对存量数据库做启动期清理；新装库由 init_roles 按标签授予，无需清理。
    采用黑名单方式（仅收回明确的管理模块），避免 tags 异常时误伤业务接口。
    """
    user_role = await Role.filter(name="普通用户").first()
    if not user_role:
        return
    admin_only_tags = {"审计日志模块", "API模块", "部门模块", "菜单模块", "角色模块", "用户模块"}
    excessive = [a for a in await user_role.apis if (a.tags or "") in admin_only_tags]
    if excessive:
        await user_role.apis.remove(*excessive)
        logger.warning("安全收紧：已收回「普通用户」角色的 %d 个管理类 API 授权", len(excessive))


async def init_data():
    await init_db()
    await init_superuser()
    await init_menus()
    await ensure_agent_menus()
    await remove_legacy_top_menu_demo()
    await init_apis()
    await init_roles()
    await ensure_user_agent_apis_for_roles()
    await restrict_normal_role_api_grants()
