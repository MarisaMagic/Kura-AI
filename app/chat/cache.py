"""Redis 缓存（会话消息与会话列表）。"""

from __future__ import annotations

import json
from typing import Any, Optional

import redis

from app.settings import settings


class RedisCache:
    def __init__(self) -> None:
        self.redis_url = settings.REDIS_URL
        self.key_prefix = settings.REDIS_KEY_PREFIX
        self.default_ttl = settings.REDIS_CACHE_TTL_SECONDS
        self._client: redis.Redis | None = None

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    def get_json(self, key: str) -> Optional[Any]:
        try:
            value = self._get_client().get(self._key(key))
            if not value:
                return None
            return json.loads(value)
        except Exception:
            return None

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False)
            self._get_client().setex(self._key(key), ttl or self.default_ttl, payload)
        except Exception:
            return

    def delete(self, key: str) -> None:
        try:
            self._get_client().delete(self._key(key))
        except Exception:
            return


cache = RedisCache()
