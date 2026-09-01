from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.core import object_storage as obs
from app.settings import settings
from app.utils.signed_media import KIND_AGENT_AVATAR, sign_media_url

# 与前端 public/logo.svg 一致，由页面同源加载
DEFAULT_AGENT_AVATAR_URL = "/logo.svg"
ALLOWED_AGENT_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def agent_avatar_url(username: str, avatar_filename: str | None) -> str:
    if not avatar_filename:
        return DEFAULT_AGENT_AVATAR_URL
    safe_user = re.sub(r"[^\w\-.]", "_", username)[:80]
    return sign_media_url(KIND_AGENT_AVATAR, f"user_{safe_user}/{avatar_filename}")


def safe_agent_avatar_extension(filename: str | None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_AGENT_AVATAR_EXTENSIONS else None


def user_agent_avatar_key_prefix(username: str) -> str:
    """智能体头像在 bucket 内的 key 前缀（USER_AGENT_AVATAR_ROOT 语义为前缀）。"""
    safe = re.sub(r"[^\w\-.]", "_", username)[:80]
    return obs.join_key(settings.USER_AGENT_AVATAR_ROOT, f"user_{safe}")


async def save_uploaded_agent_avatar(username: str, file: UploadFile) -> tuple[str | None, str | None]:
    """返回 (filename, error_msg)。"""
    ext = safe_agent_avatar_extension(file.filename)
    if not ext:
        return None, f"仅支持以下格式：{', '.join(sorted(ALLOWED_AGENT_AVATAR_EXTENSIONS))}"
    contents = await file.read()
    max_bytes = 2 * 1024 * 1024
    if len(contents) > max_bytes:
        return None, "文件大小不能超过 2MB"
    try:
        from app.utils.upload_sniff import assert_upload_magic

        assert_upload_magic(file.filename or f"avatar{ext}", contents)
    except ValueError as e:
        return None, str(e)
    new_name = f"{uuid.uuid4().hex}{ext}"
    mime = mimetypes.guess_type(file.filename or new_name)[0] or "application/octet-stream"
    obs.save_bytes(obs.join_key(user_agent_avatar_key_prefix(username), new_name), contents, content_type=mime)
    return new_name, None


def remove_agent_avatar_file(username: str, avatar_filename: str | None) -> None:
    if not avatar_filename:
        return
    obs.delete_key(obs.join_key(user_agent_avatar_key_prefix(username), avatar_filename))
