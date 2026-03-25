from fastapi import APIRouter

from .user_agents import router

user_agents_router = APIRouter()
user_agents_router.include_router(router)

__all__ = ["user_agents_router"]
