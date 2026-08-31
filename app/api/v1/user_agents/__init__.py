from fastapi import APIRouter

from .chat import router as agent_chat_router
from .user_agents import auth_router, router

user_agents_router = APIRouter()
user_agents_router.include_router(router)

__all__ = ["user_agents_router", "agent_chat_router", "auth_router"]
