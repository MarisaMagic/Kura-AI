"""
轮内 micro-compact 与原文翻牌工具单测。
覆盖：白名单边界、保留最近 N 条、tool_call_id 配对不变、MCP 绝不清理、
原消息对象不被就地修改、未达阈值零改动、超长结果两端保留、轮次区间/关键词翻牌。
不依赖 PostgreSQL / Milvus / Redis / 真实 LLM。
"""

from __future__ import annotations

import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.chat.history_tool import _clip, read_path_turns, render_history
from app.chat.tool_result_compact import (
    CLEARED_PLACEHOLDER,
    clear_old_tool_results,
    compact_tool_results_if_needed,
    is_compactable_tool,
)
from app.settings import settings


def _tool_call_round(idx: int, tool_name: str, body_chars: int = 100):
    """一轮 tool_call + tool_result 的消息对。"""
    ai = AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {"query": f"q{idx}"}, "id": f"call_{idx}"}],
    )
    tm = ToolMessage(
        content=f"结果{idx}" + "x" * body_chars,
        tool_call_id=f"call_{idx}",
        name=tool_name,
    )
    return [ai, tm]


def _conversation(rounds: list[tuple[str, int]], body_chars: int = 100):
    msgs = [HumanMessage(content="用户提问")]
    for i, (name, _) in enumerate(rounds):
        msgs.extend(_tool_call_round(i, name, body_chars))
    msgs.append(AIMessage(content="最终回答"))
    return msgs


class AllowlistTest(unittest.TestCase):
    def test_retrieval_tools_are_compactable(self):
        for name in (
            "search_knowledge_base",
            "search_knowledge_by_image",
            "web_search",
            "fetch_url",
            "web_image_search",
            "read_session_attachment",
            "search_session_attachment",
            "search_session_memory",
            "read_session_history",
        ):
            self.assertTrue(is_compactable_tool(name), name)

    def test_unknown_and_missing_names_not_compactable(self):
        """名字缺失或不在白名单一律不清理：宁可少清也不要误删不可重取的结果。"""
        self.assertFalse(is_compactable_tool(None))
        self.assertFalse(is_compactable_tool(""))
        self.assertFalse(is_compactable_tool("some_mcp_write_tool"))

    def test_mcp_tools_not_in_allowlist(self):
        for name in ("create_issue", "send_email", "context7_resolve-library-id", "playwright_click"):
            self.assertFalse(is_compactable_tool(name))


