from fastapi.routing import APIRoute

from app.core.crud import CRUDBase
from app.log import logger
from app.models.admin import Api
from app.schemas.apis import ApiCreate, ApiUpdate

# 路径前缀 → 模块标签（用于权限分组与角色授权）
# 顺序即优先级，先匹配先生效
_PATH_TAG_RULES: list[tuple[str, str]] = [
    ("/api/v1/base", "基础模块"),
    ("/api/v1/user-agent", "智能体模块"),
    ("/api/v1/user", "用户模块"),
    ("/api/v1/role", "角色模块"),
    ("/api/v1/menu", "菜单模块"),
    ("/api/v1/api", "API模块"),
    ("/api/v1/dept", "部门模块"),
    ("/api/v1/auditlog", "审计日志模块"),
]


def _tag_for_path(path: str) -> str:
    for prefix, tag in _PATH_TAG_RULES:
        if path.startswith(prefix):
            return tag
    return "未分组"


def _walk_routes(router, prefix: str = "", parent_deps: int = 0):
    """递归遍历 FastAPI 嵌套路由（含 _IncludedRouter），产出 (route, full_path, effective_deps)。"""
    for r in router.routes:
        if isinstance(r, APIRoute):
            yield r, prefix + r.path, parent_deps + len(r.dependencies)
        elif hasattr(r, "original_router"):
            ctx = getattr(r, "include_context", None)
            p = getattr(ctx, "prefix", "") if ctx else ""
            d = len(getattr(ctx, "dependencies", []) or []) if ctx else 0
            yield from _walk_routes(r.original_router, prefix + p, parent_deps + d)
        elif hasattr(r, "routes"):
            yield from _walk_routes(r, prefix, parent_deps)


class ApiController(CRUDBase[Api, ApiCreate, ApiUpdate]):
    def __init__(self):
        super().__init__(model=Api)

    async def refresh_api(self):
        from app import app

        # 收集所有有鉴权的 API（含路由自身 dependencies 与 include_router 挂载的依赖）
        all_api_list = []
        for route, full_path, eff_deps in _walk_routes(app):
            if eff_deps > 0:
                all_api_list.append((list(route.methods)[0], full_path))

        # 删除废弃API数据
        delete_api = []
        for api in await Api.all():
            if (api.method, api.path) not in all_api_list:
                delete_api.append((api.method, api.path))
        for item in delete_api:
            method, path = item
            logger.debug(f"API Deleted {method} {path}")
            await Api.filter(method=method, path=path).delete()

        for route, full_path, eff_deps in _walk_routes(app):
            if eff_deps > 0:
                method = list(route.methods)[0]
                summary = route.summary
                tags = _tag_for_path(full_path)
                api_obj = await Api.filter(method=method, path=full_path).first()
                if api_obj:
                    await api_obj.update_from_dict(dict(method=method, path=full_path, summary=summary, tags=tags)).save()
                else:
                    logger.debug(f"API Created {method} {full_path}")
                    await Api.create(**dict(method=method, path=full_path, summary=summary, tags=tags))


api_controller = ApiController()
