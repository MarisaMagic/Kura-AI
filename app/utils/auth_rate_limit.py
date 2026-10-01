"""登录 / 注册接口限流（Redis 滑动窗口计数）。"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.chat.cache import get_redis_client
from app.log import logger
from app.settings import settings

# 原子限流：INCR + 首次设置过期在同一 Lua 中完成（与 user_rate_limit 相同策略）
_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


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
        client = get_redis_client()
        count = int(client.eval(_RATE_LIMIT_LUA, 1, key, int(window_seconds)))
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


async def acheck_auth_rate_limit(request: Request, *, action: str, limit: int, window_seconds: int) -> None:
    """限流的异步入口（阶段 4）：直接走异步 Redis 客户端；fail-closed 语义与同步版一致。"""
    if not settings.AUTH_RATE_LIMIT_ENABLED:
        return
    from app.chat.cache import cache

    ip = client_ip(request)
    key = f"{settings.REDIS_KEY_PREFIX}:auth_rate:{action}:{ip}"
    try:
        count = int(await cache.aeval(_RATE_LIMIT_LUA, 1, key, int(window_seconds)))
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