class ClearOldToolResultsTest(unittest.TestCase):
    def test_keeps_most_recent_n(self):
        msgs = _conversation([("web_search", 0)] * 8)
        out, cleared, clipped = clear_old_tool_results(msgs, keep_recent=5, max_result_tokens=10**6)
        self.assertEqual(cleared, 3)
        self.assertEqual(clipped, 0)
        tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_msgs), 8)
        # 最早的 3 条被清理，最近 5 条原文保留
        for tm in tool_msgs[:3]:
            self.assertEqual(tm.content, CLEARED_PLACEHOLDER)
        for tm in tool_msgs[3:]:
            self.assertNotEqual(tm.content, CLEARED_PLACEHOLDER)

    def test_fewer_than_keep_recent_untouched(self):
        msgs = _conversation([("web_search", 0)] * 3)
        out, cleared, clipped = clear_old_tool_results(msgs, keep_recent=5, max_result_tokens=10**6)
        self.assertEqual((cleared, clipped), (0, 0))
        self.assertIs(out, msgs)  # 零改动时原样返回同一对象

    def test_tool_call_id_pairing_preserved(self):
        """清理后 tool_call_id 必须与原来的 tool_call 一一对应，否则 API 直接报 400。"""
        msgs = _conversation([("web_search", 0)] * 8)
        out, _, _ = clear_old_tool_results(msgs, keep_recent=2, max_result_tokens=10**6)
        call_ids = [tc["id"] for m in out for tc in (getattr(m, "tool_calls", None) or [])]
        result_ids = [m.tool_call_id for m in out if isinstance(m, ToolMessage)]
        self.assertEqual(call_ids, result_ids)
        self.assertEqual(len(result_ids), 8)

    def test_original_messages_not_mutated(self):
        msgs = _conversation([("web_search", 0)] * 8)
        originals = [m.content for m in msgs if isinstance(m, ToolMessage)]
        clear_old_tool_results(msgs, keep_recent=2, max_result_tokens=10**6)
        after = [m.content for m in msgs if isinstance(m, ToolMessage)]
        self.assertEqual(originals, after)
        self.assertNotIn(CLEARED_PLACEHOLDER, after)

    def test_non_allowlisted_tools_never_cleared(self):
        msgs = _conversation([("send_email", 0)] * 8)
        out, cleared, clipped = clear_old_tool_results(msgs, keep_recent=1, max_result_tokens=10**6)
        self.assertEqual((cleared, clipped), (0, 0))
        self.assertIs(out, msgs)

    def test_mixed_only_allowlisted_cleared(self):
        msgs = [HumanMessage(content="q")]
        msgs += _tool_call_round(0, "web_search", 100)
        msgs += _tool_call_round(1, "send_email", 100)
        msgs += _tool_call_round(2, "fetch_url", 100)
        msgs += _tool_call_round(3, "create_issue", 100)
        msgs += _tool_call_round(4, "search_knowledge_base", 100)
        out, cleared, _ = clear_old_tool_results(msgs, keep_recent=1, max_result_tokens=10**6)
        by_id = {m.tool_call_id: m.content for m in out if isinstance(m, ToolMessage)}
        # 白名单内 3 条，保留最近 1 条 → 清理 2 条；非白名单永不清理
        self.assertEqual(cleared, 2)
        self.assertEqual(by_id["call_0"], CLEARED_PLACEHOLDER)  # web_search（旧）
        self.assertEqual(by_id["call_2"], CLEARED_PLACEHOLDER)  # fetch_url（旧）
        self.assertNotEqual(by_id["call_1"], CLEARED_PLACEHOLDER)  # send_email 非白名单
        self.assertNotEqual(by_id["call_3"], CLEARED_PLACEHOLDER)  # create_issue 非白名单
        self.assertNotEqual(by_id["call_4"], CLEARED_PLACEHOLDER)  # 最近 1 条白名单结果保留

    def test_tool_name_falls_back_to_tool_call_lookup(self):
        """部分厂商不回传 ToolMessage.name，须由 AIMessage.tool_calls 反查。"""
        ai = AIMessage(content="", tool_calls=[{"name": "web_search", "args": {}, "id": "c1"}])
        tm = ToolMessage(content="x" * 50, tool_call_id="c1")  # 无 name
        msgs = [HumanMessage(content="q")]
        for i in range(6):
            msgs.append(
                AIMessage(content="", tool_calls=[{"name": "web_search", "args": {}, "id": f"c{i}"}])
            )
            msgs.append(ToolMessage(content="x" * 50, tool_call_id=f"c{i}"))
        msgs += [ai, tm]
        out, cleared, _ = clear_old_tool_results(msgs, keep_recent=2, max_result_tokens=10**6)
        self.assertGreater(cleared, 0)

    def test_oversized_recent_result_clipped_head_and_tail(self):
        msgs = _conversation([("fetch_url", 0)], body_chars=40000)
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            out, cleared, clipped = clear_old_tool_results(
                msgs, keep_recent=5, max_result_tokens=2000
            )
        self.assertEqual(cleared, 0)
        self.assertEqual(clipped, 1)
        body = [m for m in out if isinstance(m, ToolMessage)][0].content
        self.assertIn("中段已省略", body)
        self.assertTrue(body.startswith("结果0"))
        self.assertLess(len(body), 40000)

    def test_no_tool_messages_untouched(self):
        msgs = [HumanMessage(content="q"), AIMessage(content="a")]
        out, cleared, clipped = clear_old_tool_results(msgs, keep_recent=5, max_result_tokens=1000)
        self.assertEqual((cleared, clipped), (0, 0))
        self.assertIs(out, msgs)

    def test_empty_input(self):
        self.assertEqual(clear_old_tool_results([], keep_recent=5, max_result_tokens=1000), ([], 0, 0))


