"""加载器集成：Markdown 代码/表格、源码、CSV 的 block_type 与层级父子关系（零网络）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.kb.multimodal_document_loader import MultimodalDocumentLoader


def _load(loader: MultimodalDocumentLoader, suffix: str, content: str, filename: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"src{suffix}"
        path.write_text(content, encoding="utf-8")
        return loader.load_document(
            str(path), filename, "kb:test:1", 1, 1, str(Path(tmp) / "images")
        )


class LoaderBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = MultimodalDocumentLoader()

    def _assert_hierarchy(self, docs: list[dict]) -> None:
        ids = {d["chunk_id"] for d in docs}
        self.assertEqual(len(ids), len(docs), "chunk_id 必须唯一")
        l1 = [d for d in docs if d["chunk_level"] == 1]
        l3 = [d for d in docs if d["chunk_level"] == 3]
        self.assertTrue(l1)
        self.assertTrue(l3)
        for leaf in l3:
            self.assertIn(leaf["parent_chunk_id"], ids)
            self.assertIn(leaf["root_chunk_id"], ids)
            self.assertIn("embed_text", leaf)

    def test_markdown_code_and_table(self):
        md = (
            "# 模块 A\n说明\n\n## 用法\n"
            "```python\ndef f():\n    return 1\n```\n\n"
            "| 参数 | 含义 |\n| --- | --- |\n| x | 输入 |\n"
        )
        docs = _load(self.loader, ".md", md, "guide.md")
        self._assert_hierarchy(docs)
        leaves = [d for d in docs if d["chunk_level"] == 3]
        kinds = {leaf["block_type"] for leaf in leaves}
        self.assertIn("code", kinds)
        self.assertIn("table", kinds)
        code = next(leaf for leaf in leaves if leaf["block_type"] == "code")
        self.assertEqual(code["code_language"], "python")
        self.assertEqual(code["file_type"], "Text")
        table = next(leaf for leaf in leaves if leaf["block_type"] == "table")
        self.assertIn("| 参数 | 含义 |", table["text"])
        # L2 章节在 auto-merge 时可作为父块
        l2 = [d for d in docs if d["chunk_level"] == 2]
        self.assertEqual(len(l2), 1)

    def test_source_file(self):
        src = "import os\n\n\ndef f():\n    return os.getcwd()\n"
        docs = _load(self.loader, ".py", src, "mod.py")
        self._assert_hierarchy(docs)
        leaves = [d for d in docs if d["chunk_level"] == 3]
        self.assertTrue(all(leaf["block_type"] == "code" for leaf in leaves))
        self.assertTrue(all(leaf["code_language"] == "python" for leaf in leaves))
        self.assertEqual(docs[0]["file_type"], "Code")

    def test_csv_table(self):
        docs = _load(self.loader, ".csv", "name,value\na,1\nb,2\n", "data.csv")
        self._assert_hierarchy(docs)
        leaves = [d for d in docs if d["chunk_level"] == 3]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["block_type"], "table")
        self.assertIn("| name | value |", leaves[0]["text"])


if __name__ == "__main__":
    unittest.main()
