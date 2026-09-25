"""会话存储瘦身 / 配额 / 留存单测（SQLite 内存库，不依赖 PostgreSQL / Redis / LLM）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.chat.storage as storage_mod
from app.chat.db_models import (
    ChatAttachment,
    ChatCompactSegment,
    ChatMessage as ChatMessageRow,
    ChatSession as ChatSessionRow,
)
from app.chat.errors import ChatQuotaExceeded
from app.chat.history_tool import read_path_turns
from app.settings import settings


class _MemoryCache:
    def __init__(self):
        self._d: dict = {}

    def get_json(self, key):
        return self._d.get(key)

    def set_json(self, key, value, ttl=None):
        self._d[key] = value
        return True

    def delete(self, key):
        self._d.pop(key, None)


class _StorageBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        for table in (
            ChatSessionRow.__table__,
            ChatMessageRow.__table__,
            ChatCompactSegment.__table__,
            ChatAttachment.__table__,
        ):
            table.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls._orig_sl = storage_mod.SessionLocal
        cls._orig_cache = storage_mod.cache
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        storage_mod.SessionLocal = cls._orig_sl
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        db = storage_mod.SessionLocal()
        try:
            for table in (ChatMessageRow, ChatCompactSegment, ChatAttachment, ChatSessionRow):
                db.query(table).delete()
            db.commit()
        finally:
            db.close()

    def _raw_row(self, message_id: int):
        db = storage_mod.SessionLocal()
        try:
            return db.query(ChatMessageRow).filter(ChatMessageRow.id == message_id).first()
        finally:
            db.close()


class SessionQuotaTest(_StorageBase):
    def test_session_limit(self):
        with mock.patch.object(settings, "CHAT_MAX_SESSIONS_PER_USER_AGENT", 1):
            self.storage.append_messages(1, 2, "s1", [HumanMessage(content="hi")])
            with self.assertRaises(ChatQuotaExceeded):
                self.storage.append_messages(1, 2, "s2", [HumanMessage(content="hi")])
        # 不同智能体不受影响
        self.storage.append_messages(1, 99, "s2", [HumanMessage(content="hi")])

    def test_message_limit(self):
        with mock.patch.object(settings, "CHAT_MAX_MESSAGES_PER_SESSION", 3):
            self.storage.append_messages(1, 2, "s1", [HumanMessage(content="u1"), AIMessage(content="a1")])
            with self.assertRaises(ChatQuotaExceeded):
                self.storage.append_messages(1, 2, "s1", [HumanMessage(content="u2"), AIMessage(content="a2")])

    def test_check_chat_quota_reserve(self):
        with mock.patch.object(settings, "CHAT_MAX_MESSAGES_PER_SESSION", 3):
            self.storage.append_messages(1, 2, "s1", [HumanMessage(content="u1"), AIMessage(content="a1")])
            with self.assertRaises(ChatQuotaExceeded):
                self.storage.check_chat_quota(1, 2, "s1", reserve=2)
            self.storage.check_chat_quota(1, 2, "s1", reserve=1)

    def test_check_chat_quota_new_session_limit(self):
        with mock.patch.object(settings, "CHAT_MAX_SESSIONS_PER_USER_AGENT", 1):
            self.storage.append_messages(1, 2, "s1", [HumanMessage(content="hi")])
            with self.assertRaises(ChatQuotaExceeded):
                self.storage.check_chat_quota(1, 2, "s2", reserve=2)


class VersionEvictionTest(_StorageBase):
    def test_evicts_oldest_version(self):
        self.storage.append_messages(1, 2, "s1", [HumanMessage(content="问题")])
        self.storage.append_messages(1, 2, "s1", [AIMessage(content="回答1")])
        recs = self.storage.get_session_messages(1, 2, "s1")
        ai_id = next(r["message_id"] for r in recs if r["type"] == "ai")
        ref = self.storage.get_session_ref_id(1, 2, "s1")
        with mock.patch.object(settings, "CHAT_MAX_ASSISTANT_VERSIONS", 2):
            self.storage.insert_assistant_version(1, 2, "s1", ai_id, AIMessage(content="回答2"))
            self.storage.insert_assistant_version(1, 2, "s1", ai_id, AIMessage(content="回答3"))
        db = storage_mod.SessionLocal()
        try:
            ai_rows = (
                db.query(ChatMessageRow)
                .filter(
                    ChatMessageRow.session_ref_id == ref,
                    ChatMessageRow.message_type == "ai",
                )
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
            contents = [r.content for r in ai_rows]
        finally:
            db.close()
        self.assertEqual(len(contents), 2)
        self.assertIn("回答3", contents)
        self.assertNotIn("回答1", contents)


class PreviewAndExtraCapTest(_StorageBase):
    def test_preview_capped_but_envelope_full(self):
        with mock.patch.object(settings, "CHAT_MESSAGE_PREVIEW_CHARS", 10):
            self.storage.append_messages(1, 2, "s1", [HumanMessage(content="长" * 100)])
        recs = self.storage.get_session_messages(1, 2, "s1")
        row = self._raw_row(recs[0]["message_id"])
        self.assertEqual(len(row.content), 11)  # 10 + 省略号
        self.assertTrue(row.content.endswith("…"))
        self.assertEqual(row.content_json["lc"], "长" * 100)

    def test_extra_json_list_capped(self):
        with mock.patch.object(settings, "CHAT_RAG_STEPS_MAX", 5):
            self.storage.append_messages(
                1,
                2,
                "s1",
                [AIMessage(content="答")],
                extra_message_data=[{"rag_steps": [{"i": i} for i in range(100)]}],
            )
        recs = self.storage.get_session_messages(1, 2, "s1")
        row = self._raw_row(recs[0]["message_id"])
        self.assertEqual(len(row.rag_steps), 5)


class HistoryFullTextTest(_StorageBase):
    def test_read_path_turns_uses_full_envelope(self):
        """预览列被截断时，read_session_history 仍应取到完整原文（读 content_json）。"""
        with mock.patch.object(settings, "CHAT_MESSAGE_PREVIEW_CHARS", 5):
            self.storage.append_messages(1, 2, "s1", [HumanMessage(content="长" * 50)])
        turns = read_path_turns(1, 2, "s1")
        self.assertEqual(turns[0]["user"], "长" * 50)


class RetentionTest(_StorageBase):
    def test_purge_idle_sessions(self):
        self.storage.append_messages(1, 2, "old", [HumanMessage(content="旧")])
        self.storage.append_messages(1, 2, "new", [HumanMessage(content="新")])
        db = storage_mod.SessionLocal()
        try:
            db.query(ChatSessionRow).filter(ChatSessionRow.session_id == "old").update(
                {"updated_at": datetime.utcnow() - timedelta(days=100)}
            )
            db.commit()
        finally:
            db.close()

        cutoff = datetime.utcnow() - timedelta(days=30)
        dry = self.storage.purge_idle_sessions(cutoff, batch=10, dry_run=True)
        self.assertEqual(dry["scanned"], 1)
        self.assertEqual(dry["deleted"], 0)

        res = self.storage.purge_idle_sessions(cutoff, batch=10, dry_run=False)
        self.assertEqual(res["deleted"], 1)

        db = storage_mod.SessionLocal()
        try:
            remaining = sorted(r.session_id for r in db.query(ChatSessionRow).all())
        finally:
            db.close()
        self.assertEqual(remaining, ["new"])


if __name__ == "__main__":
    unittest.main()
