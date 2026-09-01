import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    from app.log import logger

    await init_data()
    try:
        from app.core.object_storage import ensure_bucket

        # 对象存储 bucket 初始化（头像/会话附件/知识库文档与图片）；失败时文件功能不可用，其余功能照常
        await asyncio.to_thread(ensure_bucket)
    except Exception as e:
        logger.error("对象存储初始化失败（头像/附件/知识库文件功能将不可用）: %s", e)
    try:
        from app.chat.database import init_chat_db

        init_chat_db()
    except Exception as e:
        logger.error("PostgreSQL 聊天库初始化失败（智能体对话将不可用）: %s", e)
    try:
        from app.kb.milvus_client import MilvusManager

        # 预热知识库 Milvus 集合：首次连接较慢，提前到启动期避免首个上传任务等待
        await asyncio.to_thread(MilvusManager().init_collection)
    except Exception as e:
        logger.warning("Milvus 集合预热失败（首次上传时自动重试）: %s", e)
    if settings.DEBUG:
        logger.warning(
            "DEBUG=true：Header token=dev 可跳过 JWT，仅限本机调试，公网务必关闭"
        )
    yield
    await Tortoise.close_connections()


def create_app() -> FastAPI:
    docs_on = bool(getattr(settings, "DOCS_ENABLED", True))
    app = FastAPI(
        title=settings.APP_TITLE,
        description=settings.APP_DESCRIPTION,
        version=settings.VERSION,
        openapi_url="/openapi.json" if docs_on else None,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        middleware=make_middlewares(),
        lifespan=lifespan,
    )
    register_exceptions(app)
    register_routers(app, prefix="/api")
    return app


app = create_app()
