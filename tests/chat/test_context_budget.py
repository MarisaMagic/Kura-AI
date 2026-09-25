"""
上下文 token 预算纯逻辑单测：CJK 感知估算、usage 校准、窗口阈值、工具 schema 实测。
不依赖 PostgreSQL / Milvus / Redis / 真实 LLM。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.chat.context_budget import (
    apply_factor,
    budget_for,
    calibrate_factor,
    clear_tools_cache,
    count_cjk,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tokens_blind,
    extract_input_tokens,
    factor_of,
    resolve_window,
    tools_schema_tokens,
)
from app.settings import settings
from langchain_core.messages import AIMessage, HumanMessage


class CjkCountTest(unittest.TestCase):
    def test_counts_han_kana_hangul_fullwidth(self):
        self.assertEqual(count_cjk("中文测试"), 4)
        self.assertEqual(count_cjk("ひらがな"), 4)
        self.assertEqual(count_cjk("한글"), 2)
        self.assertEqual(count_cjk("ＡＢ"), 2)  # 全角
        self.assertEqual(count_cjk("abc123"), 0)

    def test_mixed(self):
        # 用 / 替 / 换 三个汉字，其余为 latin 与空格
        self.assertEqual(count_cjk("用 JWT 替换 session"), 3)


class EstimateTokensTest(unittest.TestCase):
    def test_pure_cjk_near_one_token_per_char(self):
        text = "中" * 1000
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            self.assertEqual(estimate_tokens(text), 1000)

    def test_pure_latin_uses_chars_per_token(self):
        text = "a" * 3600
        with mock.patch.object(settings, "CHAT_CHARS_PER_LATIN_TOKEN", 3.6):
            self.assertEqual(estimate_tokens(text), 1000)

    def test_empty_and_none(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)

    def test_cjk_costs_more_than_same_length_latin(self):
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0), mock.patch.object(
            settings, "CHAT_CHARS_PER_LATIN_TOKEN", 3.6
        ):
            self.assertGreater(estimate_tokens("中" * 500), estimate_tokens("a" * 500))

    def test_blind_estimate_between_extremes(self):
        """只知字符数时的估算应落在「全 CJK」与「全 latin」之间。"""
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0), mock.patch.object(
            settings, "CHAT_CHARS_PER_LATIN_TOKEN", 4.0
        ):
            blind = estimate_tokens_blind(4000)
            self.assertLess(blind, 4000)  # 低于全 CJK
            self.assertGreater(blind, 1000)  # 高于全 latin


class MessagesTokensTest(unittest.TestCase):
    def test_sums_all_messages(self):
        msgs = [HumanMessage(content="中" * 100), AIMessage(content="中" * 200)]
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            self.assertEqual(estimate_messages_tokens(msgs), 300)

    def test_factor_multiplies(self):
        msgs = [HumanMessage(content="中" * 100)]
        with mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0):
            self.assertEqual(estimate_messages_tokens(msgs, factor=1.5), 150)

    def test_tool_calls_counted(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge_base", "args": {"query": "x" * 400}, "id": "1"}],
        )
        self.assertGreater(estimate_messages_tokens([msg]), 0)


class BudgetTest(unittest.TestCase):
    def test_double_buffer_below_window(self):
        """对齐 Claude Code：trigger = window - 摘要预留 - 安全缓冲。"""
        b = budget_for(200_000)
        with mock.patch.object(settings, "CHAT_COMPACT_SUMMARY_RESERVE_TOKENS", 20_000), mock.patch.object(
            settings, "CHAT_COMPACT_BUFFER_TOKENS", 13_000
        ):
            b = budget_for(200_000)
            self.assertEqual(b.effective, 180_000)
            self.assertEqual(b.trigger, 167_000)

    def test_keep_tokens_much_smaller_than_trigger(self):
        b = budget_for(128_000)
        self.assertLess(b.keep_tokens, b.trigger)
        self.assertLess(b.soft_trigger, b.trigger)

    def test_soft_trigger_is_ratio_of_effective(self):
        with mock.patch.object(settings, "CHAT_COMPACT_PRECOMPACT_RATIO", 0.6):
            b = budget_for(100_000)
            self.assertEqual(b.soft_trigger, int(b.effective * 0.6))

    def test_small_window_clamps_reserve_and_buffer(self):
        """小窗口模型下预留/缓冲被压到窗口的 1/4 以内，避免 trigger 变负。"""
        b = budget_for(8_000)
        self.assertLessEqual(b.summary_reserve, 8_000 // 4)
        self.assertLessEqual(b.buffer, 8_000 // 4)
        self.assertGreater(b.trigger, b.keep_tokens)

    def test_none_window_uses_global_default(self):
        with mock.patch.object(settings, "CHAT_MODEL_CONTEXT_WINDOW_DEFAULT", 64_000):
            self.assertEqual(budget_for(None).window, 64_000)
            self.assertEqual(resolve_window(None), 64_000)
            self.assertEqual(resolve_window(0), 64_000)
            self.assertEqual(resolve_window("垃圾"), 64_000)

    def test_explicit_window_wins(self):
        self.assertEqual(resolve_window(32_768), 32_768)


class CalibrationTest(unittest.TestCase):
    def test_first_sample_seeds_factor(self):
        """首个样本从基线 1.0 起 EWMA（alpha=0.4），不直接跳到单点样本，避免被一次异常值带偏。"""
        out = calibrate_factor(None, estimated=1000, input_tokens=1500, model="m1")
        self.assertEqual(out["model"], "m1")
        self.assertAlmostEqual(out["factor"], 1.0 * 0.6 + 1.5 * 0.4, places=2)
        self.assertEqual(out["samples"], 1)
        self.assertEqual(out["last_input_tokens"], 1500)

    def test_converges_to_true_ratio(self):
        """同一真实比例反复喂入，系数应收敛到该比例附近。"""
        cur = None
        for _ in range(12):
            cur = calibrate_factor(cur, estimated=1000, input_tokens=1800, model="m1")
        self.assertAlmostEqual(cur["factor"], 1.8, delta=0.05)

    def test_ewma_smooths_toward_new_sample(self):
        prev = {"model": "m1", "factor": 1.0, "samples": 1}
        out = calibrate_factor(prev, estimated=1000, input_tokens=2000, model="m1")
        self.assertGreater(out["factor"], 1.0)
        self.assertLess(out["factor"], 2.0)

    def test_model_switch_resets(self):
        prev = {"model": "m1", "factor": 1.9, "samples": 8}
        out = calibrate_factor(prev, estimated=1000, input_tokens=1000, model="m2")
        self.assertEqual(out["model"], "m2")
        self.assertLess(out["factor"], 1.9)

    def test_outlier_sample_rejected(self):
        prev = {"model": "m1", "factor": 1.0, "samples": 3}
        self.assertEqual(calibrate_factor(prev, estimated=1000, input_tokens=99_000, model="m1"), prev)
        self.assertEqual(calibrate_factor(prev, estimated=1000, input_tokens=1, model="m1"), prev)

    def test_bad_input_keeps_prev(self):
        prev = {"model": "m1", "factor": 1.2, "samples": 2}
        self.assertEqual(calibrate_factor(prev, estimated=0, input_tokens=100, model="m1"), prev)
        self.assertEqual(calibrate_factor(prev, estimated=100, input_tokens=0, model="m1"), prev)

    def test_factor_clamped(self):
        out = calibrate_factor(None, estimated=100, input_tokens=240, model="m1")
        self.assertLessEqual(out["factor"], 2.5)

    def test_samples_capped(self):
        cur = None
        for _ in range(30):
            cur = calibrate_factor(cur, estimated=1000, input_tokens=1400, model="m1")
        self.assertLessEqual(cur["samples"], 8)

    def test_factor_of_requires_model_match(self):
        calib = {"model": "m1", "factor": 1.4, "samples": 3}
        self.assertEqual(factor_of(calib, "m1"), 1.4)
        self.assertEqual(factor_of(calib, "m2"), 1.0)
        self.assertEqual(factor_of(None, "m1"), 1.0)
        self.assertEqual(factor_of("坏数据", "m1"), 1.0)

    def test_apply_factor(self):
        self.assertEqual(apply_factor(100, 1.5), 150)
        self.assertEqual(apply_factor(0, 1.5), 0)
        self.assertEqual(apply_factor(100, 0), 100)  # 非法系数按 1.0


class ExtractUsageTest(unittest.TestCase):
    def test_dict_shapes(self):
        self.assertEqual(extract_input_tokens({"input_tokens": 42}), 42)
        self.assertEqual(extract_input_tokens({"prompt_tokens": 43}), 43)
        self.assertEqual(extract_input_tokens({"total_tokens": 44}), 44)
        self.assertEqual(extract_input_tokens({}), 0)

    def test_object_shape(self):
        class _U:
            input_tokens = 55

        self.assertEqual(extract_input_tokens(_U()), 55)

    def test_none_and_empty(self):
        self.assertEqual(extract_input_tokens(None), 0)


class ToolsSchemaTokensTest(unittest.TestCase):
    def setUp(self):
        clear_tools_cache()

    def tearDown(self):
        clear_tools_cache()

    def test_empty_tools_zero(self):
        self.assertEqual(tools_schema_tokens([]), 0)
        self.assertEqual(tools_schema_tokens(None), 0)

    def test_measures_and_caches(self):
        from langchain_core.tools import StructuredTool

        def _f(query: str) -> str:
            """检索知识库内容。"""
            return query

        tool = StructuredTool.from_function(name="search_kb", description="检索" * 200, func=_f)
        first = tools_schema_tokens([tool])
        second = tools_schema_tokens([tool])
        self.assertGreater(first, 0)
        self.assertEqual(first, second)  # 命中缓存

    def test_longer_description_costs_more(self):
        from langchain_core.tools import StructuredTool

        def _f(query: str) -> str:
            """x"""
            return query

        short = StructuredTool.from_function(name="t", description="短", func=_f)
        long_ = StructuredTool.from_function(name="t", description="很长的描述" * 500, func=_f)
        self.assertGreater(tools_schema_tokens([long_]), tools_schema_tokens([short]))


if __name__ == "__main__":
    unittest.main()
