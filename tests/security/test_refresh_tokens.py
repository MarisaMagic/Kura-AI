"""Refresh jti 轮换：consume 后不可再用。"""

from __future__ import annotations

import unittest

from app.chat.cache import RedisCache
from app.utils import refresh_tokens as rt


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets: dict[str, set] = {}

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        self.sets.pop(key, None)
        return 1

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return 1

    def srem(self, key, *members):
        s = self.sets.get(key)
        if not s:
            return 0
        n = 0
        for m in members:
            if m in s:
                s.discard(m)
                n += 1
        return n

    def smembers(self, key):
        return set(self.sets.get(key) or set())

    def expire(self, *args, **kwargs):
        return True


class RefreshTokenTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeRedis()
        rt.cache._client = self.fake  # type: ignore[attr-defined]
        RedisCache._client = None  # noqa: not used

    def test_issue_and_consume_once(self):
        jti = rt.issue_refresh_token(7, 1)
        rec = rt.consume_refresh_token(jti)
        self.assertEqual(rec["user_id"], 7)
        self.assertEqual(rec["tv"], 1)
        self.assertIsNone(rt.consume_refresh_token(jti))

    def test_revoke_user_clears_jti(self):
        jti = rt.issue_refresh_token(8, 0)
        rt.revoke_user_refresh_tokens(8)
        self.assertIsNone(rt.consume_refresh_token(jti))


if __name__ == "__main__":
    unittest.main()