class TriggerGateTest(unittest.TestCase):
    def test_below_threshold_zero_change(self):
        msgs = _conversation([("web_search", 0)] * 8, body_chars=50)
        with mock.patch.object(settings, "CHAT_MICROCOMPACT_ENABLED", True), mock.patch.object(
            settings, "CHAT_MICROCOMPACT_TRIGGER_RATIO", 0.9
        ):
            out, cleared, clipped = compact_tool_results_if_needed(msgs, context_window=1_000_000)
        self.assertEqual((cleared, clipped), (0, 0))
        self.assertIs(out, msgs)

    def test_above_threshold_clears(self):
        msgs = _conversation([("web_search", 0)] * 8, body_chars=4000)
        with mock.patch.object(settings, "CHAT_MICROCOMPACT_ENABLED", True), mock.patch.object(
            settings, "CHAT_MICROCOMPACT_TRIGGER_RATIO", 0.05
        ), mock.patch.object(settings, "CHAT_MICROCOMPACT_KEEP_RECENT", 2), mock.patch.object(
            settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0
        ):
            out, cleared, _ = compact_tool_results_if_needed(msgs, context_window=8000)
        self.assertEqual(cleared, 6)
        self.assertIsNot(out, msgs)

    def test_disabled_by_config(self):
        msgs = _conversation([("web_search", 0)] * 8, body_chars=4000)
        with mock.patch.object(settings, "CHAT_MICROCOMPACT_ENABLED", False), mock.patch.object(
            settings, "CHAT_MICROCOMPACT_TRIGGER_RATIO", 0.01
        ):
            out, cleared, clipped = compact_tool_results_if_needed(msgs, context_window=8000)
        self.assertEqual((cleared, clipped), (0, 0))
        self.assertIs(out, msgs)

    def test_empty_messages(self):
        out, cleared, clipped = compact_tool_results_if_needed([], context_window=8000)
        self.assertEqual((cleared, clipped), (0, 0))
        self.assertEqual(out, [])


class ClipTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_clip("abc", 100), "abc")

    def test_long_text_keeps_both_ends(self):
        s = "头" * 100 + "中" * 1000 + "尾" * 100
        out = _clip(s, 400)
        self.assertIn("头", out)
        self.assertIn("尾", out)
        self.assertIn("中段已省略", out)
        self.assertLess(len(out), len(s))


def _rec(mid: int, mtype: str, content: str, error: str | None = None):
    return {"message_id": mid, "type": mtype, "content": content, "error_text": error}


class RenderHistoryTest(unittest.TestCase):
    TURNS = [
        {"turn_index": 0, "turn_key": 1, "user": "第一问：数据库 schema", "assistant": "第一答", "error": None},
        {"turn_index": 1, "turn_key": 3, "user": "第二问：JWT 方案", "assistant": "第二答", "error": None},
        {"turn_index": 2, "turn_key": 5, "user": "第三问：缓存策略", "assistant": "第三答", "error": "超时"},
    ]

    def test_range_inclusive(self):
        out = render_history(self.TURNS, from_turn=0, to_turn=1)
        self.assertIn("轮次 0~1", out)
        self.assertIn("第一问", out)
        self.assertIn("第二问", out)
        self.assertNotIn("第三问", out)

    def test_no_range_returns_all(self):
        out = render_history(self.TURNS)
        for kw in ("第一问", "第二问", "第三问"):
            self.assertIn(kw, out)

    def test_invalid_range_message(self):
        out = render_history(self.TURNS, from_turn=9, to_turn=10)
        self.assertIn("轮次区间无效", out)
        self.assertIn("共 3 轮", out)

    def test_keyword_mode(self):
        out = render_history(self.TURNS, keyword="jwt")
        self.assertIn("命中 1 轮", out)
        self.assertIn("第二问", out)
        self.assertNotIn("第一问", out)

    def test_keyword_case_insensitive_and_miss(self):
        self.assertIn("第二问", render_history(self.TURNS, keyword="JWT"))
        out = render_history(self.TURNS, keyword="不存在的词")
        self.assertIn("未在本会话原文中命中", out)

    def test_keyword_hits_capped(self):
        turns = [
            {"turn_index": i, "turn_key": i, "user": f"重复词 {i}", "assistant": "a", "error": None}
            for i in range(30)
        ]
        out = render_history(turns, keyword="重复词")
        self.assertIn("命中 30 轮", out)
        self.assertIn("仅展示最近", out)

    def test_error_turn_marked(self):
        out = render_history(self.TURNS, from_turn=2, to_turn=2)
        self.assertIn("该轮生成失败", out)
        self.assertIn("超时", out)

    def test_empty_turns(self):
        self.assertIn("暂无可翻阅", render_history([]))

    def test_long_turns_share_budget(self):
        """轮数多时每轮均摊预算，保证区间内每轮都出现而不是只给前几轮。"""
        turns = [
            {"turn_index": i, "turn_key": i, "user": "问" * 5000, "assistant": "答" * 5000, "error": None}
            for i in range(20)
        ]
        out = render_history(turns, from_turn=0, to_turn=19, max_chars=4000)
        self.assertIn("轮次 0", out)
        self.assertIn("轮次 19", out)


