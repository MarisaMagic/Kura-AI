"""
消息树纯逻辑单测：路径解析、版本元信息、turn_key 分组、压缩状态前缀匹配、Milvus 过滤表达式。
不依赖 PostgreSQL / Milvus / Redis 连接，仅测试静态/纯函数。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.chat.compact import load_compact_states, match_compact_state
from app.chat.memory_turns import group_turn_pairs, turn_keys_of
from app.chat.milvus_memory import memory_filter_expr
from app.chat.storage import ConversationStorage


def _row(rid, parent_id=None, selected_child_id=None, message_type="human"):
    return SimpleNamespace(
        id=rid,
        parent_id=parent_id,
        selected_child_id=selected_child_id,
        message_type=message_type,
    )


def _linear_rows(n):
    """构造 id 1..n 的线性链（等价存量回填后的形态）。"""
    rows = []
    for i in range(1, n + 1):
        rows.append(
            _row(
                i,
                parent_id=i - 1 if i > 1 else None,
                selected_child_id=i + 1 if i < n else None,
                message_type="human" if i % 2 == 1 else "ai",
            )
        )
    return rows


class WalkPathTest(unittest.TestCase):
    def test_linear_chain(self):
        rows = _linear_rows(6)
        path = ConversationStorage._walk_path(rows)
        self.assertEqual([r.id for r in path], [1, 2, 3, 4, 5, 6])

    def test_branch_selection(self):
        # U1(1) -> A1(2) -> U2(3) -> A2v1(4) -> U3(5) -> A3(6)
        #                         \-> A2v2(7) 选中 v2
        rows = _linear_rows(6) + [_row(7, parent_id=3, message_type="ai")]
        rows[2].selected_child_id = 7  # U2 选中 A2v2
        path = ConversationStorage._walk_path(rows)
        self.assertEqual([r.id for r in path], [1, 2, 3, 7])

    def test_switch_back_restores_old_branch(self):
        rows = _linear_rows(6) + [_row(7, parent_id=3, message_type="ai")]
        rows[2].selected_child_id = 4  # U2 切回 A2v1
        path = ConversationStorage._walk_path(rows)
        self.assertEqual([r.id for r in path], [1, 2, 3, 4, 5, 6])

    def test_dangling_pointer_falls_back_to_min_child(self):
        rows = _linear_rows(4)
        rows[0].selected_child_id = 999  # 悬空指针
        path = ConversationStorage._walk_path(rows)
        self.assertEqual([r.id for r in path], [1, 2, 3, 4])

    def test_empty_and_rootless(self):
        self.assertEqual(ConversationStorage._walk_path([]), [])
        # 无根兜底：退化为按 id 线性
        rows = [_row(2, parent_id=1), _row(3, parent_id=2)]
        path = ConversationStorage._walk_path(rows)
        self.assertEqual([r.id for r in path], [2, 3])


class VersionMetaTest(unittest.TestCase):
    def test_versions_counted_per_human_parent(self):
        rows = _linear_rows(4) + [_row(5, parent_id=3, message_type="ai")]
        rows[2].selected_child_id = 5
        path = ConversationStorage._walk_path(rows)  # 1,2,3,5
        meta = ConversationStorage._version_meta(rows, path)
        # 路径上的 AI(5) 是第 2 版，共 2 版
        self.assertEqual(meta[5][0], 2)
        self.assertEqual(meta[5][1], 2)
        self.assertEqual(meta[5][2], [4, 5])
        # 不在路径上的旧版本无元信息
        self.assertNotIn(4, meta)


class TurnKeyTest(unittest.TestCase):
    def test_group_turn_pairs(self):
        pairs = [
            (10, HumanMessage(content="u1")),
            (11, AIMessage(content="a1")),
            (12, HumanMessage(content="u2")),
            (13, AIMessage(content="a2")),
        ]
        turns = group_turn_pairs(pairs)
        self.assertEqual(len(turns), 2)
        self.assertEqual([t[0][0] for t in turns], [10, 12])

    def test_turn_keys_of(self):
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="u1"),
            AIMessage(content="a1"),
            HumanMessage(content="u2"),
        ]
        ids = [1, 2, 3, 4]
        self.assertEqual(turn_keys_of(msgs, ids), [2, 4])
        # 长度不匹配时回退空列表
        self.assertEqual(turn_keys_of(msgs, [1, 2]), [])


class CompactStateTest(unittest.TestCase):
    def test_legacy_state_mapped_by_path_keys(self):
        meta = {"compact_summary": "旧摘要", "compact_until_turn_index": 1}
        states = load_compact_states(meta, [10, 12, 14])
        self.assertEqual(states, [{"covered_turn_keys": [10, 12], "summary": "旧摘要"}])

    def test_longest_prefix_match_wins(self):
        states = [
            {"covered_turn_keys": [10, 12], "summary": "s2"},
            {"covered_turn_keys": [10], "summary": "s1"},
            {"covered_turn_keys": [10, 99], "summary": "sx"},  # 不是当前路径前缀
        ]
        summary, covered = match_compact_state(states, [10, 12, 14])
        self.assertEqual((summary, covered), ("s2", 2))

    def test_no_match_after_branch_diverges(self):
        states = [{"covered_turn_keys": [10, 12], "summary": "s2"}]
        # 分支在第二轮分叉：12 不在新路径上
        summary, covered = match_compact_state(states, [10, 20, 21])
        self.assertEqual((summary, covered), ("", 0))


class MemoryFilterExprTest(unittest.TestCase):
    def test_turn_keys_filter(self):
        expr = memory_filter_expr("u1_a2_s3", turn_keys=[10, 12])
        self.assertIn('memory_scope == "u1_a2_s3"', expr)
        self.assertIn("turn_key in [10,12]", expr)

    def test_empty_turn_keys_matches_nothing(self):
        expr = memory_filter_expr("u1_a2_s3", turn_keys=[])
        self.assertIn("turn_key in [-1]", expr)

    def test_no_turn_keys_keeps_scope_only(self):
        expr = memory_filter_expr("u1_a2_s3")
        self.assertNotIn("turn_key", expr)


if __name__ == "__main__":
    unittest.main()
