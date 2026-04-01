from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.settings import settings

# 与前端 public/logo.svg 一致，由页面同源加载
DEFAULT_AGENT_AVATAR_URL = "/logo.svg"
ALLOWED_AGENT_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def agent_avatar_url(username: str, avatar_filename: str | None) -> str:
    if not avatar_filename:
        return DEFAULT_AGENT_AVATAR_URL
    prefix = settings.USER_AGENT_AVATAR_URL_PREFIX.rstrip("/")
    return f"{prefix}/user_{username}/{avatar_filename}"


def safe_agent_avatar_extension(filename: str | None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_AGENT_AVATAR_EXTENSIONS else None


def user_agent_avatar_dir(username: str) -> str:
    safe = re.sub(r"[^\w\-.]", "_", username)[:80]
    return os.path.join(settings.USER_AGENT_AVATAR_ROOT, f"user_{safe}")


async def save_uploaded_agent_avatar(username: str, file: UploadFile) -> tuple[str | None, str | None]:
    """返回 (filename, error_msg)。"""
    ext = safe_agent_avatar_extension(file.filename)
    if not ext:
        return None, f"仅支持以下格式：{', '.join(sorted(ALLOWED_AGENT_AVATAR_EXTENSIONS))}"
    contents = await file.read()
    max_bytes = 2 * 1024 * 1024
    if len(contents) > max_bytes:
        return None, "文件大小不能超过 2MB"
    new_name = f"{uuid.uuid4().hex}{ext}"
    root = user_agent_avatar_dir(username)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, new_name)
    with open(path, "wb") as f:
        f.write(contents)
    return new_name, None


def remove_agent_avatar_file(username: str, avatar_filename: str | None) -> None:
    if not avatar_filename:
        return
    path = os.path.join(user_agent_avatar_dir(username), avatar_filename)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
