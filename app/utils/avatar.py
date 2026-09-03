from __future__ import annotations

from pathlib import Path

from app.utils.signed_media import KIND_USER_AVATAR, sign_media_url

ALLOWED_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def avatar_url_from_filename(filename: str | None) -> str:
    name = (filename or "").strip() or "alice.jpg"
    return sign_media_url(KIND_USER_AVATAR, name)


def enrich_user_avatar(data: dict) -> dict:
    raw = data.get("avatar")
    data["avatar"] = avatar_url_from_filename(raw if raw else None)
    return data


def safe_avatar_extension(filename: str | None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_AVATAR_EXTENSIONS else None
