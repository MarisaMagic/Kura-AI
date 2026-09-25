"""
长期记忆价值闸门单测：寒暄/失败轮/超短轮/近重复拦截，代码与实体加分，偏好触发词。
纯函数，不依赖 PostgreSQL / Milvus / Redis / LLM。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.chat.memory_value import (
    dedup_window_size,
    has_code,
    has_entity,
    has_preference_trigger,
    is_greeting,
    is_near_duplicate,
    jaccard,
    passes_value_gate,
    score_turn,
    shingles,
)
from app.settings import settings


class GreetingTest(unittest.TestCase):
    def test_pure_acknowledgements_are_greetings(self):
        for t in ("好的", "好", "嗯", "嗯嗯", "哦", "谢谢", "多谢", "收到", "明白", "了解了", "继续"):
            self.assertTrue(is_greeting(t), t)

    def test_english_acks_are_greetings(self):
        for t in ("ok", "OK", "okay", "thanks", "thank you", "thx", "continue", "hi", "hello", "test"):
            self.assertTrue(is_greeting(t), t)

    def test_punctuation_variants(self):
        for t in ("好的！", "嗯嗯。", "谢谢~", "ok!", "收到，", "明白？"):
            self.assertTrue(is_greeting(t), t)

    def test_real_questions_are_not_greetings(self):
        for t in ("帮我改一下登录接口", "好的，那顺便把缓存也加上", "继续讲讲 JWT 的刷新逻辑"):
            self.assertFalse(is_greeting(t), t)

    def test_empty_is_greeting(self):
        self.assertTrue(is_greeting(""))
        self.assertTrue(is_greeting("   "))


class PreferenceTriggerTest(unittest.TestCase):
    def test_chinese_triggers(self):
        for t in ("记住我用的是 PostgreSQL", "以后都别用 mock", "我偏好简洁回答", "不要再加注释了"):
            self.assertTrue(has_preference_trigger(t), t)

    def test_english_triggers(self):
        for t in ("Always answer in Chinese", "Never use tabs", "Remember this convention"):
            self.assertTrue(has_preference_trigger(t), t)

    def test_no_trigger(self):
        self.assertFalse(has_preference_trigger("帮我看看这个函数为什么报错"))


class EntityAndCodeTest(unittest.TestCase):
    def test_code_markers(self):
        self.assertTrue(has_code("```python\nprint(1)\n```"))
        self.assertTrue(has_code("示例：\n    def foo():\n        return 1"))
        self.assertFalse(has_code("普通文本没有代码"))

    def test_entity_patterns(self):
        for t in (
            "file_key 是 abc123",
            "参见 https://example.com/doc",
            "编号 12345678",
            "改一下 app/chat/compact.py",
            "attachment_id=xyz",
        ):
            self.assertTrue(has_entity(t), t)

    def test_plain_text_has_no_entity(self):
        self.assertFalse(has_entity("我觉得这个方案不太好"))


class ScoreTurnTest(unittest.TestCase):
    def test_error_turn_scores_zero(self):
        score, reasons = score_turn("很长的问题" * 20, "很长的回答" * 20, has_error=True)
        self.assertEqual(score, 0.0)
        self.assertIn("error_turn", reasons)

    def test_empty_turn_scores_zero(self):
        self.assertEqual(score_turn("", "")[0], 0.0)

    def test_short_ack_scores_zero(self):
        score, reasons = score_turn("好的", "已完成")
        self.assertEqual(score, 0.0)
        self.assertIn("too_short", reasons)

    def test_greeting_with_long_reply_flagged(self):
        """寒暄但助手回了长内容：不算 too_short，走 greeting 分支给极低分。"""
        score, reasons = score_turn("谢谢", "这是一段相当长的收尾说明，解释了后续可以怎么继续推进这项工作的若干步骤。" * 3)
        self.assertLessEqual(score, 0.1)
        self.assertIn("greeting", reasons)

    def test_greeting_with_substantive_reply_passes_on_content(self):
        """用户只说「好的」但助手给出了实质结论时，仍应按内容价值评分。"""
        ok, score, _ = passes_value_gate(
            "好的",
            "已把触发阈值改为按 token 计算：trigger = window - 8000 - 6000，并加了连续失败 3 次的熔断。" * 4,
        )
        self.assertTrue(ok)
        self.assertGreater(score, 0.1)

    def test_substantive_turn_passes(self):
        ok, score, reasons = passes_value_gate(
            "帮我把 compact.py 的触发阈值改成按 token 计算",
            "已经改好了，主要改动在 budget_for 与 estimate_prompt_tokens 两个函数。" * 5,
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(score, 0.25)
        self.assertIn("substantial_length", reasons)

    def test_code_and_entity_boost(self):
        base = score_turn("请解释一下这个逻辑", "这是一个普通的解释性回答" * 20)[0]
        with_code = score_turn("请解释这段代码", "```python\ndef f():\n    return 1\n```" + "说明" * 100)[0]
        self.assertGreater(with_code, base)

    def test_preference_trigger_boosts_highest(self):
        normal = score_turn("帮我写个函数读取配置文件", "好的，这是实现" * 40)[0]
        pref = score_turn("记住以后都用 YAML 而不是 JSON 配置", "好的，已记下这个偏好" * 40)[0]
        self.assertGreater(pref, normal)
        self.assertLessEqual(pref, 1.0)

    def test_score_capped_at_one(self):
        score, _ = score_turn(
            "记住以后都用 YAML，参见 https://example.com/spec 和 config/settings.py，编号 12345678",
            "```yaml\na: 1\n```\n" + "详细说明" * 400,
        )
        self.assertLessEqual(score, 1.0)

    def test_gate_threshold_configurable(self):
        u, a = "帮我看看这段代码", "```python\nx=1\n```\n这段代码定义了一个变量 x。" * 10
        with mock.patch.object(settings, "CHAT_MEMORY_MIN_VALUE_SCORE", 0.99):
            self.assertFalse(passes_value_gate(u, a)[0])
        with mock.patch.object(settings, "CHAT_MEMORY_MIN_VALUE_SCORE", 0.1):
            self.assertTrue(passes_value_gate(u, a)[0])


class NearDuplicateTest(unittest.TestCase):
    def test_identical_text_is_duplicate(self):
        t = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制"
        self.assertTrue(is_near_duplicate(t, [shingles(t)]))

    def test_different_text_not_duplicate(self):
        a = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制"
        b = "知识库检索为什么总是召回不相关的图片块，应该怎么调整 chunk 大小"
        self.assertFalse(is_near_duplicate(b, [shingles(a)]))

    def test_minor_variation_still_duplicate(self):
        a = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制"
        b = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制。"
        self.assertTrue(is_near_duplicate(b, [shingles(a)]))

    def test_empty_fingerprints_never_duplicate(self):
        self.assertFalse(is_near_duplicate("任何内容都可以", []))

    def test_threshold_configurable(self):
        a = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制"
        b = "请帮我把长期记忆的写入闸门改成按价值分过滤，并且加上近重复检测"
        self.assertFalse(is_near_duplicate(b, [shingles(a)], threshold=0.85))
        # 阈值降到 0 时任何非空指纹都判为重复，证明阈值确实生效
        self.assertTrue(is_near_duplicate(b, [shingles(a)], threshold=0.0))

    def test_shingles_normalizes_whitespace_and_case(self):
        self.assertEqual(shingles("Hello World"), shingles("hello   world"))

    def test_shingles_ignore_punctuation_shift(self):
        """回归：字符 n-gram 对插入极敏感，句首多一个句号就会让其后每个 gram 错位。

        剥掉标点后指纹具备位移不变性，「同一句话多打个标点」才能被正确判为重复。
        """
        base = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制"
        for noise in ("。", "！", "，，", "？", "...", "～"):
            self.assertEqual(shingles(base), shingles(noise + base))
            self.assertEqual(shingles(base), shingles(base + noise))

    def test_shingles_keep_digits_and_letters(self):
        """标点被剥掉，但字母数字必须保留：编号/ID 是区分不同轮次的关键信号。"""
        self.assertNotEqual(shingles("工单编号 12345 已处理完成啦"), shingles("工单编号 67890 已处理完成啦"))

    def test_jaccard_bounds(self):
        self.assertEqual(jaccard(frozenset(), frozenset({"a"})), 0.0)
        self.assertEqual(jaccard(frozenset({"a"}), frozenset({"a"})), 1.0)
        self.assertAlmostEqual(jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})), 1 / 3)

    def test_dedup_window_positive(self):
        self.assertGreaterEqual(dedup_window_size(), 1)
        with mock.patch.object(settings, "CHAT_MEMORY_DEDUP_WINDOW", 7):
            self.assertEqual(dedup_window_size(), 7)
        # 0 被视为「未配置」回退默认值，再被 max(1, …) 兜住，永不为 0
        with mock.patch.object(settings, "CHAT_MEMORY_DEDUP_WINDOW", 0):
            self.assertGreaterEqual(dedup_window_size(), 1)


if __name__ == "__main__":
    unittest.main()
