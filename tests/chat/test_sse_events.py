"""SSE 事件层：Lua 原子追加（1 次往返）、批量追更、终态短 TTL。不依赖真实 Redis。"""

from __future__ import annotations

import asyncio
import json
import unittest

from app.chat.cache import RedisCache


class _FakeRedis:
    """最小 Redis 桩：覆盖 append_event_atomic 的 eval 语义与列表/串键操作。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = int(ttl)
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        return 1

    def expire(self, key, seconds):
        self.ttls[key] = int(seconds)
        return True

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        if end < 0:
            end = len(items) + end
        return items[start : end + 1]

    def llen(self, key):
        return len(self.lists.get(key, []))

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "RPUSH" in script:
            self.lists.setdefault(keys[0], []).append(argv[0])
            self.ttls[keys[0]] = int(argv[1])
            self.ttls[keys[1]] = int(argv[1])
            return 1
        return 0


class AppendEventAtomicTests(unittest.TestCase):
    def test_single_eval_appends_and_refreshes_both_ttls(self):
        fake = _FakeRedis()
        cache = RedisCache()
        cache._client = fake  # type: ignore[attr-defined]

        ok = cache.append_event_atomic("ev", "meta", json.dumps({"seq": 0}), 1234)
        self.assertTrue(ok)
        self.assertEqual(len(fake.lists["kura_ai:ev"]), 1)
        self.assertEqual(fake.ttls["kura_ai:ev"], 1234)
        self.assertEqual(fake.ttls["kura_ai:meta"], 1234)

    def test_append_event_uses_atomic_path(self):
        from app.chat import chat_job

        fake = _FakeRedis()
        chat_job.cache._client = fake  # type: ignore[attr-defined]
        running_ttl = chat_job._ttl()

        asyncio.run(chat_job._append_event("j1", 0, {"type": "delta"}))
        events_key = f"kura_ai:{chat_job._events_key('j1')}"
        self.assertEqual(len(fake.lists[events_key]), 1)
        self.assertEqual(fake.ttls[events_key], running_ttl)
        self.assertEqual(json.loads(fake.lists[events_key][0])["seq"], 0)


class IterEventsTests(unittest.TestCase):
    def _seed(self, fake, job_id: str, events: list[dict], status: str) -> None:
        from app.chat import chat_job

        events_key = f"kura_ai:{chat_job._events_key(job_id)}"
        for idx, ev in enumerate(events):
            fake.lists.setdefault(events_key, []).append(
                json.dumps({"seq": idx, "data": ev}, ensure_ascii=False)
            )
        fake.store[f"kura_ai:{chat_job._meta_key(job_id)}"] = json.dumps({"status": status})

    def test_since_seq_catch_up_yields_tail_then_done(self):
        from app.chat import chat_job

        fake = _FakeRedis()
        chat_job.cache._client = fake  # type: ignore[attr-defined]
        self._seed(
            fake,
            "j2",
            [{"type": "delta", "i": i} for i in range(5)],
            status="completed",
        )

        async def _collect():
            return [line async for line in chat_job.iter_job_sse_events("j2", since_seq=2)]

        lines = asyncio.run(_collect())
        self.assertEqual(len(lines), 4)  # seq 2/3/4 + DONE
        self.assertTrue(lines[-1].startswith("data: [DONE]"))
        self.assertEqual(json.loads(lines[0][len("data: ") :])["i"], 2)

    def test_missing_meta_ends_immediately(self):
        from app.chat import chat_job

        fake = _FakeRedis()
        chat_job.cache._client = fake  # type: ignore[attr-defined]

        async def _collect():
            return [line async for line in chat_job.iter_job_sse_events("ghost", since_seq=0)]

        lines = asyncio.run(_collect())
        self.assertEqual(lines, ["data: [DONE]\n\n"])

    def test_drains_events_before_terminal_break(self):
        """终态判定发生在空批之后：已有事件必须先全部吐出，不得因 meta 已终态而丢。"""
        from app.chat import chat_job

        fake = _FakeRedis()
        chat_job.cache._client = fake  # type: ignore[attr-defined]
        self._seed(fake, "j3", [{"type": "done"}], status="completed")

        async def _collect():
            return [line async for line in chat_job.iter_job_sse_events("j3", since_seq=0)]

        lines = asyncio.run(_collect())
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0][len("data: ") :])["type"], "done")


class FinishMetaTtlTests(unittest.TestCase):
    def test_terminal_state_uses_done_ttl(self):
        from app.chat import chat_job
        from app.settings import settings

        fake = _FakeRedis()
        chat_job.cache._client = fake  # type: ignore[attr-defined]
        events_key = f"kura_ai:{chat_job._events_key('j9')}"
        fake.lists.setdefault(events_key, []).append("x")

        asyncio.run(chat_job._finish_meta("j9", status="completed", error=None))

        done_ttl = int(getattr(settings, "CHAT_JOB_DONE_TTL_SECONDS", 3600) or 3600)
        meta_key = f"kura_ai:{chat_job._meta_key('j9')}"
        self.assertEqual(fake.ttls[meta_key], done_ttl)
        self.assertEqual(fake.ttls[events_key], done_ttl)
        meta = json.loads(fake.store[meta_key])
        self.assertEqual(meta["status"], "completed")
        self.assertLess(done_ttl, chat_job._ttl())  # 终态 TTL 必须短于 running 期


if __name__ == "__main__":
    unittest.main()
