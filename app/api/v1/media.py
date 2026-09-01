"""媒体文件（头像 / 智能体头像 / 知识库图片）签名读取，无需登录；后端从对象存储流式中转。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.utils.signed_media import (
    KIND_AGENT_AVATAR,
    KIND_KB_IMAGE,
    KIND_USER_AVATAR,
    serve_signed_media,
)

router = APIRouter()


@router.get("/user_avatar/{file_path:path}", summary="读取用户头像（需签名）")
async def get_user_avatar(
    file_path: str,
    exp: int = Query(..., description="签名过期时间（unix 秒）"),
    sig: str = Query(..., description="HMAC 签名"),
) -> StreamingResponse:
    return serve_signed_media(KIND_USER_AVATAR, file_path, exp, sig)


@router.get("/user_agents_avatar/{file_path:path}", summary="读取智能体头像（需签名）")
async def get_agent_avatar(
    file_path: str,
    exp: int = Query(..., description="签名过期时间（unix 秒）"),
    sig: str = Query(..., description="HMAC 签名"),
) -> StreamingResponse:
    return serve_signed_media(KIND_AGENT_AVATAR, file_path, exp, sig)


@router.get("/user_agent_images/{file_path:path}", summary="读取知识库图片（需签名）")
async def get_kb_image(
    file_path: str,
    exp: int = Query(..., description="签名过期时间（unix 秒）"),
    sig: str = Query(..., description="HMAC 签名"),
) -> StreamingResponse:
    return serve_signed_media(KIND_KB_IMAGE, file_path, exp, sig)
