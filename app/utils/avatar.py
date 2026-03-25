from __future__ import annotations

from pathlib import Path

from app.settings import settings

DEFAULT_AVATAR_URL = "https://avatars.githubusercontent.com/u/54677442?v=4"
ALLOWED_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def avatar_url_from_filename(filename: str | None) -> str:
    if not filename:
        return DEFAULT_AVATAR_URL
    prefix = settings.USER_AVATAR_URL_PREFIX.rstrip("/")
    return f"{prefix}/{filename}"


def enrich_user_avatar(data: dict) -> dict:
    raw = data.get("avatar")
    data["avatar"] = avatar_url_from_filename(raw if raw else None)
    return data


def safe_avatar_extension(filename: str | None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_AVATAR_EXTENSIONS else None
