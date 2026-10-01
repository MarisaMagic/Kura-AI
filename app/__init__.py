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
    from app.core.loop_monitor import start_loop_monitor, stop_loop_monitor
    from app.log import logger
    from app.utils.concurrency import setup_default_executor

    # 并发基础设施须最先就绪：默认线程池承载 to_thread 与 LangChain 同步工具
    setup_default_executor()
    start_loop_monitor()

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
        from app.kb.milvus_client import get_milvus_manager

        # 预热知识库 Milvus 集合：首次连接较慢，提前到启动期避免首个上传任务等待。
        # 硬超时保护：Milvus 半死/不可达时不得阻塞服务启动（超时线程由进程退出回收）。
        await asyncio.wait_for(asyncio.to_thread(get_milvus_manager().init_collection), timeout=20)
    except asyncio.TimeoutError:
        logger.warning("Milvus 预热超时（20s），跳过；首次上传时自动重试")
    except Exception as e:
        logger.warning("Milvus 集合预热失败（首次上传时自动重试）: %s", e)
    if settings.DEBUG:
        logger.warning(
            "DEBUG=true：Header token=dev 可跳过 JWT，仅限本机调试，公网务必关闭"
        )
    yield
    stop_loop_monitor()
    try:
        from app.chat.web_search_providers import close_async_search_clients
        from app.utils.egress import close_pinned_async_http_clients, close_pinned_llm_clients

        await close_pinned_llm_clients()
        await close_pinned_async_http_clients()
        await close_async_search_clients()
    except Exception as e:
        logger.warning("关闭缓存的 LLM/出站客户端失败（不影响退出）: %s", e)
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
