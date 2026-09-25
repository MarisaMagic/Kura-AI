"""结构感知分块：Markdown 围栏/表格、阈值双约束、源码降级路径（零网络）。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.kb import structural_chunker as sc


class MarkdownSegmentTests(unittest.TestCase):
    def test_code_fence_kept_whole_with_language(self):
        md = (
            "# H1\n介绍\n\n## H2\n"
            "```python\ndef f():\n    return 1\n```\n\n"
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        )
        leaves = sc.segment_markdown(md, "d.md")
        self.assertEqual([leaf.block_type for leaf in leaves], ["text", "code", "table"])
        self.assertEqual(leaves[1].language, "python")
        self.assertEqual(leaves[1].text, "def f():\n    return 1\n")
        self.assertEqual(leaves[0].parent_titles, ("H1",))
        self.assertEqual(leaves[1].parent_titles, ("H1", "H2"))
        self.assertEqual(leaves[2].parent_titles, ("H1", "H2"))
        self.assertIn("| a | b |", leaves[2].text)

    def test_unclosed_fence_consumes_to_eof(self):
        leaves = sc.segment_markdown("前言\n```js\nconst a = 1;\n", "u.md")
        code = [leaf for leaf in leaves if leaf.block_type == sc.BLOCK_CODE]
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0].language, "javascript")
        self.assertIn("const a = 1;", code[0].text)

    def test_indented_fence_in_list(self):
        leaves = sc.segment_markdown("- 步骤\n  ```sh\n  echo hi\n  ```\n", "i.md")
        code = [leaf for leaf in leaves if leaf.block_type == sc.BLOCK_CODE]
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0].language, "bash")

    def test_table_inside_fence_not_table(self):
        leaves = sc.segment_markdown("```\n| a | b |\n| --- | --- |\n```\n", "f.md")
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].block_type, sc.BLOCK_CODE)
        self.assertIn("| --- | --- |", leaves[0].text)

    def test_heading_skip_and_missing_heading_fallback(self):
        leaves = sc.segment_markdown("# A\n### C\n内容\n", "s.md")
        self.assertEqual(leaves[0].parent_titles, ("A", "C"))
        plain = sc.segment_markdown("无标题正文\n", "x.md")
        self.assertEqual(plain[0].parent_titles, ("x.md",))

    def test_embed_text_carries_heading_path(self):
        leaves = sc.segment_markdown("# A\n## B\n```python\nx = 1\n```\n", "e.md")
        self.assertIn("[代码 python] e.md A > B", leaves[0].embed_text)


class TableSplitTests(unittest.TestCase):
    def _rows(self, n: int):
        return [["h", "x"]] + [[f"r{i}", "v" * 40] for i in range(n)]

    def test_every_chunk_repeats_header_and_respects_limits(self):
        chunks = sc.split_table_rows(self._rows(30), max_chars=200, max_bytes=300, max_rows=4)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("| h | x |\n| --- | --- |"))
            self.assertLessEqual(len(chunk), 200)
            self.assertLessEqual(len(chunk.encode("utf-8")), 300)
            data_lines = [ln for ln in chunk.splitlines()[2:] if ln.strip()]
            self.assertLessEqual(len(data_lines), 4)

    def test_escaped_pipe_round_trip(self):
        rows = [["a|b", "c"], ["1", "2"]]
        rendered = sc.rows_to_markdown_table(rows)
        self.assertIn(r"a\|b", rendered)
        parsed = sc.parse_markdown_table(rendered)
        self.assertEqual(parsed[0][0], "a|b")

    def test_make_table_leaves_parent_titles(self):
        leaves = sc.make_table_leaves(
            [["n", "v"], ["a", "1"]], filename="t.xlsx", title="工作表：S1"
        )
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].block_type, sc.BLOCK_TABLE)
        self.assertEqual(leaves[0].parent_titles, ("t.xlsx", "工作表：S1"))


class ThresholdTests(unittest.TestCase):
    def test_multibyte_exceeds_bytes_must_split(self):
        text = "中" * 2000  # 6000 字节 > 1800
        with mock.patch.object(sc, "_get_ts_parser", return_value=None):
            pieces = sc.split_code_structural(text, "python", max_chars=1200, max_bytes=1800)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)
        for piece in pieces:
            self.assertLessEqual(len(piece.encode("utf-8")), 1800)

    def test_under_threshold_kept_whole(self):
        code = "def f():\n    return 1\n"
        self.assertEqual(sc.split_code_structural(code, "python"), [code])


class SourceFallbackTests(unittest.TestCase):
    def test_no_tree_sitter_falls_back_to_code_splitter(self):
        src = "def a():\n    pass\n\n\ndef b():\n    pass\n"
        with mock.patch.object(sc, "_get_ts_parser", return_value=None):
            leaves = sc.segment_source(src, "a.py")
        self.assertTrue(leaves)
        self.assertTrue(all(leaf.block_type == sc.BLOCK_CODE for leaf in leaves))
        self.assertTrue(all(leaf.language == "python" for leaf in leaves))
        self.assertEqual(leaves[0].parent_titles, ("a.py",))
        self.assertIn("[代码 python] a.py", leaves[0].embed_text)

    @unittest.skipUnless(sc._get_ts_parser("python") is not None, "tree-sitter-language-pack 未安装")
    def test_ast_splits_functions(self):  # pragma: no cover - 依赖可选依赖
        src = "import os\n\n\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n"
        leaves = sc.segment_source(src, "a.py")
        texts = [leaf.text for leaf in leaves]
        self.assertTrue(any("def a()" in t for t in texts))
        self.assertTrue(any("def b()" in t for t in texts))
        self.assertTrue(all(leaf.line_start >= 0 for leaf in leaves))


class CsvAndMarkdownDetectTests(unittest.TestCase):
    def test_parse_csv_rows_comma_and_tab(self):
        self.assertEqual(sc.parse_csv_rows("a,b\n1,2\n"), [["a", "b"], ["1", "2"]])
        rows = sc.parse_csv_rows("a\tb\n1\t2\n")
        self.assertEqual(rows, [["a", "b"], ["1", "2"]])

    def test_looks_like_markdown(self):
        self.assertTrue(sc.looks_like_markdown("# 标题\ntext"))
        self.assertTrue(sc.looks_like_markdown("| a | b |\n| --- | --- |"))
        self.assertFalse(sc.looks_like_markdown("普通文本\n第二行"))


if __name__ == "__main__":
    unittest.main()
