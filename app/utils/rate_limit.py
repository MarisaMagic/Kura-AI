"""通用用户维度限流（Redis 计数窗口），用于聊天等资源敏感接口。"""

from __future__ import annotations

from fastapi import HTTPException

from app.chat.cache import get_redis_client
from app.log import logger
from app.settings import settings

# 原子限流：INCR + 首次设置过期在同一 Lua 中完成，避免 INCR/EXPIRE 之间崩溃
# 留下无 TTL 的 key 导致用户被永久限流。
_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


def check_user_rate_limit(user_id: int, *, action: str, limit: int, window_seconds: int) -> None:
    """按用户 ID 计数限流，超限抛 429。

    Redis 不可用时放行并告警：限流是滥用防护而非认证边界，不阻断主流程。
    """
    if limit <= 0:
        return
    key = f"{settings.REDIS_KEY_PREFIX}:user_rate:{action}:{int(user_id)}"
    try:
        client = get_redis_client()
        count = int(client.eval(_RATE_LIMIT_LUA, 1, key, int(window_seconds)))
        if count > limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("user rate limit skipped (redis unavailable): %s", exc)


async def acheck_user_rate_limit(user_id: int, *, action: str, limit: int, window_seconds: int) -> None:
    """限流的异步入口（阶段 4）：直接走异步 Redis 客户端，不再占用默认线程池。"""
    if limit <= 0:
        return
    from app.chat.cache import cache

    key = f"{settings.REDIS_KEY_PREFIX}:user_rate:{action}:{int(user_id)}"
    try:
        count = int(await cache.aeval(_RATE_LIMIT_LUA, 1, key, int(window_seconds)))
        if count > limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("user rate limit skipped (redis unavailable): %s", exc)
