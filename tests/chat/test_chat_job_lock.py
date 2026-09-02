"""会话 Job 锁：SET NX 互斥，并发创建只抢到一把锁。"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.chat.cache import RedisCache


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)
        return 1

    def eval(self, *args, **kwargs):
        return 0

    def expire(self, *args, **kwargs):
        return True

    def rpush(self, *args, **kwargs):
        return 1

    def lrange(self, *args, **kwargs):
        return []

    def llen(self, *args, **kwargs):
        return 0


class JobLockTests(unittest.TestCase):
    def test_set_nx_second_fails(self):
        fake = _FakeRedis()
        cache = RedisCache()
        cache._client = fake  # type: ignore[attr-defined]
        self.assertTrue(cache.set_nx("ak", {"job_id": "a"}, ttl=60))
        self.assertFalse(cache.set_nx("ak", {"job_id": "b"}, ttl=60))
        self.assertEqual(cache.get_json("ak")["job_id"], "a")

    def test_create_chat_job_concurrent_one_winner(self):
        from app.chat import chat_job

        fake = _FakeRedis()
        chat_job.cache._client = fake  # type: ignore[attr-defined]

        async def _run():
            with mock.patch.object(chat_job.asyncio, "create_task", lambda coro: coro.close() or mock.Mock()):
                r1, r2 = await asyncio.gather(
                    chat_job.create_chat_job(
                        user_id=1,
                        agent_id=1,
                        session_id="s1",
                        message="hi",
                        use_knowledge_retrieval=False,
                    ),
                    chat_job.create_chat_job(
                        user_id=1,
                        agent_id=1,
                        session_id="s1",
                        message="hi2",
                        use_knowledge_retrieval=False,
                    ),
                )
            return r1, r2

        a, b = asyncio.run(_run())
        ids = {a[0], b[0]}
        reused = [a[1], b[1]]
        self.assertEqual(len(ids), 1)
        self.assertEqual(sorted(reused), [False, True])


if __name__ == "__main__":
    unittest.main()
