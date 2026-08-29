from fastapi import APIRouter

from app.core.dependency import DependAuth, DependPermission

from .apis import apis_router
from .auditlog import auditlog_router
from .base import base_router
from .depts import depts_router
from .media import router as media_router
from .menus import menus_router
from .roles import roles_router
from .user_agents import agent_chat_router, user_agents_router
from .user_agents.kb import router as user_agent_kb_router
from .user_agents.mcp import router as user_agent_mcp_router
from .users import users_router

v1_router = APIRouter()

v1_router.include_router(media_router, prefix="/media")
v1_router.include_router(base_router, prefix="/base")
v1_router.include_router(users_router, prefix="/user", dependencies=[DependPermission])
v1_router.include_router(user_agents_router, prefix="/user-agent", dependencies=[DependPermission])
v1_router.include_router(agent_chat_router, prefix="/user-agent", dependencies=[DependAuth])
v1_router.include_router(user_agent_kb_router, prefix="/user-agent", dependencies=[DependAuth])
v1_router.include_router(user_agent_mcp_router, prefix="/user-agent", dependencies=[DependAuth])
v1_router.include_router(roles_router, prefix="/role", dependencies=[DependPermission])
v1_router.include_router(menus_router, prefix="/menu", dependencies=[DependPermission])
v1_router.include_router(apis_router, prefix="/api", dependencies=[DependPermission])
v1_router.include_router(depts_router, prefix="/dept", dependencies=[DependPermission])
v1_router.include_router(auditlog_router, prefix="/auditlog", dependencies=[DependPermission])
