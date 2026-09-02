"""
视觉 payload：压图、工具轮去图、本轮上下文并入最后一条 Human、纪律按开关拼接。
不打真实 LLM / 不启服务。
"""

from __future__ import annotations

import unittest
from io import BytesIO
from types import SimpleNamespace

from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image

from app.chat.agent_service import (
    _append_turn_context_message,
    _compose_system_prompt,
    _format_turn_context_block,
    _prepare_to_invoke_messages,
    _should_run_vision_caption,
)
from app.chat.message_codec import _prepare_vision_image_bytes, strip_image_urls_after_tools
from app.chat.vision_caption import _CAPTION_SYSTEM_PROMPT


def _png_bytes(width: int, height: int, color=(20, 80, 180)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class VisionCompressTests(unittest.TestCase):
    def test_large_png_becomes_smaller_jpeg(self):
        raw = _png_bytes(3000, 2000)
        out, mime = _prepare_vision_image_bytes(raw, mime="image/png")
        self.assertEqual(mime, "image/jpeg")
        self.assertLess(len(out), len(raw))
        compressed = Image.open(BytesIO(out))
        self.assertLessEqual(max(compressed.size), 1568)
        self.assertEqual(compressed.format, "JPEG")

    def test_bad_bytes_fallback_to_original(self):
        raw = b"not-an-image"
        out, mime = _prepare_vision_image_bytes(raw, mime="image/png")
        self.assertEqual(out, raw)
        self.assertEqual(mime, "image/png")


class StripImageAfterToolsTests(unittest.TestCase):
    def _human_with_image(self) -> HumanMessage:
        return HumanMessage(
            content=[
                {"type": "text", "text": "看这张图"},
                {
                    "type": "image_url",
                    "filename": "cat.png",
                    "image_url": {
                        "url": "data:image/jpeg;base64,xxxx",
                        "filename": "cat.png",
                    },
                },
            ]
        )

    def test_keeps_image_without_tool_message(self):
        human = self._human_with_image()
        msgs = [human, AIMessage(content="先看图")]
        out = strip_image_urls_after_tools(msgs)
        self.assertIs(out, msgs)
        self.assertEqual(out[0].content[1]["type"], "image_url")

    def test_strips_image_when_tool_present(self):
        human = self._human_with_image()
        msgs = [
            human,
            AIMessage(content=""),
            ToolMessage(content="search ok", tool_call_id="c1"),
        ]
        out = strip_image_urls_after_tools(msgs)
        self.assertIsNot(out, msgs)
        blocks = out[0].content
        self.assertTrue(all(not (isinstance(b, dict) and b.get("type") == "image_url") for b in blocks))
        texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        self.assertIn("[图片 cat.png]", texts)


class TurnContextMergeAndDisciplineTests(unittest.TestCase):
    def test_merge_into_last_human_keeps_image_block(self):
        msgs = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
                ]
            )
        ]
        out = _append_turn_context_message(msgs, "【本轮上下文】允许 web_search")
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0].content, list)
        self.assertEqual(out[0].content[1]["type"], "image_url")
        self.assertEqual(out[0].content[-1]["type"], "text")
        self.assertIn("本轮上下文", out[0].content[-1]["text"])

    def test_merge_string_human_does_not_add_second_message(self):
        out = _append_turn_context_message([HumanMessage(content="hi")], "CTX")
        self.assertEqual(len(out), 1)
        self.assertTrue(str(out[0].content).endswith("CTX"))

    def test_compose_web_search_no_must_rely_on_tools(self):
        ua = SimpleNamespace(system_prompt="人设")
        prompt = _compose_system_prompt(ua, use_web_search=True, use_knowledge_retrieval=False)
        self.assertIn("人设", prompt)
        self.assertNotIn("必须仅依据", prompt)
        self.assertIn("不必为了搜索而搜索", prompt)
        self.assertNotIn("知识库作答纪律", prompt)

    def test_compose_off_has_no_web_discipline(self):
        ua = SimpleNamespace(system_prompt="人设")
        prompt = _compose_system_prompt(ua, use_web_search=False, use_knowledge_retrieval=False)
        self.assertEqual(prompt, "人设")
        self.assertNotIn("web_search", prompt)

    def test_compose_kb_only_has_kb_discipline(self):
        ua = SimpleNamespace(system_prompt="人设")
        prompt = _compose_system_prompt(ua, use_knowledge_retrieval=True, use_web_search=False)
        self.assertIn("知识库作答纪律", prompt)
        self.assertIn("/api/v1/media/", prompt)
        self.assertNotIn("不必为了搜索而搜索", prompt)


