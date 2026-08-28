"""
Redis 缓存（会话消息与会话列表）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import redis
from loguru import logger

from app.settings import settings


class RedisCache:
    def __init__(self) -> None:
        self.redis_url = settings.REDIS_URL
        self.key_prefix = settings.REDIS_KEY_PREFIX
        self.default_ttl = settings.REDIS_CACHE_TTL_SECONDS
        self._client: redis.Redis | None = None

    def _get_client(self) -> redis.Redis:
        """
        使用懒加载模式创建 Redis 客户端
        提高性能，避免每次都创建 Redis 客户端
        """
        if self._client is None:
            self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def _key(self, key: str) -> str:
        """
        使用前缀拼接 key, 避免 key 冲突
        """
        return f"{self.key_prefix}:{key}"

    def get_json(self, key: str) -> Optional[Any]:
        """
        从 Redis 中获取 JSON 数据
        自动将 JSON 字符串反序列化转换为 Python 对象
        """
        try:
            value = self._get_client().get(self._key(key))
            if not value:
                return None
            return json.loads(value)
        except Exception as e:
            logger.warning("Redis get_json 失败 key={}: {}", key, e)
            return None

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        将 Python 对象序列化转换为 JSON 字符串，并存储到 Redis 中
        设置过期时间 TTL，避免数据长时间存储在 Redis 中
        :return: 写入成功返回 True（用于调用方判断是否「已受理」）
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
            self._get_client().setex(self._key(key), ttl or self.default_ttl, payload)
            return True
        except Exception as e:
            logger.warning("Redis set_json 失败 key={}: {}", key, e)
            return False

    def set_nx(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        SET NX：仅当键不存在时写入并返回 True，用于互斥标记（如知识库同名文档替换锁）。
        Redis 不可用时返回 False，调用方按「未能抢占」处理。
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
            return bool(self._get_client().set(self._key(key), payload, nx=True, ex=ttl or self.default_ttl))
        except Exception as e:
            logger.warning("Redis set_nx 失败 key={}: {}", key, e)
            return False

    def delete(self, key: str) -> None:
        """
        删除 Redis 缓存中的数据
        """
        try:
            self._get_client().delete(self._key(key))
        except Exception as e:
            logger.warning("Redis delete 失败 key={}: {}", key, e)
            return

    def rpush_json(self, key: str, value: Any) -> int:
        """列表尾部追加 JSON 元素，返回列表长度。"""
        try:
            payload = json.dumps(value, ensure_ascii=False)
            return int(self._get_client().rpush(self._key(key), payload))
        except Exception:
            return 0

    def lrange_str(self, key: str, start: int, end: int) -> list[str]:
        """按索引范围读取列表元素（字符串）。"""
        try:
            raw = self._get_client().lrange(self._key(key), start, end)
            return list(raw) if raw else []
        except Exception:
            return []

    def llen(self, key: str) -> int:
        try:
            return int(self._get_client().llen(self._key(key)))
        except Exception:
            return 0

    def expire(self, key: str, seconds: int) -> None:
        try:
            self._get_client().expire(self._key(key), seconds)
        except Exception:
            pass


cache = RedisCache()
