"""KB 上传队列 Stream 后端：入队/出队/ack/超时回收/旧列表迁移。不依赖真实 Redis。

后端由 KB_UPLOAD_QUEUE_BACKEND 选择（stream|list|kafka），默认 stream；list / kafka 见各自测试。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.kb import kb_job


def _payload(task_id: str = "t1") -> dict:
    return {"kind": "kb_upload", "task_id": task_id}


class QueueBackendTests(unittest.TestCase):
    def test_default_backend_is_stream(self):
        with mock.patch.object(kb_job.settings, "KB_UPLOAD_QUEUE_BACKEND", "stream", create=True):
            self.assertEqual(kb_job.queue_backend(), "stream")

    def test_invalid_backend_falls_back_to_stream(self):
        with mock.patch.object(kb_job.settings, "KB_UPLOAD_QUEUE_BACKEND", "bogus", create=True):
            self.assertEqual(kb_job.queue_backend(), "stream")

    def test_list_backend_is_accepted(self):
        with mock.patch.object(kb_job.settings, "KB_UPLOAD_QUEUE_BACKEND", "list", create=True):
            self.assertEqual(kb_job.queue_backend(), "list")


class EnqueueTests(unittest.TestCase):
    def test_stream_enqueue_uses_xadd_limited(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="stream"), mock.patch.object(
            kb_job.cache, "xadd_limited_json", return_value="1-0"
        ) as m:
            self.assertTrue(kb_job.enqueue_task("t1"))
        self.assertEqual(m.call_args.args[0], kb_job._STREAM_KEY)

    def test_stream_enqueue_returns_false_when_full(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="stream"), mock.patch.object(
            kb_job.cache, "xadd_limited_json", return_value=None
        ):
            self.assertFalse(kb_job.enqueue_task("t1"))

    def test_list_enqueue_uses_rpush_with_limit(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="list"), mock.patch.object(
            kb_job.cache, "rpush_json_with_limit", return_value=True
        ) as m:
            self.assertTrue(kb_job.enqueue_task("t1"))
        self.assertEqual(m.call_args.args[0], kb_job._QUEUE_KEY)


class DequeueAckTests(unittest.TestCase):
    def test_stream_dequeue_returns_msg_id_and_payload(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="stream"), mock.patch.object(
            kb_job.cache, "xreadgroup_json", return_value=[("7-0", _payload())]
        ):
            entry = kb_job.dequeue_task(timeout=1, consumer="c1")
        self.assertEqual(entry, ("7-0", _payload()))

    def test_stream_dequeue_empty_returns_none(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="stream"), mock.patch.object(
            kb_job.cache, "xreadgroup_json", return_value=[]
        ):
            self.assertIsNone(kb_job.dequeue_task(timeout=1, consumer="c1"))

    def test_stream_ack_uses_xack_del_by_message_id(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="stream"), mock.patch.object(
            kb_job.cache, "xack_del"
        ) as m:
            kb_job.ack_task("7-0")
        m.assert_called_once_with(kb_job._STREAM_KEY, kb_job._CONSUMER_GROUP, "7-0")

    def test_list_ack_uses_lrem_by_payload(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="list"), mock.patch.object(
            kb_job.cache, "lrem_json"
        ) as m:
            kb_job.ack_task(_payload())
        m.assert_called_once_with(kb_job._PROCESSING_KEY, _payload(), count=1)


class QueueDepthTests(unittest.TestCase):
    def test_stream_depth_is_xlen(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="stream"), mock.patch.object(
            kb_job.cache, "xlen", return_value=5
        ) as m:
            self.assertEqual(kb_job.queue_depth(), 5)
        m.assert_called_once_with(kb_job._STREAM_KEY)


class ReclaimTests(unittest.TestCase):
    def _patches(self, *, delivery: int, meta: dict):
        return [
            mock.patch.object(kb_job, "queue_backend", return_value="stream"),
            mock.patch.object(
                kb_job.cache, "xautoclaim_json", return_value=[("5-0", _payload())]
            ),
            mock.patch.object(kb_job.cache, "xpending_delivery_count", return_value=delivery),
            mock.patch.object(kb_job.cache, "get_json", return_value=meta),
            mock.patch.object(kb_job.cache, "xack_del"),
            mock.patch.object(kb_job.cache, "xadd_json"),
            mock.patch.object(kb_job, "_release_processing_lock"),
            mock.patch.object(kb_job, "_update_meta"),
            mock.patch.object(kb_job, "run_upload_task_from_source"),
            mock.patch.object(kb_job, "_remove_user_active"),
        ]

    def test_stale_meta_is_reprocessed_then_acked(self):
        meta = {"task_id": "t1", "user_id": 1, "status": "running", "updated_at": 0}
        stack = self._patches(delivery=2, meta=meta)
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        kb_job.reclaim_stale_tasks("c1")
        kb_job._release_processing_lock.assert_called_once_with("t1")
        kb_job.run_upload_task_from_source.assert_called_once_with("t1")
        kb_job.cache.xack_del.assert_called_once_with(
            kb_job._STREAM_KEY, kb_job._CONSUMER_GROUP, "5-0"
        )

    def test_terminal_meta_is_only_acked(self):
        meta = {"task_id": "t1", "user_id": 1, "status": "completed"}
        stack = self._patches(delivery=1, meta=meta)
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        kb_job.reclaim_stale_tasks("c1")
        kb_job.run_upload_task_from_source.assert_not_called()
        kb_job.cache.xack_del.assert_called_once()

    def test_missing_meta_is_only_acked(self):
        stack = self._patches(delivery=1, meta=None)
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        kb_job.reclaim_stale_tasks("c1")
        kb_job.run_upload_task_from_source.assert_not_called()
        kb_job.cache.xack_del.assert_called_once()

    def test_exceeding_max_deliveries_marks_failed_and_dead_letters(self):
        meta = {"task_id": "t1", "user_id": 1, "status": "running", "updated_at": 0}
        with mock.patch.object(
            kb_job.settings, "KB_UPLOAD_MAX_DELIVERIES", 3, create=True
        ):
            stack = self._patches(delivery=4, meta=meta)
            for p in stack:
                p.start()
                self.addCleanup(p.stop)
            kb_job.reclaim_stale_tasks("c1")
        kb_job.run_upload_task_from_source.assert_not_called()
        _args, kwargs = kb_job._update_meta.call_args
        self.assertEqual(kwargs.get("status"), "failed")
        kb_job.cache.xadd_json.assert_called_once()
        self.assertEqual(kb_job.cache.xadd_json.call_args.args[0], kb_job._DEAD_STREAM_KEY)
        kb_job.cache.xack_del.assert_called_once()
        kb_job._remove_user_active.assert_called_once_with(1, "t1")


class MigrationTests(unittest.TestCase):
    def test_migrate_moves_legacy_list_entries_into_stream(self):
        raw = '{"kind": "kb_upload", "task_id": "a"}'
        with mock.patch.object(kb_job.cache, "set_nx", return_value=True), mock.patch.object(
            kb_job.cache, "lrange_str", side_effect=[[raw], []]
        ), mock.patch.object(kb_job.cache, "xadd_json") as xadd, mock.patch.object(
            kb_job.cache, "lrem_raw"
        ) as rem, mock.patch.object(
            kb_job.cache, "delete"
        ):
            moved = kb_job.migrate_list_to_stream()
        self.assertEqual(moved, 1)
        xadd.assert_called_once()
        self.assertEqual(xadd.call_args.args[0], kb_job._STREAM_KEY)
        rem.assert_called_once()

    def test_migrate_skips_when_lock_not_acquired(self):
        with mock.patch.object(kb_job.cache, "set_nx", return_value=False), mock.patch.object(
            kb_job.cache, "lrange_str"
        ) as lrange:
            self.assertEqual(kb_job.migrate_list_to_stream(), 0)
        lrange.assert_not_called()


if __name__ == "__main__":
    unittest.main()
