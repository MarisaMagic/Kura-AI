import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from tortoise import Tortoise

from app.core.exceptions import SettingNotFound
from app.core.init_app import (
    init_data,
    make_middlewares,
    register_exceptions,
    register_routers,
)
try:
    from app.settings.config import settings
except ImportError:
    raise SettingNotFound("Can not import settings")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_data()
    try:
        from app.chat.database import init_chat_db

        init_chat_db()
    except Exception as e:
        from app.log import logger

        logger.error("PostgreSQL 聊天库初始化失败（智能体对话将不可用）: %s", e)
    try:
        from app.log import logger
        from app.kb.milvus_client import MilvusManager

        # 预热知识库 Milvus 集合：首次连接较慢，提前到启动期避免首个上传任务等待
        await asyncio.to_thread(MilvusManager().init_collection)
    except Exception as e:
        logger.warning("Milvus 集合预热失败（首次上传时自动重试）: %s", e)
    yield
    await Tortoise.close_connections()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_TITLE,
        description=settings.APP_DESCRIPTION,
        version=settings.VERSION,
        openapi_url="/openapi.json",
        middleware=make_middlewares(),
        lifespan=lifespan,
    )
    register_exceptions(app)
    register_routers(app, prefix="/api")
    os.makedirs(settings.USER_AVATAR_ROOT, exist_ok=True)
    os.makedirs(settings.USER_AGENT_AVATAR_ROOT, exist_ok=True)
    os.makedirs(settings.USER_AGENT_CHAT_UPLOAD_ROOT, exist_ok=True)
    os.makedirs(settings.USER_AGENT_KB_IMAGES_ROOT, exist_ok=True)
    app.mount(
        settings.USER_AVATAR_URL_PREFIX,
        StaticFiles(directory=settings.USER_AVATAR_ROOT),
        name="user_avatar_files",
    )
    app.mount(
        settings.USER_AGENT_AVATAR_URL_PREFIX,
        StaticFiles(directory=settings.USER_AGENT_AVATAR_ROOT),
        name="user_agent_avatar_files",
    )
    app.mount(
        settings.USER_AGENT_KB_IMAGES_URL_PREFIX,
        StaticFiles(directory=settings.USER_AGENT_KB_IMAGES_ROOT),
        name="user_agent_kb_images_files",
    )
    return app


app = create_app()
