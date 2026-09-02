"""
消息树存储层集成测试：SQLite 内存库替代 PostgreSQL（仅建会话/消息两表），
覆盖 append / insert_assistant_version / update_assistant_in_place / select_branch / 路径回放。
Redis 不可用时 cache 自动降级为无缓存，不影响本测试。
"""

from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.chat.storage as storage_mod
from app.chat.db_models import ChatMessage as ChatMessageRow
from app.chat.db_models import ChatSession as ChatSessionRow


class _MemoryCache:
    """Redis 不可用时的内存桩：接口与 app.chat.cache.cache 用到的子集一致。"""

    def __init__(self):
        self._d = {}

    def get_json(self, key):
        return self._d.get(key)

    def set_json(self, key, value, ttl=None):
        self._d[key] = value
        return True

    def delete(self, key):
        self._d.pop(key, None)


class BranchStorageSqliteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=engine)
        ChatMessageRow.__table__.create(bind=engine)
        cls._orig_session_local = storage_mod.SessionLocal
        storage_mod.SessionLocal = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        cls._orig_cache = storage_mod.cache
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        storage_mod.SessionLocal = cls._orig_session_local
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        self.uid, self.aid = 900001, 900001
        self.sid = f"branch_sqlite_{self.id().split('.')[-1]}"
        s = self.storage
        # U1/A1, U2/A2, U3/A3 三轮线性对话
        s.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content="问题1")])
        s.append_messages(self.uid, self.aid, self.sid, [AIMessage(content="回答1")])
        s.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content="问题2")])
        s.append_messages(self.uid, self.aid, self.sid, [AIMessage(content="回答2")])
        s.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content="问题3")])
        s.append_messages(self.uid, self.aid, self.sid, [AIMessage(content="回答3")])

    def _path_texts(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        return [(r["message_id"], r["type"], r["content"]) for r in records]

    def test_linear_append_links_parent_chain(self):
        path = self._path_texts()
        self.assertEqual([c for _, _, c in path], ["问题1", "回答1", "问题2", "回答2", "问题3", "回答3"])
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        ids = [r["message_id"] for r in records]
        parents = [r["parent_id"] for r in records]
        self.assertEqual(parents[0], None)
        self.assertEqual(parents[1:], ids[:-1])

    def test_insert_version_keeps_old_branch_and_selects_new(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a2 = next(r for r in records if r["content"] == "回答2")
        ok = self.storage.insert_assistant_version(
            self.uid, self.aid, self.sid, a2["message_id"], AIMessage(content="回答2-v2")
        )
        self.assertTrue(ok)
        # 新路径：第 2 轮换成 v2，第 3 轮（旧分支）不在路径上
        path = self._path_texts()
        self.assertEqual([c for _, _, c in path], ["问题1", "回答1", "问题2", "回答2-v2"])
        v2 = next(r for r in records if r["content"] == "回答2")
        # 版本信息：2 个版本，当前选中第 2 版
        records2 = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        cur = records2[3]
        self.assertEqual(cur["version_index"], 2)
        self.assertEqual(cur["version_count"], 2)
        self.assertEqual(cur["sibling_ids"], [v2["message_id"], cur["message_id"]])

    def test_select_branch_roundtrip(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a2 = next(r for r in records if r["content"] == "回答2")
        self.storage.insert_assistant_version(
            self.uid, self.aid, self.sid, a2["message_id"], AIMessage(content="回答2-v2")
        )
        # 切回 v1：旧分支后续轮次恢复可见
        back = self.storage.select_branch(self.uid, self.aid, self.sid, a2["message_id"])
        self.assertIsNotNone(back)
        self.assertEqual(
            [r["content"] for r in back], ["问题1", "回答1", "问题2", "回答2", "问题3", "回答3"]
        )
        # 再切到 v2：v2 不在当前路径，从 sibling_ids 取
        a2_rec = next(r for r in back if r["content"] == "回答2")
        other = [i for i in a2_rec["sibling_ids"] if i != a2_rec["message_id"]][0]
        fwd = self.storage.select_branch(self.uid, self.aid, self.sid, other)
        self.assertEqual([r["content"] for r in fwd], ["问题1", "回答1", "问题2", "回答2-v2"])

    def test_continue_on_new_branch_appends_under_selected_version(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a2 = next(r for r in records if r["content"] == "回答2")
        self.storage.insert_assistant_version(
            self.uid, self.aid, self.sid, a2["message_id"], AIMessage(content="回答2-v2")
        )
        # 在新分支上继续第 3' 轮
        self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content="问题3B")])
        self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage(content="回答3B")])
        path = self._path_texts()
        self.assertEqual(
            [c for _, _, c in path], ["问题1", "回答1", "问题2", "回答2-v2", "问题3B", "回答3B"]
        )
        # 切回 v1 后旧第 3 轮仍在
        back = self.storage.select_branch(self.uid, self.aid, self.sid, a2["message_id"])
        self.assertEqual(
            [r["content"] for r in back], ["问题1", "回答1", "问题2", "回答2", "问题3", "回答3"]
        )

    def test_update_assistant_in_place_keeps_single_version(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a3 = records[-1]
        ok = self.storage.update_assistant_in_place(
            self.uid, self.aid, self.sid, a3["message_id"], AIMessage(content="回答3-覆盖")
        )
        self.assertTrue(ok)
        records2 = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        self.assertEqual(records2[-1]["content"], "回答3-覆盖")
        self.assertEqual(records2[-1]["message_id"], a3["message_id"])
        self.assertEqual(records2[-1]["version_count"], 1)

    def test_get_regenerate_context_ends_at_target_parent_human(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a2 = next(r for r in records if r["content"] == "回答2")
        ctx = self.storage.get_regenerate_context(self.uid, self.aid, self.sid, a2["message_id"])
        self.assertIsNotNone(ctx)
        messages, human_text, ids = ctx
        self.assertEqual(human_text, "问题2")
        self.assertEqual([getattr(m, "content", "") for m in messages], ["问题1", "回答1", "问题2"])
        self.assertEqual(len(ids), 3)

    def test_invalid_targets_rejected(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        u2 = next(r for r in records if r["content"] == "问题2")
        # 目标不是 ai
        self.assertFalse(
            self.storage.insert_assistant_version(
                self.uid, self.aid, self.sid, u2["message_id"], AIMessage(content="x")
            )
        )
        # 不存在的 id
        self.assertIsNone(
            self.storage.get_regenerate_context(self.uid, self.aid, self.sid, 99999999)
        )
        self.assertIsNone(self.storage.select_branch(self.uid, self.aid, self.sid, 99999999))

    def test_session_info_uses_current_path(self):
        records = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        a2 = next(r for r in records if r["content"] == "回答2")
        self.storage.insert_assistant_version(
            self.uid, self.aid, self.sid, a2["message_id"], AIMessage(content="回答2-v2")
        )
        infos = self.storage.list_session_infos(self.uid, self.aid)
        info = next(x for x in infos if x["session_id"] == self.sid)
        # 当前路径只有 2 轮：预览应为「问题2」而不是旧分支的「问题3」
        self.assertEqual(info["last_user_preview"], "问题2")
        self.assertEqual(info["message_count"], 4)


if __name__ == "__main__":
    unittest.main()
