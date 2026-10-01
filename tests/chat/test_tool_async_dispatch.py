"""双路注册（prefer_async）：coroutine/func 正确置位、名称与描述两路一致。

覆盖 P2 六组工具工厂：web_search / fetch_url / web_image_search /
user_memory×3 / session_history / session_attachment×3。
纯构造逻辑，不执行工具、不打外网。
"""

from __future__ import annotations

import unittest

from app.chat.attachment_tools import make_session_attachment_tools
from app.chat.history_tool import make_session_history_tools
from app.chat.user_memory_tool import make_user_memory_tools
from app.chat.web_search_tool import (
    make_fetch_url_tool,
    make_web_image_search_tool,
    make_web_search_tool,
)


def _assert_async_registered(tc: unittest.TestCase, tool) -> None:
    tc.assertIsNotNone(getattr(tool, "coroutine", None), f"{tool.name} 应注册 coroutine")
    tc.assertIsNone(getattr(tool, "func", None), f"{tool.name} 异步路不应有同步 func")


def _assert_sync_registered(tc: unittest.TestCase, tool) -> None:
    tc.assertIsNotNone(getattr(tool, "func", None), f"{tool.name} 应注册同步 func")
    tc.assertIsNone(getattr(tool, "coroutine", None), f"{tool.name} 同步路不应有 coroutine")


class WebToolDispatchTests(unittest.TestCase):
    def test_web_search_dual_path(self):
        async_tool = make_web_search_tool(prefer_async=True)
        sync_tool = make_web_search_tool(prefer_async=False)
        self.assertEqual(async_tool.name, "web_search")
        self.assertEqual(sync_tool.name, "web_search")
        self.assertEqual(async_tool.description, sync_tool.description)
        # 两路各自定义局部参数模型，类对象不同但字段集合须一致
        self.assertEqual(
            set(async_tool.args_schema.model_fields), set(sync_tool.args_schema.model_fields)
        )
        _assert_async_registered(self, async_tool)
        _assert_sync_registered(self, sync_tool)

    def test_fetch_url_dual_path(self):
        async_tool = make_fetch_url_tool(prefer_async=True)
        sync_tool = make_fetch_url_tool(prefer_async=False)
        self.assertEqual(async_tool.name, "fetch_url")
        self.assertEqual(async_tool.description, sync_tool.description)
        _assert_async_registered(self, async_tool)
        _assert_sync_registered(self, sync_tool)

    def test_web_image_search_dual_path(self):
        async_tool = make_web_image_search_tool(prefer_async=True)
        sync_tool = make_web_image_search_tool(prefer_async=False)
        self.assertEqual(async_tool.name, "web_image_search")
        self.assertEqual(async_tool.description, sync_tool.description)
        _assert_async_registered(self, async_tool)
        _assert_sync_registered(self, sync_tool)

    def test_default_is_sync(self):
        self.assertIsNone(make_web_search_tool().coroutine)
        self.assertIsNone(make_fetch_url_tool().coroutine)
        self.assertIsNone(make_web_image_search_tool().coroutine)


class MemoryToolDispatchTests(unittest.TestCase):
    def test_three_tools_dual_path(self):
        async_tools = make_user_memory_tools(1, 2, prefer_async=True)
        sync_tools = make_user_memory_tools(1, 2, prefer_async=False)
        expected = {"read_user_memory", "save_user_memory", "forget_user_memory"}
        self.assertEqual({t.name for t in async_tools}, expected)
        self.assertEqual({t.name for t in sync_tools}, expected)
        for tool in async_tools:
            _assert_async_registered(self, tool)
        for tool in sync_tools:
            _assert_sync_registered(self, tool)
        # 两条路同工具的 name→description 一致
        async_desc = {t.name: t.description for t in async_tools}
        sync_desc = {t.name: t.description for t in sync_tools}
        self.assertEqual(async_desc, sync_desc)

    def test_default_is_sync(self):
        for tool in make_user_memory_tools(1, 2):
            _assert_sync_registered(self, tool)


class HistoryToolDispatchTests(unittest.TestCase):
    def test_dual_path(self):
        async_tool = make_session_history_tools(1, 2, "sess", prefer_async=True)[0]
        sync_tool = make_session_history_tools(1, 2, "sess", prefer_async=False)[0]
        self.assertEqual(async_tool.name, "read_session_history")
        self.assertEqual(async_tool.description, sync_tool.description)
        _assert_async_registered(self, async_tool)
        _assert_sync_registered(self, sync_tool)


class AttachmentToolDispatchTests(unittest.TestCase):
    def test_three_tools_dual_path(self):
        async_tools = make_session_attachment_tools(1, 2, "sess", prefer_async=True)
        sync_tools = make_session_attachment_tools(1, 2, "sess", prefer_async=False)
        expected = {
            "search_session_attachment",
            "read_session_attachment",
            "list_session_attachments_brief",
        }
        self.assertEqual({t.name for t in async_tools}, expected)
        self.assertEqual({t.name for t in sync_tools}, expected)
        for tool in async_tools:
            _assert_async_registered(self, tool)
        for tool in sync_tools:
            _assert_sync_registered(self, tool)
        async_desc = {t.name: t.description for t in async_tools}
        sync_desc = {t.name: t.description for t in sync_tools}
        self.assertEqual(async_desc, sync_desc)


if __name__ == "__main__":
    unittest.main()