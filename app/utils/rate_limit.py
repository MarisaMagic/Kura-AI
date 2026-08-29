"""通用用户维度限流（Redis 计数窗口），用于聊天等资源敏感接口。"""

from __future__ import annotations

import redis
from fastapi import HTTPException

from app.log import logger
from app.settings import settings


def check_user_rate_limit(user_id: int, *, action: str, limit: int, window_seconds: int) -> None:
    """按用户 ID 计数限流，超限抛 429。

    Redis 不可用时放行并告警：限流是滥用防护而非认证边界，不阻断主流程。
    """
    if limit <= 0:
        return
    key = f"{settings.REDIS_KEY_PREFIX}:user_rate:{action}:{int(user_id)}"
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
        logger.warning("user rate limit skipped (redis unavailable): %s", exc)
