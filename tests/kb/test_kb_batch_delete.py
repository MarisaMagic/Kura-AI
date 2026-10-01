"""知识库批量删除：文件名去重/清洗、单次上限与成功/失败统计。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.kb import kb_service


class DeleteKbDocumentsTests(unittest.TestCase):
    def test_dedupe_strip_and_skip_empty(self):
        calls: list[str] = []

        def fake_delete(scope, user_id, agent_id, name, **kwargs):
            calls.append(name)
            return True

        with mock.patch.object(kb_service, "delete_kb_document", side_effect=fake_delete):
            result = kb_service.delete_kb_documents(
                "scope", 1, 2, ["a.md", " a.md ", "", "   ", "b.md", "b.md"]
            )
        self.assertEqual(calls, ["a.md", "b.md"])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["requested"], 2)

    def test_exceeds_limit_raises(self):
        names = [f"f{i}.md" for i in range(kb_service.BATCH_DELETE_MAX + 1)]
        with mock.patch.object(kb_service, "delete_kb_document") as fake:
            with self.assertRaises(ValueError):
                kb_service.delete_kb_documents("scope", 1, 2, names)
        fake.assert_not_called()

    def test_counts_deleted_and_failed(self):
        def fake_delete(scope, user_id, agent_id, name, **kwargs):
            if name == "ok.md":
                return True
            if name == "false.md":
                return False
            raise RuntimeError("boom")

        with mock.patch.object(kb_service, "delete_kb_document", side_effect=fake_delete):
            result = kb_service.delete_kb_documents("scope", 1, 2, ["ok.md", "false.md", "err.md"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["failed"], ["false.md", "err.md"])
        self.assertEqual(result["requested"], 3)


if __name__ == "__main__":
    unittest.main()
