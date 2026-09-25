"""
压缩摘要提示词与解析单测：9 段清单、<analysis>/<summary> 契约、禁止工具调用、
被移出对话的截断方向（丢最旧保最新）。不打真实 LLM。
"""

from __future__ import annotations

import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage

from app.chat.compact import (
    CONTINUATION_LEAD,
    _SUMMARY_SECTIONS,
    build_summary_prompt,
    parse_summary_output,
    prepare_dropped_text,
    render_summary_block,
)
from app.settings import settings


def _turn(idx: int, body_chars: int = 100) -> list:
    return [
        HumanMessage(content=f"第{idx}轮用户问题" + "问" * body_chars),
        AIMessage(content=f"第{idx}轮助手回答" + "答" * body_chars),
    ]


class SummaryPromptTest(unittest.TestCase):
    def test_nine_sections_present_in_order(self):
        for i, name in enumerate(
            [
                "Primary Request and Intent",
                "Key Technical Concepts",
                "Files, Attachments and Sources",
                "Errors and fixes",
                "Problem Solving",
                "All user messages",
                "Pending Tasks",
                "Current Work",
                "Optional Next Step",
            ],
            1,
        ):
            self.assertIn(f"{i}. {name}", _SUMMARY_SECTIONS)

    def test_analysis_summary_contract_declared(self):
        p = build_summary_prompt(old_summary="", dropped_text="x", max_chars=4000)
        self.assertIn("<analysis>", p)
        self.assertIn("</analysis>", p)
        self.assertIn("<summary>", p)
        self.assertIn("</summary>", p)

    def test_no_tool_warning_sandwiched_head_and_tail(self):
        """早期模型会无视单次警告去调工具，故首尾各写一遍。"""
        p = build_summary_prompt(old_summary="", dropped_text="x", max_chars=4000)
        self.assertGreaterEqual(p.count("禁止调用任何工具"), 2)
        self.assertTrue(p.strip().startswith("你是会话压缩助手"))
        self.assertIn("禁止调用任何工具", p.strip()[-400:])

    def test_auto_mode_suppresses_followup_questions(self):
        p = build_summary_prompt(
            old_summary="", dropped_text="x", max_chars=4000, suppress_follow_up=True
        )
        self.assertIn("用户不在场", p)
        self.assertIn("Pending Tasks", p)
        p2 = build_summary_prompt(
            old_summary="", dropped_text="x", max_chars=4000, suppress_follow_up=False
        )
        self.assertNotIn("用户不在场", p2)

    def test_old_summary_and_dropped_inlined(self):
        p = build_summary_prompt(old_summary="旧摘要ABC", dropped_text="被移出内容XYZ", max_chars=4000)
        self.assertIn("旧摘要ABC", p)
        self.assertIn("被移出内容XYZ", p)

    def test_empty_inputs_rendered_as_placeholder(self):
        p = build_summary_prompt(old_summary="", dropped_text="", max_chars=4000)
        self.assertIn("（无）", p)

    def test_max_chars_injected(self):
        p = build_summary_prompt(old_summary="", dropped_text="x", max_chars=6000)
        self.assertIn("6000", p)


class ParseSummaryOutputTest(unittest.TestCase):
    def test_strips_analysis_keeps_summary(self):
        raw = "<analysis>我在想哪些重要</analysis>\n<summary>\n1. Primary Request and Intent\n- 用户要登录\n</summary>"
        summary = parse_summary_output(raw)
        self.assertNotIn("我在想", summary)
        self.assertIn("用户要登录", summary)

    def test_unclosed_summary_takes_rest(self):
        summary = parse_summary_output("<analysis>草稿</analysis><summary>正文没有闭合标签")
        self.assertEqual(summary, "正文没有闭合标签")

    def test_no_tags_falls_back_to_whole_text(self):
        summary = parse_summary_output("模型没按格式输出，直接给了摘要正文")
        self.assertIn("摘要正文", summary)

    def test_strips_markdown_fence(self):
        summary = parse_summary_output("<summary>\n```markdown\n正文\n```\n</summary>")
        self.assertEqual(summary, "正文")
        self.assertNotIn("```", summary)

    def test_empty_input(self):
        self.assertEqual(parse_summary_output(""), "")
        self.assertEqual(parse_summary_output(None), "")


class DroppedTextTruncationTest(unittest.TestCase):
    def test_keeps_newest_when_over_budget(self):
        """旧实现 dropped[:N] 会截掉最新的被挤出轮次，此处方向必须相反。"""
        turns = [_turn(i, 400) for i in range(10)]
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            text = prepare_dropped_text(turns, max_tokens=2000, turn_offset=0)
        self.assertIn("第9轮", text)
        self.assertIn("第8轮", text)
        self.assertNotIn("第0轮", text)

    def test_omission_note_reports_dropped_count(self):
        turns = [_turn(i, 400) for i in range(10)]
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            text = prepare_dropped_text(turns, max_tokens=2000, turn_offset=0)
        self.assertIn("未纳入", text)

    def test_within_budget_keeps_everything(self):
        turns = [_turn(i, 20) for i in range(4)]
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            text = prepare_dropped_text(turns, max_tokens=100_000, turn_offset=0)
        for i in range(4):
            self.assertIn(f"第{i}轮", text)
        self.assertNotIn("未纳入", text)

    def test_turn_offset_labels_are_absolute(self):
        turns = [_turn(0, 10), _turn(1, 10)]
        text = prepare_dropped_text(turns, max_tokens=100_000, turn_offset=7)
        self.assertIn("轮次 7", text)
        self.assertIn("轮次 8", text)

    def test_chronological_order_newest_last(self):
        turns = [_turn(i, 10) for i in range(4)]
        text = prepare_dropped_text(turns, max_tokens=100_000, turn_offset=0)
        self.assertLess(text.index("轮次 0"), text.index("轮次 3"))

    def test_single_huge_turn_keeps_head_and_tail(self):
        turns = [[HumanMessage(content="头" * 50 + "中" * 20000 + "尾" * 50)]]
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            text = prepare_dropped_text(turns, max_tokens=1000, turn_offset=0)
        self.assertIn("头", text)
        self.assertIn("尾", text)
        self.assertIn("本轮中段已省略", text)
        self.assertLess(len(text), 20000)

    def test_empty_turns(self):
        self.assertEqual(prepare_dropped_text([], max_tokens=1000), "")


class RenderSummaryBlockTest(unittest.TestCase):
    def test_single_segment_no_range_header(self):
        out = render_summary_block([{"from_index": 0, "to_index": 5, "summary": "摘要A"}])
        self.assertIn(CONTINUATION_LEAD, out)
        self.assertIn("摘要A", out)

    def test_multiple_segments_labeled_old_to_new(self):
        chain = [
            {"from_index": 0, "to_index": 5, "summary": "早段"},
            {"from_index": 5, "to_index": 9, "summary": "近段"},
        ]
        out = render_summary_block(chain)
        self.assertLess(out.index("早段"), out.index("近段"))
        self.assertIn("覆盖轮次 0~4", out)
        self.assertIn("覆盖轮次 5~8", out)

    def test_legacy_summary_used_when_no_chain(self):
        self.assertEqual(render_summary_block([], legacy_summary="旧摘要"), "旧摘要")
        self.assertEqual(render_summary_block([]), "")


if __name__ == "__main__":
    unittest.main()
