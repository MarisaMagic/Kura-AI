"""跨会话用户长期记忆单测：PG（SQLite 内存库）upsert / 过滤 / 上限 / 格式化 / 清理，以及读取工具。

不依赖 PostgreSQL / Milvus / Redis / 真实 LLM。
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.chat.user_memory as um
from app.chat.db_models import ChatUserMemory
from app.settings import settings


class UserMemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatUserMemory.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls._orig_session = um.SessionLocal
        um.SessionLocal = factory

    @classmethod
    def tearDownClass(cls):
        um.SessionLocal = cls._orig_session

    def setUp(self):
        db = um.SessionLocal()
        try:
            db.query(ChatUserMemory).delete()
            db.commit()
        finally:
            db.close()

    def _fact(self, subject="回答语言", content="始终用中文", ftype="preference"):
        return {
            "type": ftype,
            "subject": subject,
            "content": content,
            "why": "用户明确要求",
            "how_to_apply": "生成回答时默认中文",
        }

    def test_normalize_filters_non_user_types(self):
        """决策/实体交给摘要，长期记忆只收偏好与约束。"""
        self.assertIsNone(um.normalize_fact({"type": "decision", "content": "用 JWT"}))
        self.assertIsNone(um.normalize_fact({"type": "entity", "content": "用户叫小王"}))
        self.assertIsNotNone(um.normalize_fact(self._fact()))
        self.assertIsNotNone(um.normalize_fact(self._fact(ftype="constraint")))

    def test_insert_skip_update(self):
        r1 = um.store_user_facts(1, 2, [self._fact()])
        self.assertEqual(r1["inserted"], 1)
        r2 = um.store_user_facts(1, 2, [self._fact()])
        self.assertEqual(r2["skipped"], 1)
        r3 = um.store_user_facts(1, 2, [self._fact(content="始终用英文")])
        self.assertEqual(r3["updated"], 1)
        rows = um.list_user_facts(1, 2)
        self.assertEqual(len(rows), 1)
        self.assertIn("英文", rows[0]["content"])

    def test_batch_dedup_same_slot_last_wins(self):
        um.store_user_facts(
            1, 2, [self._fact(content="请用中文回答"), self._fact(content="请用英文回答")]
        )
        rows = um.list_user_facts(1, 2)
        self.assertEqual(len(rows), 1)
        self.assertIn("英文", rows[0]["content"])

    def test_scope_isolation(self):
        um.store_user_facts(1, 2, [self._fact()])
        um.store_user_facts(9, 2, [self._fact()])
        self.assertEqual(len(um.list_user_facts(1, 2)), 1)
        self.assertEqual(len(um.list_user_facts(9, 2)), 1)
        self.assertEqual(len(um.list_user_facts(1, 99)), 0)

    def test_keyword_filter(self):
        um.store_user_facts(
            1,
            2,
            [
                self._fact(subject="回答语言"),
                self._fact(subject="输出格式", content="用 Markdown 表格"),
            ],
        )
        self.assertEqual(len(um.list_user_facts(1, 2, keyword="Markdown")), 1)
        self.assertEqual(len(um.list_user_facts(1, 2, keyword="不存在")), 0)

    def test_cap_evicts_oldest(self):
        with mock.patch.object(settings, "CHAT_MEMORY_USER_FACT_MAX", 2):
            for i in range(5):
                um.store_user_facts(1, 2, [self._fact(subject=f"主题{i}", content=f"请记住内容{i}")])
        self.assertEqual(len(um.list_user_facts(1, 2, limit=10)), 2)

    def test_disabled_is_noop(self):
        with mock.patch.object(settings, "CHAT_USER_MEMORY_ENABLED", False):
            out = um.store_user_facts(1, 2, [self._fact()])
        self.assertEqual(out, {"inserted": 0, "skipped": 0, "updated": 0})
        self.assertEqual(um.list_user_facts(1, 2), [])

    def test_format(self):
        um.store_user_facts(1, 2, [self._fact()])
        text = um.format_user_facts(um.list_user_facts(1, 2))
        self.assertIn("用户长期记忆", text)
        self.assertIn("[偏好]", text)
        self.assertIn("始终用中文", text)

    def test_format_empty(self):
        self.assertIn("暂无", um.format_user_facts([]))

    def test_purge(self):
        um.store_user_facts(1, 2, [self._fact()])
        self.assertEqual(um.purge_user_memory(1, 2), 1)
        self.assertEqual(um.list_user_facts(1, 2), [])

    def test_purge_for_agent(self):
        um.store_user_facts(1, 2, [self._fact()])
        um.store_user_facts(9, 2, [self._fact()])
        self.assertEqual(um.purge_user_memory_for_agent(2), 2)
        self.assertEqual(um.list_user_facts(1, 2), [])


class ReadUserMemoryToolTest(unittest.TestCase):
    def test_tool_registered_and_limits_calls(self):
        from app.chat.tools import reset_tool_call_guards
        from app.chat.user_memory_tool import make_read_user_memory_tool

        tool = make_read_user_memory_tool(1, 2)
        self.assertEqual(tool.name, "read_user_memory")
        self.assertIn("read_session_history", tool.description)

        reset_tool_call_guards()
        row = {"fact_type": "preference", "subject": "语言", "content": "用中文"}
        with mock.patch.object(um, "list_user_facts", return_value=[row]) as listed, mock.patch.object(
            um, "format_user_facts", return_value="【用户长期记忆（跨会话，偏好与硬约束）】\n- [偏好] 语言：用中文"
        ):
            first = tool.func(keyword="语言")
            second = tool.func()
        self.assertIn("用中文", first)
        self.assertIn("TOOL_CALL_LIMIT_REACHED", second)
        listed.assert_called_once_with(1, 2, keyword="语言")


if __name__ == "__main__":
    unittest.main()
