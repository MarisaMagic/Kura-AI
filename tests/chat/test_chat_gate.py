"""并发闸门：排队事件、等待超时置失败。不依赖真实 Redis/上游模型。"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from tests.chat.test_sse_events import _FakeRedis


def _events(fake: _FakeRedis, job_id: str) -> list[dict]:
    from app.chat import chat_job

    key = f"kura_ai:{chat_job._events_key(job_id)}"
    return [json.loads(w)["data"] for w in fake.lists.get(key, [])]


def _meta(fake: _FakeRedis, job_id: str) -> dict:
    from app.chat import chat_job

    key = f"kura_ai:{chat_job._meta_key(job_id)}"
    return json.loads(fake.store[key])


async def _fake_done_stream(*_a, **_k):
    yield {"type": "done", "cancelled": False}


class GateTests(unittest.TestCase):
    def setUp(self):
        from app.chat import chat_job

        self.fake = _FakeRedis()
        chat_job.cache._client = self.fake  # type: ignore[attr-defined]
        chat_job._llm_inflight_sem = None
        chat_job._llm_inflight_waiting = 0

    def tearDown(self):
        from app.chat import chat_job

        chat_job._llm_inflight_sem = None
        chat_job._llm_inflight_waiting = 0

    def _patches(self, stream):
        from app.chat import chat_job

        return [
            mock.patch.object(
                chat_job.user_agent_controller,
                "get_accessible",
                new=mock.AsyncMock(return_value=object()),
            ),
            mock.patch.object(chat_job, "iter_chat_stream_events", stream),
            mock.patch(
                "app.controllers.user_agent_recent.touch_recent_agent",
                new=mock.AsyncMock(),
            ),
        ]

    def test_gate_free_no_queued_event(self):
        from app.chat import chat_job

        async def _scenario():
            patches = self._patches(_fake_done_stream)
            for p in patches:
                p.start()
            try:
                await chat_job._run_chat_job(
                    job_id="jf",
                    user_id=1,
                    agent_id=2,
                    session_id="s1",
                    message="hi",
                    use_knowledge_retrieval=False,
                )
            finally:
                for p in patches:
                    p.stop()

        asyncio.run(_scenario())
        types = [e["type"] for e in _events(self.fake, "jf")]
        self.assertNotIn("queued", types)
        self.assertEqual(_meta(self.fake, "jf")["status"], "completed")

    def test_gate_full_emits_queued_then_completes(self):
        from app.chat import chat_job

        async def _scenario():
            sem = asyncio.Semaphore(1)
            await sem.acquire()  # 占满唯一槽位
            chat_job._llm_inflight_sem = sem

            async def _release_soon():
                await asyncio.sleep(0.05)
                sem.release()

            patches = self._patches(_fake_done_stream)
            for p in patches:
                p.start()
            releaser = asyncio.create_task(_release_soon())
            try:
                await chat_job._run_chat_job(
                    job_id="jq",
                    user_id=1,
                    agent_id=2,
                    session_id="s1",
                    message="hi",
                    use_knowledge_retrieval=False,
                )
            finally:
                await releaser
                for p in patches:
                    p.stop()

        asyncio.run(_scenario())
        events = _events(self.fake, "jq")
        self.assertEqual(events[0]["type"], "queued")
        self.assertEqual(events[0]["waiting"], 1)
        self.assertEqual(_meta(self.fake, "jq")["status"], "completed")

    def test_queue_timeout_fails_job_without_generation(self):
        from app.chat import chat_job

        async def _blocked_stream(*_a, **_k):
            raise AssertionError("排队超时后不应进入生成流程")
            yield  # pragma: no cover

        async def _scenario():
            sem = asyncio.Semaphore(1)
            await sem.acquire()  # 永不释放 → 必然排队超时
            chat_job._llm_inflight_sem = sem

            patches = self._patches(_blocked_stream)
            patches.append(mock.patch.object(chat_job, "_queue_timeout", return_value=0.05))
            for p in patches:
                p.start()
            try:
                await chat_job._run_chat_job(
                    job_id="jt",
                    user_id=1,
                    agent_id=2,
                    session_id="s1",
                    message="hi",
                    use_knowledge_retrieval=False,
                )
            finally:
                for p in patches:
                    p.stop()

        asyncio.run(_scenario())
        events = _events(self.fake, "jt")
        self.assertEqual(events[0]["type"], "queued")
        self.assertEqual(events[1]["type"], "error")
        meta = _meta(self.fake, "jt")
        self.assertEqual(meta["status"], "failed")
        self.assertIn("排队等待超时", meta["error"])


if __name__ == "__main__":
    unittest.main()
