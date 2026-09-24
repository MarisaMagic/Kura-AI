"""批量任务状态：get_kb_upload_job_meta_many 的返回 key 必须是 task_id。

回归背景：cache.mget_json 返回的 key 是 Redis meta key（kb_upload_job:<id>:meta），
曾因直接透传该 dict，导致批量状态接口的 items 与前端 task_id 对不上，
大批量上传时所有任务被误判为「不存在或已过期」（实际已正常入库）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.kb import kb_job


class GetKbUploadJobMetaManyTests(unittest.TestCase):
    def test_keys_are_task_ids_not_redis_keys(self):
        ids = ["a" * 32, "b" * 32]
        raw = {
            kb_job._meta_key(ids[0]): {"task_id": ids[0], "status": "queued"},
            kb_job._meta_key(ids[1]): {"task_id": ids[1], "status": "completed"},
        }
        with mock.patch.object(kb_job.cache, "mget_json", return_value=raw):
            out = kb_job.get_kb_upload_job_meta_many(ids)
        self.assertEqual(set(out), set(ids))
        self.assertEqual(out[ids[0]]["status"], "queued")
        self.assertEqual(out[ids[1]]["status"], "completed")

    def test_missing_invalid_and_empty_ids_are_skipped(self):
        valid = "c" * 32
        raw = {
            kb_job._meta_key(valid): ["not", "a", "dict"],
        }
        with mock.patch.object(kb_job.cache, "mget_json", return_value=raw):
            out = kb_job.get_kb_upload_job_meta_many([valid, "d" * 32, ""])
        self.assertEqual(out, {})

    def test_requested_redis_keys_are_meta_keys(self):
        captured: dict = {}

        def fake_mget(keys):
            captured["keys"] = keys
            return {}

        with mock.patch.object(kb_job.cache, "mget_json", side_effect=fake_mget):
            kb_job.get_kb_upload_job_meta_many(["x" * 32, ""])
        self.assertEqual(captured["keys"], [kb_job._meta_key("x" * 32)])


if __name__ == "__main__":
    unittest.main()