def _ua(**over):
    base = {
        "system_prompt": "人设",
        "supports_vision": True,
        "sub_api_key_ciphertext": None,
        "sub_model_name": "",
        "sub_base_url": "",
        "api_key_ciphertext": None,
        "base_url": "",
        "model_name": "test-model",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _image_human() -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": "图里是什么"},
            {"type": "image_ref", "attachment_id": "a1", "mime": "image/png", "filename": "x.png"},
        ]
    )


class VisionCaptionGateTests(unittest.TestCase):
    def test_no_image_no_caption(self):
        msgs = [HumanMessage(content="纯文字")]
        self.assertFalse(
            _should_run_vision_caption(
                msgs, _ua(), use_knowledge_retrieval=False, use_web_search=True, has_mcp_tools=False
            )
        )

    def test_image_without_tools_no_caption(self):
        self.assertFalse(
            _should_run_vision_caption(
                [_image_human()],
                _ua(),
                use_knowledge_retrieval=False,
                use_web_search=False,
                has_mcp_tools=False,
            )
        )

    def test_image_with_web_search_runs_caption(self):
        self.assertTrue(
            _should_run_vision_caption(
                [_image_human()],
                _ua(),
                use_knowledge_retrieval=False,
                use_web_search=True,
                has_mcp_tools=False,
            )
        )

    def test_image_with_mcp_tools_runs_caption(self):
        self.assertTrue(
            _should_run_vision_caption(
                [_image_human()],
                _ua(),
                use_knowledge_retrieval=False,
                use_web_search=False,
                has_mcp_tools=True,
            )
        )

    def test_no_vision_support_no_caption(self):
        self.assertFalse(
            _should_run_vision_caption(
                [_image_human()],
                _ua(supports_vision=False),
                use_knowledge_retrieval=False,
                use_web_search=True,
                has_mcp_tools=False,
            )
        )

    def test_config_disabled_no_caption(self):
        from app.settings import settings

        with mock.patch.object(settings, "CHAT_VISION_CAPTION_ENABLED", False):
            self.assertFalse(
                _should_run_vision_caption(
                    [_image_human()],
                    _ua(),
                    use_knowledge_retrieval=False,
                    use_web_search=True,
                    has_mcp_tools=False,
                )
            )


class CaptionPrepareTests(unittest.TestCase):
    def _prepare(self, caption):
        from app.settings import settings

        with mock.patch.object(settings, "CHAT_USE_SESSION_MEMORY", False):
            return _prepare_to_invoke_messages(
                [_image_human()],
                _ua(),
                user_id=1,
                agent_id=2,
                session_id="s1",
                user_query_for_memory="图里是什么",
                use_knowledge_retrieval=False,
                use_web_search=True,
                document_filter=None,
                session_attachment_hint="",
                image_caption=caption,
            )

    def test_with_caption_no_image_url_and_caption_injected(self):
        out = self._prepare("图中是一只猫，旁边有文字「你好」")
        for m in out:
            if isinstance(m.content, list):
                self.assertTrue(
                    all(
                        not (isinstance(b, dict) and b.get("type") == "image_url")
                        for b in m.content
                    )
                )
        last = out[-1]
        self.assertIsInstance(last, HumanMessage)
        blob = str(last.content)
        self.assertIn("本回合图片内容理解", blob)
        self.assertIn("图中是一只猫", blob)
        # 图占位仍在，便于模型知道有附件
        self.assertIn("x.png", blob)

    def test_without_caption_still_expands_image(self):
        raw = _png_bytes(32, 32)
        with mock.patch(
            "app.chat.attachment_service.file_bytes_for_attachment", return_value=raw
        ):
            out = self._prepare(None)
        blocks = out[-1].content
        self.assertTrue(
            any(isinstance(b, dict) and b.get("type") == "image_url" for b in blocks)
        )

    def test_caption_truncated_to_max_chars(self):
        block = _format_turn_context_block(
            use_knowledge_retrieval=False,
            use_web_search=True,
            document_filter=None,
            session_attachment_hint="",
            memory_inject=None,
            mcp_approval_note=None,
            image_caption="长" * 5000,
        )
        self.assertIn("已截断", block)
        self.assertLess(len(block), 5000)

    def test_caption_prompt_forbids_fabrication(self):
        self.assertIn("不要编造", _CAPTION_SYSTEM_PROMPT)
        self.assertIn("不要调用任何工具", _CAPTION_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
