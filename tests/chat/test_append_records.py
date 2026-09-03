"""
append 路径回归：追加前后 records 结构完全一致 + 分支/重新生成场景；
并用 SQL 事件计数证明单次追加只对消息表发一次全量 SELECT（优化前为 3 次）。
"""

from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.chat.storage as storage_mod
from app.chat.db_models import ChatMessage as ChatMessageRow
from app.chat.db_models import ChatSession as ChatSessionRow


class _MemoryCache:
    def __init__(self):
        self._d = {}

    def get_json(self, key):
        return self._d.get(key)

    def set_json(self, key, value, ttl=None):
        self._d[key] = value
        return True

    def delete(self, key):
        self._d.pop(key, None)


class AppendRecordsRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        cls._orig_session_local = storage_mod.SessionLocal
        storage_mod.SessionLocal = sessionmaker(
            bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        cls._orig_cache = storage_mod.cache
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        storage_mod.SessionLocal = cls._orig_session_local
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        self.uid, self.aid = 910001, 910001
        self.sid = f"append_records_{self.id().split('.')[-1]}"
        self._msg_selects = 0

        def _count(conn, cursor, statement, parameters, context, executemany):
            if "FROM MG_CHAT_MESSAGES" in statement.upper():
                self._msg_selects += 1

        event.listen(self.engine, "before_cursor_execute", _count)
        self._remove_listener = lambda: event.remove(
            self.engine, "before_cursor_execute", _count
        )

    def tearDown(self):
        self._remove_listener()

    def _append(self, msgs, extras=None):
        self.storage.append_messages(self.uid, self.aid, self.sid, msgs, extras)

    def test_records_structure_after_append(self):
        self._append([HumanMessage(content="Q1")])
        self._append([AIMessage(content="A1")])
        self._append(
            [HumanMessage(content="Q2")],
            extras=[{"thinking_text": "想了一下", "rag_steps": [{"label": "检索"}]}],
        )
        self._append([AIMessage(content="A2")])

        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        self.assertEqual([r["content"] for r in records], ["Q1", "A1", "Q2", "A2"])
        ids = [r["message_id"] for r in records]
        self.assertEqual(records[0]["parent_id"], None)
        self.assertEqual([r["parent_id"] for r in records[1:]], ids[:-1])
        # extras 完整落库回放
        self.assertEqual(records[2]["thinking_text"], "想了一下")
        self.assertEqual(records[2]["rag_steps"], [{"label": "检索"}])
        # 线性链上每条消息都是唯一版本
        for r in records:
            self.assertEqual(r["version_index"], 1)
            self.assertEqual(r["version_count"], 1)
            self.assertEqual(r["sibling_ids"], [r["message_id"]])
        # 缓存与库一致（缓存即追加路径写入的 records）
        cache_key = self.storage._messages_cache_key(self.uid, self.aid, self.sid)
        self.assertEqual(storage_mod.cache.get_json(cache_key), records)

    def test_append_after_branch_keeps_version_meta(self):
        self._append([HumanMessage(content="Q1")])
        self._append([AIMessage(content="A1")])
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a1 = records[-1]
        # 重新生成：插入兄弟版本
        ok = self.storage.insert_assistant_version(
            self.uid, self.aid, self.sid, a1["message_id"], AIMessage(content="A1-v2")
        )
        self.assertTrue(ok)
        # 在新分支上继续追加
        self._append([HumanMessage(content="Q2B")])
        self._append([AIMessage(content="A2B")])

        records2 = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        self.assertEqual(
            [r["content"] for r in records2], ["Q1", "A1-v2", "Q2B", "A2B"]
        )
        cur = records2[1]
        self.assertEqual(cur["version_index"], 2)
        self.assertEqual(cur["version_count"], 2)
        self.assertEqual(sorted(cur["sibling_ids"]), sorted([a1["message_id"], cur["message_id"]]))
        # 新追加消息不携带版本信息
        self.assertEqual(records2[2]["version_count"], 1)
        self.assertEqual(records2[3]["version_count"], 1)

    def test_session_preview_and_count_follow_selected_path(self):
        self._append([HumanMessage(content="第一个问题")])
        self._append([AIMessage(content="A1")])
        infos = self.storage.list_session_infos(self.uid, self.aid)
        info = next(x for x in infos if x["session_id"] == self.sid)
        self.assertEqual(info["last_user_preview"], "第一个问题")
        self.assertEqual(info["message_count"], 2)
        self._append([HumanMessage(content="第二个问题")])
        self._append([AIMessage(content="A2")])
        infos = self.storage.list_session_infos(self.uid, self.aid)
        info = next(x for x in infos if x["session_id"] == self.sid)
        self.assertEqual(info["last_user_preview"], "第二个问题")
        self.assertEqual(info["message_count"], 4)

    def test_single_message_select_per_append(self):
        """优化目标：一次 append 对消息表的全量 SELECT ≤ 1（优化前为 3）。"""
        self._append([HumanMessage(content="Q1")])
        self._append([AIMessage(content="A1")])
        self._msg_selects = 0
        self._append([HumanMessage(content="Q2")])
        self.assertLessEqual(
            self._msg_selects,
            1,
            f"append 期间对消息表的全量 SELECT 应 ≤1，实际 {self._msg_selects}",
        )
        # 行为不受影响
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        self.assertEqual([r["content"] for r in records], ["Q1", "A1", "Q2"])


if __name__ == "__main__":
    unittest.main()