class ReadPathTurnsTest(unittest.TestCase):
    def test_groups_by_human_and_pairs_assistant(self):
        records = [
            _rec(1, "human", "问1"),
            _rec(2, "ai", "答1"),
            _rec(3, "human", "问2"),
            _rec(4, "ai", "答2", error="boom"),
        ]
        with mock.patch(
            "app.chat.history_tool.storage.get_session_messages", return_value=records
        ):
            turns = read_path_turns(1, 2, "s")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["turn_index"], 0)
        self.assertEqual(turns[0]["turn_key"], 1)
        self.assertEqual(turns[0]["user"], "问1")
        self.assertEqual(turns[0]["assistant"], "答1")
        self.assertIsNone(turns[0]["error"])
        self.assertEqual(turns[1]["turn_index"], 1)
        self.assertEqual(turns[1]["error"], "boom")

    def test_leading_ai_without_human_ignored(self):
        records = [_rec(1, "ai", "孤儿答"), _rec(2, "human", "问"), _rec(3, "ai", "答")]
        with mock.patch(
            "app.chat.history_tool.storage.get_session_messages", return_value=records
        ):
            turns = read_path_turns(1, 2, "s")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["user"], "问")

    def test_empty_session(self):
        with mock.patch("app.chat.history_tool.storage.get_session_messages", return_value=[]):
            self.assertEqual(read_path_turns(1, 2, "s"), [])


class HistoryToolSlotTest(unittest.TestCase):
    def test_slot_limits_calls_per_turn(self):
        from app.chat.tools import reset_tool_call_guards, try_acquire_history_tool_slot

        reset_tool_call_guards()
        self.assertTrue(try_acquire_history_tool_slot(2))
        self.assertTrue(try_acquire_history_tool_slot(2))
        self.assertFalse(try_acquire_history_tool_slot(2))
        reset_tool_call_guards()
        self.assertTrue(try_acquire_history_tool_slot(2))

    def test_history_slot_independent_from_memory_slot(self):
        from app.chat.tools import (
            reset_tool_call_guards,
            try_acquire_history_tool_slot,
            try_acquire_memory_tool_slot,
        )

        reset_tool_call_guards()
        self.assertTrue(try_acquire_memory_tool_slot())
        self.assertFalse(try_acquire_memory_tool_slot())
        # 记忆槽用尽不影响原文翻牌槽
        self.assertTrue(try_acquire_history_tool_slot(2))

    def test_tool_is_registered_with_expected_name(self):
        from app.chat.history_tool import make_session_history_tools

        tools = make_session_history_tools(1, 2, "s")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "read_session_history")
        self.assertIn("逐字原文", tools[0].description)
        self.assertIn("search_session_memory", tools[0].description)

    def test_tool_returns_limit_message_when_slot_exhausted(self):
        from app.chat.history_tool import make_session_history_tools
        from app.chat.tools import reset_tool_call_guards

        reset_tool_call_guards()
        tool = make_session_history_tools(1, 2, "s")[0]
        with mock.patch("app.chat.history_tool.try_acquire_history_tool_slot", return_value=False):
            out = tool.func(from_turn=0, to_turn=1)
        self.assertIn("TOOL_CALL_LIMIT_REACHED", out)


if __name__ == "__main__":
    unittest.main()
