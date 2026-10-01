"""用户长期记忆异步版：aiosqlite 文件库上走真异步分支。

Windows 默认 ProactorEventLoop 会让 @sync_fallback_fn 退化为线程执行同步实现，
本测试显式使用 SelectorEventLoop（win32）以覆盖真正的异步实现；Linux 默认即真异步。
与同步单测（tests/chat/test_user_memory.py）语义对齐：upsert/skip/update、keyword、
cap 驱逐、删除、scope 隔离、禁用 noop。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.chat.user_memory as um
from app.chat.db_models import ChatUserMemory
from app.chat.user_memory import adelete_user_facts, alist_user_facts, astore_user_facts
from app.settings import settings


def run_true_async(coro):
    """在真异步分支执行协程（win32 强制 SelectorEventLoop，绕过 Proactor 退化）。"""
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class UserMemoryAsyncTest(unittest.TestCase):
    def setUp(self):
        self._orig_get_async_session = um.get_async_session
        self._tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._tmp.name, "memory_async_test.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        um.get_async_session = self.factory

        async def _create_table():
            async with self.engine.begin() as conn:
                await conn.run_sync(ChatUserMemory.__table__.create)

        run_true_async(_create_table())

    def tearDown(self):
        um.get_async_session = self._orig_get_async_session
        run_true_async(self.engine.dispose())
        self._tmp.cleanup()

    def _fact(self, subject="回答语言", content="始终用中文", ftype="preference"):
        return {
            "type": ftype,
            "subject": subject,
            "content": content,
            "why": "用户明确要求",
            "how_to_apply": "生成回答时默认中文",
        }

    def test_runs_on_true_async_branch(self):
        """确认测试环境未走 Windows 线程退化（否则本文件覆盖不到异步实现）。"""
        from app.utils.async_compat import async_pg_unsupported

        async def _impl():
            return async_pg_unsupported()

        self.assertFalse(run_true_async(_impl()))

    def test_insert_skip_update(self):
        async def _impl():
            r1 = await astore_user_facts(1, 2, [self._fact()])
            r2 = await astore_user_facts(1, 2, [self._fact()])
            r3 = await astore_user_facts(1, 2, [self._fact(content="始终用英文")])
            rows = await alist_user_facts(1, 2)
            return r1, r2, r3, rows

        r1, r2, r3, rows = run_true_async(_impl())
        self.assertEqual(r1["inserted"], 1)
        self.assertEqual(r2["skipped"], 1)
        self.assertEqual(r3["updated"], 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("英文", rows[0]["content"])

    def test_batch_dedup_same_slot_last_wins(self):
        async def _impl():
            await astore_user_facts(
                1, 2, [self._fact(content="请用中文回答"), self._fact(content="请用英文回答")]
            )
            return await alist_user_facts(1, 2)

        rows = run_true_async(_impl())
        self.assertEqual(len(rows), 1)
        self.assertIn("英文", rows[0]["content"])

    def test_scope_isolation(self):
        async def _impl():
            await astore_user_facts(1, 2, [self._fact()])
            await astore_user_facts(9, 2, [self._fact()])
            return (
                await alist_user_facts(1, 2),
                await alist_user_facts(9, 2),
                await alist_user_facts(1, 99),
            )

        a, b, c = run_true_async(_impl())
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(len(c), 0)

    def test_keyword_filter(self):
        async def _impl():
            await astore_user_facts(
                1,
                2,
                [
                    self._fact(subject="回答语言"),
                    self._fact(subject="输出格式", content="用 Markdown 表格"),
                ],
            )
            return (
                await alist_user_facts(1, 2, keyword="Markdown"),
                await alist_user_facts(1, 2, keyword="不存在"),
            )

        hit, miss = run_true_async(_impl())
        self.assertEqual(len(hit), 1)
        self.assertEqual(len(miss), 0)

    def test_cap_evicts_oldest(self):
        with mock.patch.object(settings, "CHAT_MEMORY_USER_FACT_MAX", 2):
            async def _impl():
                for i in range(5):
                    await astore_user_facts(
                        1, 2, [self._fact(subject=f"主题{i}", content=f"请记住内容{i}")]
                    )

            run_true_async(_impl())

        async def _list():
            return await alist_user_facts(1, 2, limit=10)

        rows = run_true_async(_list())
        self.assertEqual(len(rows), 2)

    def test_delete_keyword_and_all(self):
        async def _seed():
            await astore_user_facts(
                1,
                2,
                [
                    self._fact(subject="回答语言", content="始终用中文"),
                    self._fact(subject="输出格式", content="用 Markdown 表格"),
                ],
            )

        run_true_async(_seed())

        async def _delete_kw():
            return await adelete_user_facts(1, 2, keyword="Markdown")

        removed = run_true_async(_delete_kw())
        self.assertEqual(removed, 1)

        async def _remaining():
            return await alist_user_facts(1, 2)

        self.assertEqual(len(run_true_async(_remaining())), 1)

        async def _delete_all():
            return await adelete_user_facts(1, 2, all=True)

        self.assertEqual(run_true_async(_delete_all()), 1)

        async def _empty():
            return await alist_user_facts(1, 2)

        self.assertEqual(run_true_async(_empty()), [])

    def test_delete_requires_keyword_or_all(self):
        async def _impl():
            return await adelete_user_facts(1, 2)

        self.assertEqual(run_true_async(_impl()), 0)

    def test_disabled_is_noop(self):
        with mock.patch.object(settings, "CHAT_USER_MEMORY_ENABLED", False):
            stored = run_true_async(astore_user_facts(1, 2, [self._fact()]))
        self.assertEqual(stored, {"inserted": 0, "skipped": 0, "updated": 0})

        rows = run_true_async(alist_user_facts(1, 2))
        self.assertEqual(rows, [])

    def test_invalid_fact_ignored(self):
        async def _impl():
            return await astore_user_facts(
                1, 2, [{"type": "decision", "content": "用 JWT"}, {"type": "preference", "content": "短"}]
            )

        stored = run_true_async(_impl())
        self.assertEqual(stored, {"inserted": 0, "skipped": 0, "updated": 0})

        rows = run_true_async(alist_user_facts(1, 2))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()