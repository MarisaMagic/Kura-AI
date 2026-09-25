"""
对话收尾任务单测：Redis 互斥锁、执行顺序依赖、队列载荷不含密钥、失败隔离。
Redis 用内存桩替代，不依赖真实服务 / PostgreSQL / LLM。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.chat import post_turn_job as ptj
from app.settings import settings


class _MemoryCache:
    """内存版 Redis 桩：覆盖 post_turn_job 用到的子集。"""

    def __init__(self):
        self._d: dict = {}
        self.lists: dict[str, list] = {}

    def get_json(self, key):
        return self._d.get(key)

    def set_json(self, key, value, ttl=None):
        self._d[key] = value
        return True

    def set_nx(self, key, value, ttl=None):
        if key in self._d:
            return False
        self._d[key] = value
        return True

    def delete(self, key):
        self._d.pop(key, None)

    def delete_if_job_matches(self, key, job_id):
        v = self._d.get(key)
        if isinstance(v, dict) and v.get("job_id") == job_id:
            self._d.pop(key, None)
            return True
        return False

    def rpush_json(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange_str(self, key, start, end):
        import json

        return [json.dumps(v, ensure_ascii=False) for v in self.lists.get(key, [])[start:]]

    def lrem_raw(self, key, raw):
        import json

        lst = self.lists.get(key, [])
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return 0
        if payload in lst:
            lst.remove(payload)
            return 1
        return 0

    def brpoplpush_json(self, src, dst, timeout=5):
        """阻塞出队的内存版：src 有元素就搬到 dst 并返回，否则返回 None。"""
        lst = self.lists.get(src) or []
        if not lst:
            return None
        item = lst.pop(0)
        self.lists.setdefault(dst, []).append(item)
        return item

    def lrem_json(self, key, value):
        lst = self.lists.get(key, [])
        if value in lst:
            lst.remove(value)
            return 1
        return 0


class LockTest(unittest.TestCase):
    def setUp(self):
        self.cache = _MemoryCache()
        p = mock.patch.object(ptj, "cache", self.cache)
        p.start()
        self.addCleanup(p.stop)
        self.calls: list[str] = []

    def _patch_steps(self):
        return [
            mock.patch(
                "app.chat.compact.record_usage_calibration",
                side_effect=lambda *a, **k: self.calls.append("calib"),
            ),
            mock.patch(
                "app.chat.compact.precompute_compaction",
                side_effect=lambda *a, **k: self.calls.append("precompact") or {"ok": True},
            ),
            mock.patch(
                "app.chat.memory_archive.archive_session_memory",
                side_effect=lambda *a, **k: self.calls.append("archive") or {},
            ),
        ]

    def test_lock_acquired_and_released(self):
        for p in self._patch_steps():
            p.start()
            self.addCleanup(p.stop)
        ptj.run_post_turn(1, 2, "s1", llm_config={"api_key": "k"}, estimated=100, input_tokens=120)
        self.assertEqual(self.cache._d, {})  # 锁已释放
        self.assertEqual(self.calls, ["calib", "precompact", "archive"])

    def test_second_run_skipped_while_locked(self):
        """多副本部署下同一会话只允许一个进程收尾，避免重复的 LLM 调用。"""
        self.cache.set_nx(ptj._lock_key(1, 2, "s1"), {"job_id": "别的进程"}, 120)
        for p in self._patch_steps():
            p.start()
            self.addCleanup(p.stop)
        info = ptj.run_post_turn(1, 2, "s1", llm_config={"api_key": "k"})
        self.assertFalse(info["lock"])
        self.assertEqual(self.calls, [])

    def test_lock_released_even_on_failure(self):
        with mock.patch(
            "app.chat.compact.record_usage_calibration", side_effect=RuntimeError("boom")
        ), mock.patch("app.chat.compact.precompute_compaction") as pc, mock.patch(
            "app.chat.memory_archive.archive_session_memory"
        ) as ar:
            ptj.run_post_turn(1, 2, "s1", llm_config={"api_key": "k"}, estimated=10, input_tokens=12)
        self.assertEqual(self.cache._d, {})  # 异常路径也必须释放锁
        pc.assert_not_called()
        ar.assert_not_called()

    def test_ordering_calibration_then_precompact_then_archive(self):
        """顺序有依赖：归档必须在预压缩之后，否则窗口边界是旧的。"""
        for p in self._patch_steps():
            p.start()
            self.addCleanup(p.stop)
        ptj.run_post_turn(1, 2, "s1", llm_config={"api_key": "k"}, estimated=100, input_tokens=120)
        self.assertEqual(self.calls.index("precompact"), self.calls.index("archive") - 1)

    def test_no_usage_skips_calibration(self):
        for p in self._patch_steps():
            p.start()
            self.addCleanup(p.stop)
        ptj.run_post_turn(1, 2, "s1", llm_config={"api_key": "k"}, estimated=0, input_tokens=0)
        self.assertNotIn("calib", self.calls)
        self.assertIn("precompact", self.calls)


class QueuePayloadTest(unittest.TestCase):
    def setUp(self):
        self.cache = _MemoryCache()
        p = mock.patch.object(ptj, "cache", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def test_queue_mode_enqueues_without_secrets(self):
        """队列载荷会落到 Redis，绝不能带 API Key 或提示词。"""
        with mock.patch.object(settings, "CHAT_MEMORY_TASK_MODE", "queue"):
            ptj.schedule_post_turn(
                1,
                2,
                "s1",
                llm_config={"api_key": "sk-超机密", "model_name": "m1"},
                system_prompt="人设提示词内容",
                tools_tokens=1234,
                usage={"input_tokens": 500},
                estimated=480,
                model_name="m1",
            )
        items = self.cache.lists[ptj._QUEUE_KEY]
        self.assertEqual(len(items), 1)
        payload = items[0]
        blob = str(payload)
        self.assertNotIn("sk-超机密", blob)
        self.assertNotIn("api_key", blob)
        self.assertNotIn("人设提示词内容", blob)
        self.assertNotIn("system_prompt", blob)
        # worker 侧重建所需的最小信息必须在
        self.assertEqual(payload["user_id"], 1)
        self.assertEqual(payload["agent_id"], 2)
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["tools_tokens"], 1234)
        self.assertEqual(payload["input_tokens"], 500)
        self.assertEqual(payload["estimated"], 480)

    def test_thread_mode_does_not_enqueue(self):
        with mock.patch.object(settings, "CHAT_MEMORY_TASK_MODE", "thread"), mock.patch.object(
            ptj, "run_post_turn"
        ) as run:
            ptj.schedule_post_turn(1, 2, "s1", llm_config={"api_key": "k"})
        self.assertEqual(self.cache.lists.get(ptj._QUEUE_KEY, []), [])
        run.assert_called_once()

    def test_disabled_session_memory_is_noop(self):
        with mock.patch.object(settings, "CHAT_USE_SESSION_MEMORY", False), mock.patch.object(
            ptj, "run_post_turn"
        ) as run:
            ptj.schedule_post_turn(1, 2, "s1", llm_config={"api_key": "k"})
        run.assert_not_called()

    def test_queue_overflow_drops_task(self):
        """收尾任务可安全重算，队列过长时丢弃而不是无限堆积。"""
        with mock.patch.object(ptj, "_QUEUE_MAX", 2):
            self.cache.rpush_json(ptj._QUEUE_KEY, {"n": 1})
            self.cache.rpush_json(ptj._QUEUE_KEY, {"n": 2})
            with mock.patch.object(settings, "CHAT_MEMORY_TASK_MODE", "queue"):
                ptj.schedule_post_turn(1, 2, "s1", llm_config={"api_key": "k"})
        self.assertEqual(len(self.cache.lists[ptj._QUEUE_KEY]), 2)

    def test_dequeue_and_ack(self):
        self.cache.rpush_json(ptj._QUEUE_KEY, {"kind": "chat_post_turn", "session_id": "s"})
        payload = ptj.dequeue_task(timeout=1)
        self.assertEqual(payload["session_id"], "s")
        # 出队后先进 processing 列表，ack 才真正移除（崩溃可回收重投）
        self.assertEqual(len(self.cache.lists[ptj._PROCESSING_KEY]), 1)
        self.assertEqual(self.cache.lists[ptj._QUEUE_KEY], [])
        ptj.ack_task(payload)
        self.assertEqual(self.cache.lists[ptj._PROCESSING_KEY], [])

    def test_dequeue_empty_returns_none(self):
        self.assertIsNone(ptj.dequeue_task(timeout=1))

    def test_recover_stale_requeues(self):
        self.cache.lists[ptj._PROCESSING_KEY] = [{"session_id": "崩了的"}, "坏数据"]
        n = ptj.recover_stale_processing()
        self.assertEqual(n, 1)
        self.assertEqual(len(self.cache.lists[ptj._PROCESSING_KEY]), 0)
        self.assertIn({"session_id": "崩了的"}, self.cache.lists[ptj._QUEUE_KEY])
        # 非 dict 的残留条目被丢弃而不是重新入队（否则 worker 会再失败一次）
        self.assertNotIn("坏数据", self.cache.lists[ptj._QUEUE_KEY])

    def test_bad_payload_rejected(self):
        self.assertEqual(ptj.run_task_payload({"user_id": 0})["reason"], "bad_payload")


class SessionLockTest(unittest.TestCase):
    def setUp(self):
        self.cache = _MemoryCache()
        p = mock.patch.object(ptj, "cache", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def test_lock_serializes_same_session(self):
        with ptj.session_lock(1, 2, "s1") as a1:
            self.assertTrue(a1)
            with ptj.session_lock(1, 2, "s1") as a2:
                self.assertFalse(a2)  # 同会话已被占用
        # 释放后可再次获取
        with ptj.session_lock(1, 2, "s1") as a3:
            self.assertTrue(a3)

    def test_different_sessions_independent(self):
        with ptj.session_lock(1, 2, "s1") as a1:
            self.assertTrue(a1)
            with ptj.session_lock(1, 2, "s2") as a2:
                self.assertTrue(a2)

    def test_lock_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with ptj.session_lock(1, 2, "s1") as a:
                self.assertTrue(a)
                raise RuntimeError("boom")
        self.assertEqual(self.cache._d, {})  # 锁已释放
        with ptj.session_lock(1, 2, "s1") as again:
            self.assertTrue(again)

    def test_manual_compact_and_post_turn_share_lock(self):
        """手动 /compact 与后台预压缩必须互斥，否则会重复调用 LLM 并互相覆盖状态。"""
        with ptj.session_lock(1, 2, "s1") as a:
            self.assertTrue(a)
            info = ptj.run_post_turn(1, 2, "s1", llm_config={"api_key": "k"})
        self.assertFalse(info["lock"])


if __name__ == "__main__":
    unittest.main()
