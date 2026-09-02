"""Refresh token：HttpOnly cookie + Redis jti，支持轮换与按用户吊销。"""

from __future__ import annotations

import uuid

from fastapi import Response

from app.chat.cache import cache
from app.settings import settings

COOKIE_NAME = "kura_refresh"


def _ttl() -> int:
    days = max(1, int(getattr(settings, "JWT_REFRESH_TOKEN_EXPIRE_DAYS", 7) or 7))
    return days * 86400


def _cookie_name() -> str:
    return (getattr(settings, "JWT_REFRESH_COOKIE_NAME", None) or COOKIE_NAME).strip() or COOKIE_NAME


def _jti_key(jti: str) -> str:
    return f"refresh:{jti}"


def _user_set_key(user_id: int) -> str:
    return f"refresh_jtis:{int(user_id)}"


def issue_refresh_token(user_id: int, token_version: int) -> str:
    jti = uuid.uuid4().hex
    ttl = _ttl()
    cache.set_json(_jti_key(jti), {"user_id": int(user_id), "tv": int(token_version or 0)}, ttl)
    try:
        client = cache._get_client()
        skey = cache._key(_user_set_key(user_id))
        client.sadd(skey, jti)
        client.expire(skey, ttl)
    except Exception:
        pass
    return jti


def consume_refresh_token(jti: str) -> dict | None:
    if not (jti or "").strip():
        return None
    rec = cache.get_json(_jti_key(jti))
    cache.delete(_jti_key(jti))
    if isinstance(rec, dict) and rec.get("user_id") is not None:
        try:
            cache._get_client().srem(cache._key(_user_set_key(int(rec["user_id"]))), jti)
        except Exception:
            pass
        return rec
    return None


def revoke_user_refresh_tokens(user_id: int) -> None:
    try:
        client = cache._get_client()
        skey = cache._key(_user_set_key(user_id))
        members = client.smembers(skey) or set()
        for jti in members:
            cache.delete(_jti_key(str(jti)))
        client.delete(skey)
    except Exception:
        pass


def set_refresh_cookie(response: Response, jti: str) -> None:
    response.set_cookie(
        key=_cookie_name(),
        value=jti,
        httponly=True,
        secure=bool(getattr(settings, "AUTH_COOKIE_SECURE", False)),
        samesite=(getattr(settings, "AUTH_COOKIE_SAMESITE", None) or "lax"),
        path="/api/v1/base",
        max_age=_ttl(),
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_cookie_name(), path="/api/v1/base")
