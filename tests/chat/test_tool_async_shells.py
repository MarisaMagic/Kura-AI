"""异步工具壳行为：守卫 / 限流 / 错误路径 / 输出与来源登记（mock 异步后端，不打外网）。

对应 P2 的 coroutine 注册壳：web_search / fetch_url / web_image_search /
user_memory×3 / session_history / session_attachment×3。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.chat import attachment_tools, history_tool, user_memory, web_image_search, web_search_tool
from app.chat.tools import get_last_rag_context, reset_tool_call_guards, set_turn_tool_policy
from app.settings import settings


def _run(coro):
    return asyncio.run(coro)


def _web_results() -> list[dict]:
    return [
        {
            "title": "标题一",
            "url": "https://a.example/1",
            "snippet": "摘要一",
            "siteName": "站点A",
            "datePublished": "2026-01-02",
            "read_ok": True,
            "page_text": "正文摘录一",
        }
    ]


class _AsyncShellTestBase(unittest.TestCase):
    def setUp(self):
        reset_tool_call_guards()
        # last_rag_context 为请求级状态且来源列表为合并语义：逐测试清空避免相互污染
        get_last_rag_context(clear=True)
        set_turn_tool_policy(use_knowledge_retrieval=True, use_web_search=True)

    def tearDown(self):
        reset_tool_call_guards()
        get_last_rag_context(clear=True)
        set_turn_tool_policy(use_knowledge_retrieval=True, use_web_search=False)


class WebSearchAsyncShellTests(_AsyncShellTestBase):
    def _patched_search(self, results=None):
        results = results if results is not None else _web_results()
        return [
            mock.patch.object(
                web_search_tool,
                "_arun_searches",
                mock.AsyncMock(return_value=(results, "bocha", "noLimit")),
            ),
            mock.patch.object(
                web_search_tool,
                "_arerank_web_results",
                mock.AsyncMock(return_value=(results, {"applied": False})),
            ),
            mock.patch.object(
                web_search_tool,
                "aread_top_pages",
                mock.AsyncMock(return_value=(results, {"enabled": True, "attempted": 1, "ok": 1})),
            ),
        ]

    def test_happy_path_registers_sources(self):
        tool = web_search_tool.make_web_search_tool(prefer_async=True)
        patches = self._patched_search()
        for p in patches:
            p.start()
        try:
            out = _run(tool.ainvoke({"query": "深度求索"}))
        finally:
            for p in patches:
                p.stop()
        self.assertIn("[1] 标题一", out)
        self.assertIn("https://a.example/1", out)
        self.assertIn("正文摘录一", out)
        ctx = get_last_rag_context(clear=False) or {}
        sources = ctx.get("web_sources") or []
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["url"], "https://a.example/1")
        self.assertTrue(sources[0]["read_ok"])

    def test_disabled_turn_returns_guard_message(self):
        set_turn_tool_policy(use_knowledge_retrieval=True, use_web_search=False)
        tool = web_search_tool.make_web_search_tool(prefer_async=True)
        out = _run(tool.ainvoke({"query": "任意"}))
        self.assertTrue(out.startswith("TOOL_DISABLED_THIS_TURN"))

    def test_slot_limit(self):
        tool = web_search_tool.make_web_search_tool(prefer_async=True)
        patches = self._patched_search()
        for p in patches:
            p.start()
        try:
            with mock.patch.object(settings, "WEB_SEARCH_MAX_CALLS_PER_TURN", 1):
                first = _run(tool.ainvoke({"query": "第一次"}))
                second = _run(tool.ainvoke({"query": "第二次"}))
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("TOOL_CALL_LIMIT_REACHED", first)
        self.assertTrue(second.startswith("TOOL_CALL_LIMIT_REACHED"))

    def test_backend_failure_returns_failed_message(self):
        tool = web_search_tool.make_web_search_tool(prefer_async=True)
        with mock.patch.object(
            web_search_tool, "_arun_searches", mock.AsyncMock(side_effect=RuntimeError("boom"))
        ):
            out = _run(tool.ainvoke({"query": "任意"}))
        self.assertTrue(out.startswith("WEB_SEARCH_FAILED"))

    def test_empty_query(self):
        tool = web_search_tool.make_web_search_tool(prefer_async=True)
        out = _run(tool.ainvoke({"query": ""}))
        self.assertEqual(out, "错误：query 为空。")


class FetchUrlAsyncShellTests(_AsyncShellTestBase):
    def test_happy_path(self):
        tool = web_search_tool.make_fetch_url_tool(prefer_async=True)
        page = {"ok": True, "title": "Example", "text": "Example Domain 正文", "url": "https://example.com/", "error": ""}
        with mock.patch.object(web_search_tool, "afetch_page", mock.AsyncMock(return_value=page)) as m:
            out = _run(tool.ainvoke({"url": "https://example.com/"}))
        m.assert_awaited_once()
        self.assertIn("Example Domain 正文", out)
        ctx = get_last_rag_context(clear=False) or {}
        sources = ctx.get("web_sources") or []
        self.assertEqual(sources[0]["url"], "https://example.com/")

    def test_failure(self):
        tool = web_search_tool.make_fetch_url_tool(prefer_async=True)
        page = {"ok": False, "title": "", "text": "", "url": "https://example.com/", "error": "HTTP 500"}
        with mock.patch.object(web_search_tool, "afetch_page", mock.AsyncMock(return_value=page)):
            out = _run(tool.ainvoke({"url": "https://example.com/"}))
        self.assertTrue(out.startswith("FETCH_URL_FAILED"))

    def test_empty_url(self):
        tool = web_search_tool.make_fetch_url_tool(prefer_async=True)
        out = _run(tool.ainvoke({"url": ""}))
        self.assertEqual(out, "错误：url 为空。")


class ImageSearchAsyncShellTests(_AsyncShellTestBase):
    def test_happy_path_outputs_markdown(self):
        tool = web_search_tool.make_web_image_search_tool(prefer_async=True)
        images = [
            {
                "title": "橘猫",
                "contentUrl": "https://cdn.example/cat.jpg",
                "hostPageUrl": "https://example.com/cat",
            }
        ]
        with mock.patch.object(
            web_image_search, "_arun_image_searches", mock.AsyncMock(return_value=(images, "bocha"))
        ):
            out = _run(tool.ainvoke({"query": "橘猫 立绘"}))
        self.assertIn("![橘猫](https://cdn.example/cat.jpg)", out)
        ctx = get_last_rag_context(clear=False) or {}
        sources = ctx.get("web_sources") or []
        self.assertEqual(sources[0]["image_url"], "https://cdn.example/cat.jpg")

    def test_empty_results_message(self):
        tool = web_search_tool.make_web_image_search_tool(prefer_async=True)
        with mock.patch.object(
            web_image_search, "_arun_image_searches", mock.AsyncMock(return_value=([], "none"))
        ):
            out = _run(tool.ainvoke({"query": "不存在的图"}))
        self.assertTrue(out.startswith("WEB_IMAGE_SEARCH_NO_RESULTS"))


class MemoryAsyncShellTests(_AsyncShellTestBase):
    def _rows(self) -> list[dict]:
        return [
            {
                "fact_key": "k1",
                "fact_type": "preference",
                "subject": "回答语言",
                "content": "始终用中文",
                "why": "用户明确要求",
                "how_to_apply": "生成回答时默认中文",
                "updated_at": "2026-01-01T00:00:00",
            }
        ]

    def test_read_uses_async_backend(self):
        from app.chat.user_memory_tool import make_read_user_memory_tool

        tool = make_read_user_memory_tool(1, 2, prefer_async=True)
        with mock.patch.object(
            user_memory, "alist_user_facts", mock.AsyncMock(return_value=self._rows())
        ) as m_async, mock.patch.object(user_memory, "list_user_facts", mock.Mock()) as m_sync:
            out = _run(tool.ainvoke({"keyword": ""}))
        m_async.assert_awaited_once_with(1, 2, keyword="")
        m_sync.assert_not_called()
        self.assertIn("始终用中文", out)

    def test_read_limit_once_per_turn(self):
        from app.chat.user_memory_tool import make_read_user_memory_tool

        tool = make_read_user_memory_tool(1, 2, prefer_async=True)
        with mock.patch.object(
            user_memory, "alist_user_facts", mock.AsyncMock(return_value=self._rows())
        ):
            first = _run(tool.ainvoke({"keyword": ""}))
            second = _run(tool.ainvoke({"keyword": ""}))
        self.assertIn("始终用中文", first)
        self.assertTrue(second.startswith("TOOL_CALL_LIMIT_REACHED"))

    def test_read_error_path(self):
        from app.chat.user_memory_tool import make_read_user_memory_tool

        tool = make_read_user_memory_tool(1, 2, prefer_async=True)
        with mock.patch.object(
            user_memory, "alist_user_facts", mock.AsyncMock(side_effect=RuntimeError("db down"))
        ):
            out = _run(tool.ainvoke({"keyword": ""}))
        self.assertTrue(out.startswith("读取用户长期记忆出错"))

    def test_save_uses_async_backend(self):
        from app.chat.user_memory_tool import make_save_user_memory_tool

        tool = make_save_user_memory_tool(1, 2, prefer_async=True)
        stored = {"inserted": 1, "skipped": 0, "updated": 0}
        with mock.patch.object(
            user_memory, "astore_user_facts", mock.AsyncMock(return_value=stored)
        ) as m_async, mock.patch.object(user_memory, "store_user_facts", mock.Mock()) as m_sync:
            out = _run(
                tool.ainvoke(
                    {"subject": "回答语言", "content": "始终用中文", "type": "preference"}
                )
            )
        m_sync.assert_not_called()
        args = m_async.await_args.args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1], 2)
        self.assertEqual(args[2][0]["subject"], "回答语言")
        self.assertIn("已记住", out)

    def test_save_invalid_type_no_backend_call(self):
        from app.chat.user_memory_tool import make_save_user_memory_tool

        tool = make_save_user_memory_tool(1, 2, prefer_async=True)
        with mock.patch.object(user_memory, "astore_user_facts", mock.AsyncMock()) as m_async:
            out = _run(tool.ainvoke({"subject": "语言", "content": "用英文", "type": "决策"}))
        m_async.assert_not_awaited()
        self.assertIn("类型无效", out)

    def test_forget_uses_async_backend(self):
        from app.chat.user_memory_tool import make_forget_user_memory_tool

        tool = make_forget_user_memory_tool(1, 2, prefer_async=True)
        with mock.patch.object(
            user_memory, "adelete_user_facts", mock.AsyncMock(return_value=2)
        ) as m_async:
            out = _run(tool.ainvoke({"keyword": "Markdown"}))
        m_async.assert_awaited_once_with(1, 2, keyword="Markdown", all=False)
        self.assertIn("已删除 2 条", out)


class HistoryAsyncShellTests(_AsyncShellTestBase):
    def _turns(self) -> list[dict]:
        return [
            {
                "turn_index": 0,
                "turn_key": 11,
                "user": "你好历史原文",
                "assistant": "助手回答",
                "error": None,
            }
        ]

    def test_read_uses_async_backend(self):
        tool = history_tool.make_session_history_tools(1, 2, "sess", prefer_async=True)[0]
        with mock.patch.object(
            history_tool, "aread_path_turns", mock.AsyncMock(return_value=self._turns())
        ) as m_async, mock.patch.object(history_tool, "read_path_turns", mock.Mock()) as m_sync:
            out = _run(tool.ainvoke({}))
        m_async.assert_awaited_once_with(1, 2, "sess")
        m_sync.assert_not_called()
        self.assertIn("你好历史原文", out)
        self.assertIn("助手回答", out)

    def test_slot_limit(self):
        tool = history_tool.make_session_history_tools(1, 2, "sess", prefer_async=True)[0]
        with mock.patch.object(
            history_tool, "aread_path_turns", mock.AsyncMock(return_value=self._turns())
        ), mock.patch.object(settings, "CHAT_HISTORY_TOOL_MAX_CALLS", 1):
            first = _run(tool.ainvoke({}))
            second = _run(tool.ainvoke({}))
        self.assertIn("你好历史原文", first)
        self.assertTrue(second.startswith("TOOL_CALL_LIMIT_REACHED"))


class AttachmentAsyncShellTests(_AsyncShellTestBase):
    def _tools(self):
        tools = attachment_tools.make_session_attachment_tools(1, 2, "sess", prefer_async=True)
        return {t.name: t for t in tools}

    def test_read_uses_async_backend(self):
        tool = self._tools()["read_session_attachment"]
        with mock.patch.object(
            attachment_tools, "aread_attachment_text", mock.AsyncMock(return_value="正文ABC")
        ) as m_async, mock.patch.object(attachment_tools, "read_attachment_text", mock.Mock()) as m_sync:
            out = _run(tool.ainvoke({"attachment_id": "aid1", "max_chars": 12000}))
        m_sync.assert_not_called()
        m_async.assert_awaited_once_with(
            "aid1", user_id=1, agent_id=2, session_id="sess", max_chars=12000
        )
        self.assertEqual(out, "正文ABC")

    def test_read_empty_id(self):
        tool = self._tools()["read_session_attachment"]
        out = _run(tool.ainvoke({"attachment_id": ""}))
        self.assertEqual(out, "错误：attachment_id 为空。")

    def test_search_uses_async_backend(self):
        tool = self._tools()["search_session_attachment"]
        with mock.patch.object(
            attachment_tools, "asearch_attachment_text_bm25", mock.AsyncMock(return_value="BM25 命中")
        ) as m_async, mock.patch.object(
            attachment_tools, "search_attachment_text_bm25", mock.Mock()
        ) as m_sync:
            out = _run(tool.ainvoke({"attachment_id": "aid1", "query": "量子西瓜"}))
        m_sync.assert_not_called()
        kwargs = m_async.await_args.kwargs
        self.assertEqual(kwargs["user_id"], 1)
        self.assertEqual(kwargs["top_k"], 5)
        self.assertEqual(kwargs["max_snippet_chars"], 800)
        self.assertEqual(out, "BM25 命中")

    def test_list_uses_async_backend(self):
        tool = self._tools()["list_session_attachments_brief"]
        with mock.patch.object(
            attachment_tools, "aformat_attachment_hint", mock.AsyncMock(return_value="附件列表内容")
        ) as m_async, mock.patch.object(
            attachment_tools, "format_attachment_hint", mock.Mock()
        ) as m_sync:
            out = _run(tool.ainvoke({}))
        m_sync.assert_not_called()
        m_async.assert_awaited_once_with(1, 2, "sess")
        self.assertEqual(out, "附件列表内容")

    def test_list_empty_default_message(self):
        tool = self._tools()["list_session_attachments_brief"]
        with mock.patch.object(
            attachment_tools, "aformat_attachment_hint", mock.AsyncMock(return_value="")
        ):
            out = _run(tool.ainvoke({}))
        self.assertEqual(out, "本会话暂无附件。")


if __name__ == "__main__":
    unittest.main()