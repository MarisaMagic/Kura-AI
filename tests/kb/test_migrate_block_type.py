"""集合迁移：复制旧行时新字段回填默认值（文本 text / 图片空串）。"""

from __future__ import annotations

import unittest

from app.kb import migrate_block_type as mbt


class _FakeIterator:
    def __init__(self, rows):
        self._rows = rows
        self._done = False

    def next(self):
        if self._done:
            return []
        self._done = True
        return self._rows

    def close(self):
        pass


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.inserted: list[dict] = []
        self.query_fields: list[str] = []

    def query_iterator(self, **kwargs):
        self.query_fields = list(kwargs.get("output_fields") or [])
        return _FakeIterator(self._rows)

    def insert(self, collection_name, data):
        self.inserted.extend(data)


def _row(chunk_id: str, content_type: str) -> dict:
    return {
        "dense_embedding": [0.1, 0.2],
        "kb_scope": "s",
        "text": "t",
        "filename": "f",
        "file_type": "Text",
        "file_path": "p",
        "page_number": 0,
        "chunk_idx": 0,
        "chunk_id": chunk_id,
        "parent_chunk_id": "",
        "root_chunk_id": "",
        "chunk_level": 3,
        "content_type": content_type,
        "image_path": "",
        "position_start": 0,
        "position_end": 0,
        "image_position_x": 0,
        "image_position_y": 0,
        "image_width": 0,
        "image_height": 0,
    }


class CopyRowsTests(unittest.TestCase):
    def test_defaults_injected(self):
        client = _FakeClient([_row("c1", "text"), _row("c2", "image")])
        copied = mbt._copy_rows(client, "src", "dst")
        self.assertEqual(copied, 2)
        self.assertEqual(client.inserted[0]["block_type"], "text")
        self.assertEqual(client.inserted[1]["block_type"], "")
        for row in client.inserted:
            self.assertEqual(row["code_language"], "")

    def test_query_does_not_request_new_fields(self):
        client = _FakeClient([_row("c1", "text")])
        mbt._copy_rows(client, "src", "dst")
        self.assertNotIn("block_type", client.query_fields)
        self.assertNotIn("code_language", client.query_fields)


class HasFieldTests(unittest.TestCase):
    def test_has_field(self):
        desc = {"fields": [{"name": "text"}, {"name": "block_type"}]}
        self.assertTrue(mbt._has_field(desc, "block_type"))
        self.assertFalse(mbt._has_field(desc, "code_language"))


if __name__ == "__main__":
    unittest.main()
