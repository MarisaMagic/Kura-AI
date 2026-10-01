"""Milvus 连接不可用判定：连接失败应转成友好 503，而不是逐条记为删除失败。"""

from __future__ import annotations

import unittest
from unittest import mock

from pymilvus import MilvusException

from app.kb import kb_service


def _connect_failure() -> MilvusException:
    return MilvusException(
        code=2,
        message="Fail connecting to server on standalone:19530, illegal connection params or server unavailable",
    )


class IsMilvusUnavailableTests(unittest.TestCase):
    def test_connect_failure_is_unavailable(self):
        self.assertTrue(kb_service.is_milvus_unavailable(_connect_failure()))

    def test_other_milvus_error_is_not_unavailable(self):
        self.assertFalse(
            kb_service.is_milvus_unavailable(MilvusException(code=1, message="some other error"))
        )

    def test_non_milvus_exception_is_not_unavailable(self):
        self.assertFalse(kb_service.is_milvus_unavailable(RuntimeError("boom")))


class DeleteKbDocumentsMilvusUnavailableTests(unittest.TestCase):
    def test_connect_failure_propagates(self):
        with mock.patch.object(kb_service, "delete_kb_document", side_effect=_connect_failure()):
            with self.assertRaises(MilvusException):
                kb_service.delete_kb_documents("scope", 1, 2, ["a.md"])


if __name__ == "__main__":
    unittest.main()
