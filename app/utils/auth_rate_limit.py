"""登录 / 注册接口限流（Redis 滑动窗口计数）。"""

from __future__ import annotations

import redis
from fastapi import HTTPException, Request

from app.log import logger
from app.settings import settings


def client_ip(request: Request) -> str:
    if getattr(settings, "AUTH_TRUST_X_FORWARDED_FOR", False):
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip.split(",")[0].strip()
        forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
        if forwarded:
            return forwarded.split(",")[-1].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def check_auth_rate_limit(request: Request, *, action: str, limit: int, window_seconds: int) -> None:
    if not settings.AUTH_RATE_LIMIT_ENABLED:
        return
    ip = client_ip(request)
    key = f"{settings.REDIS_KEY_PREFIX}:auth_rate:{action}:{ip}"
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        count = client.incr(key)
        if count == 1:
            client.expire(key, window_seconds)
        if count > limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    except HTTPException:
        raise
    except Exception as exc:
        if settings.DEBUG:
            logger.warning("auth rate limit skipped (redis unavailable): %s", exc)
            return
        logger.warning("auth rate limit fail-closed (redis unavailable): %s", exc)
        raise HTTPException(status_code=503, detail="认证服务暂时不可用") from exc
