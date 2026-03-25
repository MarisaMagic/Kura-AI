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
    return app


app = create_app()
