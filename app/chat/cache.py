"""
Redis 缓存（会话消息与会话列表）。

双轨设计（阶段 4 深度异步化）：
- 同步方法：worker/线程池内使用（kb_job/report 等）；
- ``a*`` 异步方法：事件循环内使用（对话热路径），底层为 ``redis.asyncio`` 客户端；
  经 ``_acall`` 桥接，测试注入的同步 fake 也可直接复用（自动识别 awaitable）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from typing import Any, Optional

import redis
import redis.asyncio as redis_async
from loguru import logger

from app.settings import settings


class RedisCache:
    def __init__(self) -> None:
        self.redis_url = settings.REDIS_URL
        self.key_prefix = settings.REDIS_KEY_PREFIX
        self.default_ttl = settings.REDIS_CACHE_TTL_SECONDS
        self._client: redis.Redis | None = None
        # 异步客户端（redis.asyncio 连接池绑定事件循环，跨 loop 自动重建）
        self._aclient: Any = None
        self._aclient_loop: Any = None
        self._aclient_lock = threading.Lock()

    def _get_client(self) -> redis.Redis:
        """
        使用懒加载模式创建 Redis 客户端
        提高性能，避免每次都创建 Redis 客户端
        """
        if self._client is None:
            self._client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                max_connections=int(getattr(settings, "REDIS_MAX_CONNECTIONS", 64) or 64),
                socket_timeout=10,
                socket_connect_timeout=3,
                health_check_interval=30,
            )
        return self._client

    def _get_aclient(self):
        """异步客户端懒加载；跨事件循环（测试多 loop）自动重建连接池。"""
        loop = asyncio.get_running_loop()
        with self._aclient_lock:
            if self._aclient is None or self._aclient_loop is not loop:
                self._aclient = redis_async.Redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    max_connections=int(getattr(settings, "REDIS_MAX_CONNECTIONS", 64) or 64),
                    socket_timeout=10,
                    socket_connect_timeout=3,
                    health_check_interval=30,
                )
                self._aclient_loop = loop
            return self._aclient

    async def _acall(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """
        执行异步 Redis 命令。

        优先使用已注入的客户端（真实 redis.asyncio 返回 awaitable；测试注入的同步
        fake 直接返回结果，自动识别）；否则懒建真实异步客户端。
        跨事件循环时重建真实客户端（测试多 loop 场景）。
        """
        if self._aclient is not None and self._aclient_loop is not None:
            try:
                if self._aclient_loop is not asyncio.get_running_loop():
                    self._aclient = None
                    self._aclient_loop = None
            except RuntimeError:
                pass
        client = self._aclient if self._aclient is not None else self._get_aclient()
        result = getattr(client, method_name)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

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

    def mget_json(self, keys: list[str]) -> dict[str, Any]:
        """
        一次 MGET 批量读取 JSON（批任务状态轮询场景，避免 N 次往返打满事件循环）。
        :param keys: 不含前缀的 key 列表
        :return: {key: 反序列化对象}，缺失/损坏的 key 不返回
        """
        if not keys:
            return {}
        try:
            values = self._get_client().mget([self._key(k) for k in keys])
        except Exception as e:
            logger.warning("Redis mget_json 失败 keys={}: {}", len(keys), e)
            return {}
        out: dict[str, Any] = {}
        for key, raw in zip(keys, values):
            if not raw:
                continue
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                continue
        return out

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        将 Python 对象序列化转换为 JSON 字符串，并存储到 Redis 中
        设置过期时间 TTL，避免数据长时间存储在 Redis 中
        :return: 写入成功返回 True（用于调用方判断是否「已受理」）
        """
        try:
            # default=str：datetime 等非原生类型转字符串（父块缓存快照含 updated_at）
            payload = json.dumps(value, ensure_ascii=False, default=str)
            self._get_client().setex(self._key(key), ttl or self.default_ttl, payload)
            return True
        except Exception as e:
            logger.warning("Redis set_json 失败 key={}: {}", key, e)
            return False

    def set_json_many(self, items: dict[str, Any], ttl: Optional[int] = None) -> bool:
        """一次 pipeline 批量写入多个 JSON key（父块缓存等多 key 写场景，避免 N 次往返）。"""
        if not items:
            return True
        try:
            pipe = self._get_client().pipeline(transaction=False)
            for key, value in items.items():
                payload = json.dumps(value, ensure_ascii=False, default=str)
                if ttl:
                    pipe.setex(self._key(key), int(ttl), payload)
                else:
                    pipe.setex(self._key(key), self.default_ttl, payload)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("Redis set_json_many 失败 keys={}: {}", len(items), e)
            return False

    def set_nx(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        SET NX：仅当键不存在时写入并返回 True，用于互斥标记（会话 Job 锁、知识库同名文档替换锁）。
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

    def delete_if_job_matches(self, key: str, job_id: str) -> bool:
        """
        原子 compare-and-delete：仅当 key 存储的 JSON 中 job_id 字段匹配时才删除。
        用于释放会话占用锁时避免旧任务误删新任务的锁。
        :return: 实际执行了删除返回 True
        """
        lua = """
        local v = redis.call('GET', KEYS[1])
        if not v then return 0 end
        local ok, decoded = pcall(cjson.decode, v)
        if ok and type(decoded) == 'table' and decoded['job_id'] == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            return bool(self._get_client().eval(lua, 1, self._key(key), job_id))
        except Exception as e:
            logger.warning("Redis delete_if_job_matches 失败 key={}: {}", key, e)
            return False

    # RPUSH + 双 EXPIRE（事件列表 + Job meta）合并为单次 Lua 往返，
    # 替代原先每条事件 3 次串行 Redis 命令的写放大
    _APPEND_EVENT_LUA = """
    redis.call('RPUSH', KEYS[1], ARGV[1])
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    redis.call('EXPIRE', KEYS[2], ARGV[2])
    return 1
    """

    def append_event_atomic(self, events_key: str, meta_key: str, payload_json: str, ttl: int) -> bool:
        """
        原子追加 Job 事件：一次往返完成事件入队与 meta/events 双 TTL 刷新。
        :param payload_json: 已序列化的 {"seq":..,"data":..} JSON 字符串
        :return: 写入成功返回 True
        """
        try:
            self._get_client().eval(
                self._APPEND_EVENT_LUA,
                2,
                self._key(events_key),
                self._key(meta_key),
                payload_json,
                str(int(ttl)),
            )
            return True
        except Exception as e:
            logger.warning("Redis append_event_atomic 失败 key={}: {}", events_key, e)
            return False

    def rpush_json(self, key: str, value: Any) -> int:
        """列表尾部追加 JSON 元素，返回列表长度。"""
        try:
            payload = json.dumps(value, ensure_ascii=False)
            return int(self._get_client().rpush(self._key(key), payload))
        except Exception:
            return 0

    # 原子「深度检查 + 入队」：多副本高并发下消除 check-then-act 超卖
    _ENQUEUE_LIMITED_LUA = """
    local depth = redis.call('LLEN', KEYS[1]) + redis.call('LLEN', KEYS[2])
    local maxd = tonumber(ARGV[2])
    if maxd > 0 and depth >= maxd then
      return 0
    end
    redis.call('RPUSH', KEYS[1], ARGV[1])
    return 1
    """

    def rpush_json_with_limit(self, key: str, other_key: str, value: Any, max_depth: int) -> bool:
        """
        原子入队：key 与 other_key 列表深度合计达到 max_depth 时拒绝（max_depth<=0 不限制）。
        :return: 入队成功 True；超限或 Redis 不可用 False
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
            result = self._get_client().eval(
                self._ENQUEUE_LIMITED_LUA,
                2,
                self._key(key),
                self._key(other_key),
                payload,
                str(int(max_depth)),
            )
            return int(result) == 1
        except Exception as e:
            logger.warning("Redis rpush_json_with_limit 失败 key={}: {}", key, e)
            return False

    def brpoplpush_json(self, src: str, dst: str, timeout: int = 5) -> Optional[Any]:
        """
        阻塞式可靠出队：从 src 弹出元素并原子写入 dst（处理中列表），超时返回 None。
        消费方处理完成后再 LREM dst —— 进程崩溃时元素仍留在 dst，可被回收重投。
        """
        try:
            raw = self._get_client().brpoplpush(self._key(src), self._key(dst), timeout)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("Redis brpoplpush_json 失败 src={}: {}", src, e)
            return None

    def lrem_json(self, key: str, value: Any, count: int = 0) -> int:
        """从列表中移除与 value 相等的 JSON 元素，返回移除数量。

        :param count: 0=全部移除；1=仅移除一个（多副本消费者 ack 场景，只删自己处理的那条）
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
            return int(self._get_client().lrem(self._key(key), int(count), payload))
        except Exception:
            return 0

    def lrem_raw(self, key: str, raw: str, count: int = 0) -> int:
        """按原始字符串移除列表元素（回收损坏/无法反序列化的条目）。"""
        try:
            return int(self._get_client().lrem(self._key(key), int(count), raw))
        except Exception:
            return 0

    def sadd_json(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set 追加元素并刷新 TTL（用户活动任务集合等）。"""
        try:
            client = self._get_client()
            client.sadd(self._key(key), json.dumps(value, ensure_ascii=False))
            if ttl:
                client.expire(self._key(key), ttl)
            return True
        except Exception as e:
            logger.warning("Redis sadd_json 失败 key={}: {}", key, e)
            return False

    def srem_json(self, key: str, value: Any) -> None:
        try:
            self._get_client().srem(self._key(key), json.dumps(value, ensure_ascii=False))
        except Exception:
            pass

    def scard(self, key: str) -> int:
        try:
            return int(self._get_client().scard(self._key(key)))
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

    # ---------------------------------------------------------------- 异步方法（事件循环内用）

    async def aget_json(self, key: str) -> Optional[Any]:
        """异步读取 JSON 缓存（异常时返回 None，与同步版语义一致）。"""
        try:
            value = await self._acall("get", self._key(key))
            if not value:
                return None
            return json.loads(value)
        except Exception as e:
            logger.warning("Redis aget_json 失败 key={}: {}", key, e)
            return None

    async def aset_json(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """异步写入 JSON 缓存；返回是否成功（用于「已受理」类判断）。"""
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            await self._acall("setex", self._key(key), ttl or self.default_ttl, payload)
            return True
        except Exception as e:
            logger.warning("Redis aset_json 失败 key={}: {}", key, e)
            return False

    async def aset_nx(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """异步 SET NX（互斥标记）；Redis 不可用返回 False。"""
        try:
            payload = json.dumps(value, ensure_ascii=False)
            return bool(
                await self._acall("set", self._key(key), payload, nx=True, ex=ttl or self.default_ttl)
            )
        except Exception as e:
            logger.warning("Redis aset_nx 失败 key={}: {}", key, e)
            return False

    async def adelete(self, key: str) -> None:
        try:
            await self._acall("delete", self._key(key))
        except Exception as e:
            logger.warning("Redis adelete 失败 key={}: {}", key, e)

    async def adelete_if_job_matches(self, key: str, job_id: str) -> bool:
        """异步 compare-and-delete：仅当 key 中 job_id 匹配时才删除。"""
        lua = """
        local v = redis.call('GET', KEYS[1])
        if not v then return 0 end
        local ok, decoded = pcall(cjson.decode, v)
        if ok and type(decoded) == 'table' and decoded['job_id'] == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            return bool(await self._acall("eval", lua, 1, self._key(key), job_id))
        except Exception as e:
            logger.warning("Redis adelete_if_job_matches 失败 key={}: {}", key, e)
            return False

    async def aappend_event_atomic(
        self, events_key: str, meta_key: str, payload_json: str, ttl: int
    ) -> bool:
        """异步原子追加 Job 事件（单次 Lua 往返：RPUSH + 双 TTL）。"""
        try:
            await self._acall(
                "eval",
                self._APPEND_EVENT_LUA,
                2,
                self._key(events_key),
                self._key(meta_key),
                payload_json,
                str(int(ttl)),
            )
            return True
        except Exception as e:
            logger.warning("Redis aappend_event_atomic 失败 key={}: {}", events_key, e)
            return False

    async def aexpire(self, key: str, seconds: int) -> None:
        try:
            await self._acall("expire", self._key(key), int(seconds))
        except Exception:
            pass

    async def alrange_str(self, key: str, start: int, end: int) -> list[str]:
        try:
            raw = await self._acall("lrange", self._key(key), start, end)
            return list(raw) if raw else []
        except Exception:
            return []

    async def aeval(self, script: str, numkeys: int, *args: Any) -> Any:
        """异步执行 Lua 脚本（限流等场景；调用方自行处理异常语义）。"""
        return await self._acall("eval", script, numkeys, *args)


cache = RedisCache()


def get_redis_client() -> redis.Redis:
    """
    共享 Redis 客户端（限流等模块直接使用；key 前缀由调用方自行处理）。
    复用 cache 单例连接池，避免每请求新建连接。
    """
    return cache._get_client()
