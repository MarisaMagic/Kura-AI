"""知识库工具输出：代码块补围栏、表格原样、旧数据（无 block_type）兼容。"""

from __future__ import annotations

import unittest

from app.kb import kb_tool_formatting as ktf


class RenderTextBlockTests(unittest.TestCase):
    def test_code_wrapped_with_language(self):
        rendered = ktf._render_text_block("def f():\n    return 1", "code", "python")
        self.assertEqual(rendered, "```python\ndef f():\n    return 1\n```")

    def test_fence_extends_when_code_contains_backticks(self):
        rendered = ktf._render_text_block("x = ```inner```", "code", "js")
        self.assertTrue(rendered.startswith("````js"))
        self.assertTrue(rendered.endswith("````"))

    def test_plain_and_table_untouched(self):
        table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        self.assertEqual(ktf._render_text_block(table, "table", ""), table)
        self.assertEqual(ktf._render_text_block("正文", "text", ""), "正文")
        # 旧数据无 block_type 时按文本处理
        self.assertEqual(ktf._render_text_block("正文", "", ""), "正文")


class FormatToolOutputTests(unittest.TestCase):
    def test_code_source_metadata_exposed(self):
        docs = [
            {
                "filename": "a.py",
                "page_number": 0,
                "content_type": "text",
                "text": "print(1)",
                "block_type": "code",
                "code_language": "python",
                "chunk_id": "c1",
                "score": 0.9,
            }
        ]
        out, images, sources = ktf.format_knowledge_retrieval_tool_output(docs)
        self.assertIn("```python", out)
        self.assertEqual(images, [])
        self.assertEqual(sources[0]["block_type"], "code")
        self.assertEqual(sources[0]["code_language"], "python")


if __name__ == "__main__":
    unittest.main()
