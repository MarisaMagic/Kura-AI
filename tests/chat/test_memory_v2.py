"""
长期记忆 v2 单测：scope 分级隔离、过滤表达式降级、行归一化、事实路由去重、
episodic 写入量与失效清理、滚动淘汰、检索渲染与配额。
Milvus 与嵌入服务均用内存桩替代，不打真实服务。
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.chat.compact_store as store_mod
import app.chat.memory_archive as ma
import app.chat.storage as storage_mod
from app.chat.compact_store import upsert_segment
from app.chat.db_models import ChatCompactSegment, ChatMessage as ChatMessageRow, ChatSession as ChatSessionRow
from app.chat.memory_scope import (
    is_session_scope,
    is_user_scope,
    memory_scope_for,
    session_memory_scope,
    user_memory_scope,
)
from app.chat.memory_search import _fused_score, _merge_hits, format_memory_hits
from app.chat.milvus_memory import (
    KIND_EPISODIC,
    KIND_FACTUAL,
    KIND_RAW,
    ChatMemoryMilvusManager,
    memory_filter_expr,
)
from app.settings import settings


class _MemoryCache:
    def __init__(self):
        self._d = {}

    def get_json(self, key):
        return self._d.get(key)

    def set_json(self, key, value, ttl=None):
        self._d[key] = value
        return True

    def set_nx(self, key, value, ttl=None):
        if key in self._d:
            return False
        self._d[key] = value
        return True

    def delete(self, key):
        self._d.pop(key, None)

    def delete_if_job_matches(self, key, job_id):
        v = self._d.get(key)
        if isinstance(v, dict) and v.get("job_id") == job_id:
            self._d.pop(key, None)
            return True
        return False


class _FakeMilvus:
    """内存版会话记忆集合：按 chunk_id 主键 upsert，支持标量 query 与按表达式删除。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.init_calls = 0

    def init_collection(self, *a, **kw):
        self.init_calls += 1

    def collection_exists(self):
        return True

    def upsert(self, data):
        for r in data:
            row = ChatMemoryMilvusManager.normalize_row(r)
            self.rows[row["chunk_id"]] = row
        return len(data)

    insert = upsert

    def query(self, filter_expr, *, output_fields=None, limit=100):
        scope = _parse_scope(filter_expr)
        kinds = _parse_kinds(filter_expr)
        out = []
        for row in self.rows.values():
            if scope and row["memory_scope"] not in scope:
                continue
            if kinds and row["kind"] not in kinds:
                continue
            out.append({k: row.get(k) for k in (output_fields or list(row))})
            if len(out) >= limit:
                break
        return out

    def delete_by_chunk_ids(self, chunk_ids):
        n = 0
        for cid in chunk_ids:
            if self.rows.pop(str(cid), None) is not None:
                n += 1
        return n

    def delete_by_expr(self, expr):
        scope = _parse_scope(expr)
        victims = [k for k, v in self.rows.items() if not scope or v["memory_scope"] in scope]
        for k in victims:
            self.rows.pop(k, None)
        return len(victims)

    def delete_by_scope(self, scope):
        return self.delete_by_expr(f'memory_scope == "{scope}"')

    def hybrid_retrieve(self, dense, query_text="", *, top_k=8, filter_expr="", **kw):
        scope = _parse_scope(filter_expr)
        kinds = _parse_kinds(filter_expr)
        to_keys = _parse_to_turn_keys(filter_expr)
        out = []
        for row in self.rows.values():
            if scope and row["memory_scope"] not in scope:
                continue
            if kinds and row["kind"] not in kinds:
                continue
            # 表达式形如 (kind != "episodic" || to_turn_key in [...])：仅约束 episodic 行
            if to_keys is not None and row["kind"] == KIND_EPISODIC:
                if int(row.get("to_turn_key") or 0) not in to_keys:
                    continue
            out.append({**row, "score": 0.02})
        return out[:top_k]


def _parse_to_turn_keys(expr: str) -> set[int] | None:
    import re

    m = re.search(r"to_turn_key in \[([^\]]*)\]", expr or "")
    if not m:
        return None
    out = set()
    for part in m.group(1).split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def _parse_scope(expr: str) -> set[str]:
    import re

    return set(re.findall(r'memory_scope == "([^"]+)"', expr or ""))


def _parse_kinds(expr: str) -> set[str]:
    import re

    m = re.search(r"kind in \[([^\]]*)\]", expr or "")
    if not m:
        return set()
    return {x.strip().strip('"') for x in m.group(1).split(",") if x.strip()}


class ScopeTest(unittest.TestCase):
    def test_session_and_user_scopes_differ(self):
        s = session_memory_scope(1, 2, "sess")
        u = user_memory_scope(1, 2)
        self.assertTrue(is_session_scope(s))
        self.assertTrue(is_user_scope(u))
        self.assertFalse(is_user_scope(s))
        self.assertFalse(is_session_scope(u))
        self.assertTrue(s.startswith("s:"))
        self.assertTrue(u.startswith("u:"))

    def test_legacy_alias_is_session_scope(self):
        self.assertEqual(memory_scope_for(1, 2, "sess"), session_memory_scope(1, 2, "sess"))

    def test_scope_binds_user_agent_and_session(self):
        self.assertNotEqual(session_memory_scope(1, 2, "a"), session_memory_scope(1, 2, "b"))
        self.assertNotEqual(session_memory_scope(1, 2, "a"), session_memory_scope(1, 3, "a"))
        self.assertNotEqual(session_memory_scope(1, 2, "a"), session_memory_scope(9, 2, "a"))

    def test_shared_agent_isolation_red_line(self):
        """共享智能体下，属主与共享用户必须落在不同的用户级 scope。

        误用属主 id 会让共享用户读到属主的偏好、属主被共享用户的对话污染。
        """
        owner_id, shared_user_id, agent_id = 100, 200, 7
        self.assertNotEqual(
            user_memory_scope(owner_id, agent_id), user_memory_scope(shared_user_id, agent_id)
        )
        self.assertNotEqual(
            session_memory_scope(owner_id, agent_id, "s1"),
            session_memory_scope(shared_user_id, agent_id, "s1"),
        )

    def test_user_scope_is_cross_session(self):
        """用户级 scope 不含 session_id，因此天然跨会话。"""
        self.assertEqual(user_memory_scope(1, 2), user_memory_scope(1, 2))
        self.assertNotIn("s1", user_memory_scope(1, 2))


class FilterExprTest(unittest.TestCase):
    def test_scope_only(self):
        expr = memory_filter_expr("s:u1_a2_s3")
        self.assertIn('memory_scope == "s:u1_a2_s3"', expr)
        self.assertNotIn("turn_key", expr)

    def test_kind_filter(self):
        expr = memory_filter_expr("s:x", kinds=[KIND_EPISODIC, KIND_RAW])
        self.assertIn("kind in [", expr)
        self.assertIn(KIND_EPISODIC, expr)
        self.assertIn(KIND_RAW, expr)

    def test_empty_turn_keys_matches_nothing(self):
        self.assertIn("turn_key in [-1]", memory_filter_expr("s:x", turn_keys=[]))

    def test_short_turn_key_list_uses_in(self):
        expr = memory_filter_expr("s:x", turn_keys=[10, 12, 14])
        self.assertIn("turn_key in [10,12,14]", expr)

    def test_long_turn_key_list_degrades_to_range(self):
        """v1 的 IN 列表会随会话无限膨胀；超上限必须降级为范围表达式。"""
        keys = list(range(1, 600))
        expr = memory_filter_expr("s:x", turn_keys=keys, max_turn_keys=256)
        self.assertNotIn("turn_key in [", expr)
        self.assertIn("turn_key >= 1", expr)
        self.assertIn("turn_key <= 599", expr)
        self.assertLess(len(expr), 200)

    def test_range_degradation_threshold_configurable(self):
        keys = list(range(1, 20))
        expr = memory_filter_expr("s:x", turn_keys=keys, max_turn_keys=10)
        self.assertIn("turn_key >= 1", expr)
        self.assertNotIn("turn_key in [", expr)

    def test_to_turn_key_upper_bound(self):
        expr = memory_filter_expr("s:x", to_turn_key_at_most=42)
        self.assertIn("to_turn_key <= 42", expr)

    def test_scope_escaped(self):
        expr = memory_filter_expr('s:evil"_x')
        self.assertIn('\\"', expr)


class NormalizeRowTest(unittest.TestCase):
    def test_fills_all_v2_fields(self):
        row = ChatMemoryMilvusManager.normalize_row({"chunk_id": "c1", "text": "正文", "memory_scope": "s:x"})
        for f in (
            "chunk_id",
            "memory_scope",
            "kind",
            "text",
            "turn_index",
            "turn_key",
            "from_turn_key",
            "to_turn_key",
            "chunk_index",
            "level",
            "fact_key",
            "fact_type",
            "value_score",
            "created_at",
        ):
            self.assertIn(f, row)
        self.assertEqual(row["kind"], KIND_RAW)
        self.assertGreater(row["created_at"], 0)

    def test_drops_v1_autoincrement_id(self):
        row = ChatMemoryMilvusManager.normalize_row({"chunk_id": "c1", "text": "t", "id": 999})
        self.assertNotIn("id", row)

    def test_text_truncated_to_varchar_cap(self):
        with mock.patch.object(settings, "CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH", 512):
            row = ChatMemoryMilvusManager.normalize_row({"chunk_id": "c", "text": "长" * 5000})
        self.assertLessEqual(len(row["text"]), 512)

    def test_same_chunk_id_is_idempotent(self):
        """chunk_id 作主键：重复写入覆盖而非新增（v1 auto_id 会插出重复行）。"""
        fake = _FakeMilvus()
        fake.upsert([{"chunk_id": "dup", "text": "v1", "memory_scope": "s:x"}])
        fake.upsert([{"chunk_id": "dup", "text": "v2", "memory_scope": "s:x"}])
        self.assertEqual(len(fake.rows), 1)
        self.assertEqual(fake.rows["dup"]["text"], "v2")

    def test_bad_numbers_coerced(self):
        row = ChatMemoryMilvusManager.normalize_row(
            {"chunk_id": "c", "text": "t", "turn_key": "坏", "value_score": "也坏"}
        )
        self.assertEqual(row["turn_key"], 0)
        self.assertEqual(row["value_score"], 0.0)


class FactRoutingTest(unittest.TestCase):
    def test_normalize_rejects_bad_type(self):
        self.assertIsNone(ma.normalize_fact({"type": "闲聊", "content": "内容足够长"}))
        self.assertIsNone(ma.normalize_fact({"content": "没有类型"}))
        self.assertIsNone(ma.normalize_fact("不是字典"))

    def test_normalize_rejects_too_short_content(self):
        self.assertIsNone(ma.normalize_fact({"type": "entity", "content": "短"}))

    def test_normalize_accepts_all_four_types(self):
        for t in ("preference", "decision", "entity", "constraint"):
            f = ma.normalize_fact({"type": t, "subject": "主题", "content": "这是一条足够长的内容"})
            self.assertIsNotNone(f, t)
            self.assertEqual(f["type"], t)
            self.assertTrue(f["fact_key"])

    def test_fact_key_stable_and_type_sensitive(self):
        a = ma.fact_key_of("preference", "回答语言")
        b = ma.fact_key_of("preference", "回答语言")
        c = ma.fact_key_of("decision", "回答语言")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_fact_key_normalizes_whitespace_and_case(self):
        self.assertEqual(ma.fact_key_of("entity", "PostgreSQL"), ma.fact_key_of("entity", "postgre sql"))

    def test_preference_and_constraint_go_to_user_scope(self):
        """偏好/约束跨会话稳定 → 用户级；决策/实体只在本任务内有意义 → 会话级。"""
        for t in ("preference", "constraint"):
            scope, level = ma.fact_scope({"type": t}, user_id=1, agent_id=2, session_id="s")
            self.assertEqual(level, "user")
            self.assertTrue(is_user_scope(scope))
        for t in ("decision", "entity"):
            scope, level = ma.fact_scope({"type": t}, user_id=1, agent_id=2, session_id="s")
            self.assertEqual(level, "session")
            self.assertTrue(is_session_scope(scope))

    def test_fact_text_includes_why_and_how(self):
        text = ma.fact_text(
            {"type": "preference", "subject": "语言", "content": "用中文", "why": "用户要求", "how_to_apply": "默认中文"}
        )
        self.assertIn("用中文", text)
        self.assertIn("依据：用户要求", text)
        self.assertIn("适用方式：默认中文", text)

    def test_fact_content_capped(self):
        with mock.patch.object(settings, "CHAT_MEMORY_FACT_MAX_CHARS", 100):
            f = ma.normalize_fact({"type": "entity", "content": "长" * 500})
        self.assertLessEqual(len(f["content"]), 100)


class RenderHitsTest(unittest.TestCase):
    def _hit(self, kind, text, **kw):
        return {
            "kind": kind,
            "text": text,
            "turn_index": kw.pop("turn_index", 3),
            "fact_type": kw.pop("fact_type", ""),
            "value_score": kw.pop("value_score", 0.5),
            "score": kw.pop("score", 0.02),
            **kw,
        }

    def test_blocks_separated_by_kind(self):
        hits = [
            self._hit(KIND_FACTUAL, "始终用中文回答", fact_type="preference"),
            self._hit(KIND_EPISODIC, "1. Primary Request and Intent\n- 用户要压缩"),
            self._hit(KIND_RAW, "用户: 原始轮次文本"),
        ]
        out = format_memory_hits(hits, max_tokens=5000)
        self.assertIn("跨会话长期记忆", out)
        self.assertIn("长期偏好", out)
        self.assertIn("分段摘要", out)
        self.assertIn("原文片段", out)
        # 长期记忆排最前：优先级最高
        self.assertLess(out.index("跨会话长期记忆"), out.index("分段摘要"))

    def test_points_to_history_tool_for_verbatim(self):
        out = format_memory_hits([self._hit(KIND_EPISODIC, "摘要")], max_tokens=5000)
        self.assertIn("read_session_history", out)

    def test_token_budget_truncates(self):
        hits = [self._hit(KIND_EPISODIC, "很长的摘要内容" * 200, turn_index=i) for i in range(10)]
        out = format_memory_hits(hits, max_tokens=300)
        self.assertLess(len(out), 5000)
        # 预算耗尽后不再追加更多条目
        self.assertLess(out.count("较早对话摘要"), 10)

    def test_empty_hits(self):
        self.assertEqual(format_memory_hits([]), "")

    def test_fused_score_ranks_factual_above_episodic_above_raw(self):
        base = {"score": 0.02, "value_score": 0.5}
        f = _fused_score({**base, "kind": KIND_FACTUAL})
        e = _fused_score({**base, "kind": KIND_EPISODIC})
        r = _fused_score({**base, "kind": KIND_RAW})
        self.assertGreater(f, e)
        self.assertGreater(e, r)

    def test_merge_reserves_slots_for_facts_within_top_k(self):
        facts = [{"kind": KIND_FACTUAL, "score": 0.01, "value_score": 1.0, "text": "f1"}]
        sess = [
            {"kind": KIND_EPISODIC, "score": 0.09, "value_score": 0.9, "text": f"e{i}"} for i in range(6)
        ]
        merged = _merge_hits(facts, sess, top_k=3)
        # 事实保底 1 条，其余名额给会话记忆；总量必须 ≤ top_k
        self.assertEqual(merged[0]["kind"], KIND_FACTUAL)
        self.assertEqual(len(merged), 3)

    def test_merge_never_exceeds_top_k(self):
        facts = [{"kind": KIND_FACTUAL, "score": 0.1, "value_score": 1.0, "text": f"f{i}"} for i in range(5)]
        sess = [{"kind": KIND_EPISODIC, "score": 0.05, "value_score": 0.5, "text": f"e{i}"} for i in range(5)]
        for k in (1, 2, 3, 5):
            self.assertLessEqual(len(_merge_hits(facts, sess, top_k=k)), k)

    def test_merge_top_k_one_still_returns_one_fact(self):
        facts = [{"kind": KIND_FACTUAL, "score": 0.1, "value_score": 1.0, "text": "f"}]
        sess = [{"kind": KIND_EPISODIC, "score": 0.9, "value_score": 0.5, "text": "e"}]
        merged = _merge_hits(facts, sess, top_k=1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["kind"], KIND_FACTUAL)

    def test_merge_without_facts(self):
        sess = [{"kind": KIND_EPISODIC, "score": s, "value_score": 0.5, "text": "e"} for s in (0.01, 0.05, 0.03)]
        merged = _merge_hits([], sess, top_k=2)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["score"], 0.05)  # 按融合分降序


class EpisodicArchiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls.factory = factory
        cls._orig = (store_mod.SessionLocal, storage_mod.SessionLocal, storage_mod.cache, ma.storage)
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        ma.storage = storage_mod.ConversationStorage()
        cls.storage = ma.storage

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal, storage_mod.SessionLocal, storage_mod.cache, ma.storage = cls._orig

    def setUp(self):
        self.uid, self.aid = 940001, 940001
        self.sid = f"ep_{self.id().split('.')[-1]}"
        for i in range(12):
            self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage_(f"问{i}")])
            self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage_(f"答{i}")])
        self.ref = self.storage.get_session_ref_id(self.uid, self.aid, self.sid)
        self.turn_keys = [
            r["message_id"] for r in self.storage.get_session_messages(self.uid, self.aid, self.sid)
        ][0::2]
        self.fake = _FakeMilvus()
        self._patches = [
            mock.patch.object(ma, "get_chat_memory_milvus", return_value=self.fake),
            mock.patch.object(settings, "EMBEDDING_API_KEY", "sk-test"),
            mock.patch(
                "app.kb.multimodal_embedding.get_multimodal_embedding_service",
                return_value=_FakeEmbedder(),
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _add_segment(self, from_index, to_index, summary, level=0):
        return upsert_segment(
            self.ref,
            from_index=from_index,
            to_index=to_index,
            from_turn_key=self.turn_keys[from_index],
            to_turn_key=self.turn_keys[to_index - 1],
            summary=summary,
            tokens=len(summary),
            level=level,
        )

    def test_one_segment_yields_few_vectors(self):
        """v1 会把 12 轮原文切成十几条向量；v2 一段摘要只产少量向量。"""
        self._add_segment(0, 12, "1. Primary Request and Intent\n- 用户要 X\n\n2. Key Technical Concepts\n- Y")
        info = ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        self.assertEqual(info["segments"], 1)
        self.assertLessEqual(info["inserted"], 3)
        self.assertGreater(info["inserted"], 0)
        rows = list(self.fake.rows.values())
        self.assertTrue(all(r["kind"] == KIND_EPISODIC for r in rows))
        self.assertTrue(all(r["memory_scope"].startswith("s:") for r in rows))

    def test_idempotent_rerun(self):
        """重复归档不得产生新向量（chunk_id 幂等 + metadata 记录已入库 id）。"""
        self._add_segment(0, 6, "摘要A" * 50)
        first = ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        second = ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        self.assertGreater(first["inserted"], 0)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(len(self.fake.rows), first["inserted"])

    def test_merged_segment_removes_stale_vectors(self):
        """段被压缩侧归并后，旧的细粒度向量必须删除，否则会召回到失效摘要。"""
        self._add_segment(0, 4, "细段一" * 40)
        self._add_segment(4, 8, "细段二" * 40)
        ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        before = len(self.fake.rows)
        self.assertGreater(before, 0)

        # 模拟压缩侧归并：删掉两条细段，写入一条 level=1 的粗段覆盖 [0,8)
        from app.chat.compact_store import delete_segments, load_segments

        delete_segments(self.ref, [s["seg_id"] for s in load_segments(self.ref)])
        self._add_segment(0, 8, "归并后的粗段摘要" * 40, level=1)

        info = ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        self.assertGreater(info["removed"], 0)
        self.assertLess(len(self.fake.rows), before + info["inserted"])
        self.assertTrue(all("L1" in cid for cid in self.fake.rows))

    def test_skipped_without_embedding_key(self):
        with mock.patch.object(settings, "EMBEDDING_API_KEY", ""):
            self._add_segment(0, 6, "摘要" * 50)
            info = ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        self.assertEqual(info["inserted"], 0)
        self.assertEqual(self.fake.rows, {})

    def test_no_segments_no_writes(self):
        info = ma.archive_episodic_segments(self.uid, self.aid, self.sid)
        self.assertEqual(info["inserted"], 0)
        self.assertEqual(self.fake.rows, {})

    def test_episodic_allowed_to_keys_bounded_by_segment_count(self):
        """允许集合来自段链，长度受 MAX_SEGMENTS 约束，不会随会话长度膨胀。"""
        for i in range(3):
            self._add_segment(i * 4, i * 4 + 4, f"段{i}" * 40)
        keys = ma.episodic_allowed_to_keys(self.uid, self.aid, self.sid)
        self.assertEqual(len(keys), 3)
        expr = memory_filter_expr("s:x", kinds=[KIND_EPISODIC]) + " && to_turn_key in [{}]".format(
            ",".join(str(k) for k in keys)
        )
        self.assertLess(len(expr), 300)

    def test_segment_on_other_branch_not_searchable(self):
        """分叉后旧分支的段不在当前路径链上，不得进入允许集合。"""
        self._add_segment(0, 4, "旧分支段" * 40)
        # 篡改段表里该段的 to_turn_key，使其不再匹配当前路径
        db = self.factory()
        try:
            row = db.query(ChatCompactSegment).filter(ChatCompactSegment.session_ref_id == self.ref).first()
            row.to_turn_key = 999999
            db.commit()
        finally:
            db.close()
        self.assertEqual(ma.episodic_allowed_to_keys(self.uid, self.aid, self.sid), [])


class EvictionTest(unittest.TestCase):
    def setUp(self):
        self.uid, self.aid, self.sid = 950001, 950001, "evict_s"
        self.scope = session_memory_scope(self.uid, self.aid, self.sid)
        self.fake = _FakeMilvus()
        self._patches = [
            mock.patch.object(ma, "get_chat_memory_milvus", return_value=self.fake),
            mock.patch.object(ma, "storage", mock.MagicMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        ma.storage.get_session_metadata.return_value = {}
        ma.storage.mutate_session_metadata.return_value = {}

    def _fill(self, n, kind=KIND_RAW, scope=None, base_ts=1_000_000):
        for i in range(n):
            self.fake.upsert(
                [
                    {
                        "chunk_id": f"{kind}_{i}",
                        "memory_scope": scope or self.scope,
                        "kind": kind,
                        "text": f"内容{i}",
                        "turn_key": i,
                        "created_at": base_ts + i,
                    }
                ]
            )

    def test_under_cap_no_eviction(self):
        self._fill(5)
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 100):
            self.assertEqual(ma.enforce_rolling_cap(self.uid, self.aid, self.sid)["removed"], 0)
        self.assertEqual(len(self.fake.rows), 5)

    def test_over_cap_evicts_oldest(self):
        self._fill(20)
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 8):
            out = ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        self.assertEqual(out["removed"], 12)
        self.assertEqual(len(self.fake.rows), 8)
        # 留下的是 created_at 最大的 8 条
        self.assertIn(f"{KIND_RAW}_19", self.fake.rows)
        self.assertNotIn(f"{KIND_RAW}_0", self.fake.rows)

    def test_cap_zero_disables_eviction(self):
        """显式配 0 表示关闭，不能回退成默认 400。"""
        self._fill(5)
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 0):
            self.assertEqual(ma.enforce_rolling_cap(self.uid, self.aid, self.sid)["removed"], 0)
        self.assertEqual(len(self.fake.rows), 5)

    def test_eviction_advances_raw_watermark(self):
        """被删掉的 raw 轮次要前移水位线，否则下一轮又会被重新归档。"""
        self._fill(20)
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 8):
            ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        patch_fn = ma.storage.mutate_session_metadata.call_args[0][3]
        patch = patch_fn({})
        self.assertEqual(patch[ma._META_EVICTED_BELOW], 11)

    def test_other_sessions_untouched(self):
        self._fill(20)
        other = session_memory_scope(self.uid, self.aid, "别的会话")
        self._fill(3, scope=other)
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 8):
            ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        survivors = [r for r in self.fake.rows.values() if r["memory_scope"] == other]
        self.assertEqual(len(survivors), 3)

    def test_user_fact_cap_evicts_oldest(self):
        uscope = user_memory_scope(self.uid, self.aid)
        self._fill(12, kind=KIND_FACTUAL, scope=uscope)
        with mock.patch.object(settings, "CHAT_MEMORY_USER_FACT_MAX", 5):
            removed = ma.enforce_user_fact_cap(self.uid, self.aid)
        self.assertEqual(removed, 7)
        self.assertEqual(len(self.fake.rows), 5)

    def test_user_fact_cap_zero_disables(self):
        uscope = user_memory_scope(self.uid, self.aid)
        self._fill(12, kind=KIND_FACTUAL, scope=uscope)
        with mock.patch.object(settings, "CHAT_MEMORY_USER_FACT_MAX", 0):
            self.assertEqual(ma.enforce_user_fact_cap(self.uid, self.aid), 0)
        self.assertEqual(len(self.fake.rows), 12)

    def test_user_fact_cap_short_circuits_below_cap(self):
        """已知条数未达上限时直接返回，省掉每轮一次的 Milvus 查询。"""
        uscope = user_memory_scope(self.uid, self.aid)
        self._fill(12, kind=KIND_FACTUAL, scope=uscope)
        with mock.patch.object(settings, "CHAT_MEMORY_USER_FACT_MAX", 5):
            self.assertEqual(ma.enforce_user_fact_cap(self.uid, self.aid, known_fact_count=3), 0)
        self.assertEqual(len(self.fake.rows), 12)
        # 超过上限时照常淘汰
        with mock.patch.object(settings, "CHAT_MEMORY_USER_FACT_MAX", 5):
            self.assertEqual(ma.enforce_user_fact_cap(self.uid, self.aid, known_fact_count=9), 7)
        self.assertEqual(len(self.fake.rows), 5)

    def test_purge_session_keeps_user_facts(self):
        """删会话只清会话级向量，跨会话的用户级事实必须保留。"""
        self._fill(4)
        uscope = user_memory_scope(self.uid, self.aid)
        self._fill(3, kind=KIND_FACTUAL, scope=uscope)
        ma.purge_session_memory_vectors(self.uid, self.aid, self.sid)
        remaining = [r["memory_scope"] for r in self.fake.rows.values()]
        self.assertEqual(remaining.count(uscope), 3)
        self.assertNotIn(self.scope, remaining)


class MigrateScopeTest(unittest.TestCase):
    """v1→v2 scope 前缀改写：不改写则迁移后的旧向量永远不可达。"""

    def test_v1_session_scope_prefixed(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        new, status = migrate_scope("u12_a34_sabc")
        self.assertEqual(new, "s:u12_a34_sabc")
        self.assertEqual(status, "rewritten")

    def test_v1_user_scope_prefixed(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        new, status = migrate_scope("u12_a34")
        self.assertEqual(new, "u:u12_a34")
        self.assertEqual(status, "rewritten")

    def test_v2_scope_kept(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        for s in ("s:u1_a2_sx", "u:u1_a2"):
            self.assertEqual(migrate_scope(s), (s, "kept"))

    def test_unknown_scope_flagged(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        new, status = migrate_scope("乱写的scope")
        self.assertEqual(new, "乱写的scope")
        self.assertEqual(status, "unknown")

    def test_empty_scope_unknown(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        self.assertEqual(migrate_scope(""), ("", "unknown"))

    def test_session_scope_with_underscore_session_id(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        new, status = migrate_scope("u1_a2_smy_session_1")
        self.assertEqual(new, "s:u1_a2_smy_session_1")
        self.assertEqual(status, "rewritten")

    def test_empty_session_id_scope_rewritten(self):
        from app.chat.migrate_memory_v2 import migrate_scope

        new, status = migrate_scope("u1_a2_s")
        self.assertEqual(new, "s:u1_a2_s")
        self.assertEqual(status, "rewritten")


class FactStoreTest(unittest.TestCase):
    """factual 写入：同槽位内容变化要覆盖，而不是因为 key 已存在就丢弃。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls.factory = factory
        cls._orig = (store_mod.SessionLocal, storage_mod.SessionLocal, storage_mod.cache, ma.storage)
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        ma.storage = storage_mod.ConversationStorage()
        cls.storage = ma.storage

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal, storage_mod.SessionLocal, storage_mod.cache, ma.storage = cls._orig

    def setUp(self):
        self.uid, self.aid = 970001, 970001
        self.sid = f"fact_{self.id().split('.')[-1]}"
        self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage_("初始化会话")])
        self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage_("好的")])
        self.fake = _FakeMilvus()
        patches = [
            mock.patch.object(ma, "get_chat_memory_milvus", return_value=self.fake),
            mock.patch.object(settings, "EMBEDDING_API_KEY", "sk-test"),
            mock.patch.object(settings, "CHAT_MEMORY_FACTUAL_ENABLED", True),
            mock.patch(
                "app.kb.multimodal_embedding.get_multimodal_embedding_service",
                return_value=_FakeEmbedder(),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _store(self, facts):
        return ma.store_facts(self.uid, self.aid, self.sid, facts)

    def test_preference_written_to_user_scope(self):
        out = self._store([{"type": "preference", "subject": "回答语言", "content": "始终用中文"}])
        self.assertEqual(out["inserted"], 1)
        rows = list(self.fake.rows.values())
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["memory_scope"].startswith("u:"))
        self.assertEqual(rows[0]["fact_type"], "preference")

    def test_unchanged_fact_skipped(self):
        f = [{"type": "preference", "subject": "回答语言", "content": "始终用中文"}]
        self._store(f)
        out = self._store(f)
        self.assertEqual(out["inserted"], 0)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(len(self.fake.rows), 1)

    def test_changed_fact_overwrites_same_slot(self):
        """回归：同一主题的偏好从「中文」改为「英文」，必须覆盖而不是静默丢弃。"""
        self._store([{"type": "preference", "subject": "回答语言", "content": "始终用中文"}])
        out = self._store([{"type": "preference", "subject": "回答语言", "content": "始终用英文"}])
        self.assertEqual(out["inserted"], 1)
        self.assertEqual(out["updated"], 1)
        rows = list(self.fake.rows.values())
        self.assertEqual(len(rows), 1)  # 同槽位覆盖，不堆两条矛盾记忆
        self.assertIn("英文", rows[0]["text"])
        self.assertNotIn("中文", rows[0]["text"])

    def test_distinct_subjects_coexist(self):
        self._store([{"type": "preference", "subject": "回答语言", "content": "始终用中文"}])
        self._store([{"type": "preference", "subject": "代码风格", "content": "不要加注释"}])
        self.assertEqual(len(self.fake.rows), 2)

    def test_same_batch_same_slot_last_wins(self):
        """同批内 LLM 输出同槽位多条：只留最后一条，且后续相同内容被判为未变化。"""
        facts = [
            {"type": "preference", "subject": "回答语言", "content": "始终用中文"},
            {"type": "preference", "subject": "回答语言", "content": "始终用英文"},
        ]
        out = self._store(facts)
        rows = list(self.fake.rows.values())
        self.assertEqual(len(rows), 1)  # 同 chunk_id 只留一条
        self.assertIn("英文", rows[0]["text"])
        self.assertEqual(out["inserted"], 1)  # 去重后只嵌入一条
        again = self._store([{"type": "preference", "subject": "回答语言", "content": "始终用英文"}])
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(again["inserted"], 0)

    def test_decision_goes_to_session_scope(self):
        out = self._store([{"type": "decision", "subject": "认证方案", "content": "采用 JWT 而非 session"}])
        self.assertEqual(out["inserted"], 1)
        rows = list(self.fake.rows.values())
        self.assertTrue(rows[0]["memory_scope"].startswith("s:"))

    def test_disabled_by_config(self):
        with mock.patch.object(settings, "CHAT_MEMORY_FACTUAL_ENABLED", False):
            out = self._store([{"type": "preference", "subject": "x", "content": "很长的一段偏好内容"}])
        self.assertEqual(out["inserted"], 0)
        self.assertEqual(self.fake.rows, {})


class EpisodicEvictionCleanupTest(unittest.TestCase):
    """淘汰删除 episodic 向量后必须清掉「已入库」记录，否则该段永不回填。"""

    def setUp(self):
        self.uid, self.aid, self.sid = 980001, 980001, "ep_cleanup"
        self.scope = session_memory_scope(self.uid, self.aid, self.sid)
        self.fake = _FakeMilvus()
        self._patches = [
            mock.patch.object(ma, "get_chat_memory_milvus", return_value=self.fake),
            mock.patch.object(ma, "storage", mock.MagicMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        ma.storage.get_session_metadata.return_value = {}
        ma.storage.mutate_session_metadata.return_value = {}

    def test_evicted_episodic_ids_removed_from_metadata(self):
        for i in range(12):
            self.fake.upsert(
                [
                    {
                        "chunk_id": f"ep_c{i}",
                        "memory_scope": self.scope,
                        "kind": KIND_EPISODIC,
                        "text": f"摘要{i}",
                        "to_turn_key": i,
                        "created_at": 1000 + i,
                    }
                ]
            )
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 5):
            ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        patch_fn = ma.storage.mutate_session_metadata.call_args[0][3]
        patch = patch_fn({ma._META_EPISODIC_IDS: [f"ep_c{i}" for i in range(12)]})
        remaining = set(patch[ma._META_EPISODIC_IDS])
        self.assertEqual(remaining, {f"ep_c{i}" for i in range(7, 12)})

    def test_raw_only_eviction_still_sets_watermark(self):
        for i in range(12):
            self.fake.upsert(
                [
                    {
                        "chunk_id": f"raw_{i}",
                        "memory_scope": self.scope,
                        "kind": KIND_RAW,
                        "text": f"原文{i}",
                        "turn_key": i,
                        "created_at": 1000 + i,
                    }
                ]
            )
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 5):
            ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        patch_fn = ma.storage.mutate_session_metadata.call_args[0][3]
        patch = patch_fn({})
        self.assertEqual(patch[ma._META_EVICTED_BELOW], 6)
        self.assertNotIn(ma._META_EPISODIC_IDS, patch)

    def test_evicted_session_facts_clear_metadata(self):
        """会话级事实被淘汰后必须清对应 fact 键/hash，否则重新活跃时永久不回填。"""
        for i in range(12):
            self.fake.upsert(
                [
                    {
                        "chunk_id": f"fa_{i}",
                        "memory_scope": self.scope,
                        "kind": KIND_FACTUAL,
                        "fact_key": f"key{i}",
                        "fact_type": "decision",
                        "text": f"决策{i}",
                        "created_at": 1000 + i,
                    }
                ]
            )
        existing_keys = [f"key{i}" for i in range(12)]
        existing_hashes = {f"key{i}": f"h{i}" for i in range(12)}
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 5):
            ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        patch_fn = ma.storage.mutate_session_metadata.call_args[0][3]
        patch = patch_fn(
            {ma._META_FACT_KEYS: existing_keys, ma._META_FACT_HASHES: existing_hashes}
        )
        self.assertEqual(set(patch[ma._META_FACT_KEYS]), {f"key{i}" for i in range(7, 12)})
        self.assertEqual(set(patch[ma._META_FACT_HASHES]), {f"key{i}" for i in range(7, 12)})

    def test_session_fact_eviction_does_not_touch_user_facts(self):
        """用户级跨会话事实不在会话 scope 内，会话淘汰不得波及。"""
        uscope = user_memory_scope(self.uid, self.aid)
        self.fake.upsert(
            [
                {
                    "chunk_id": "fa_user",
                    "memory_scope": uscope,
                    "kind": KIND_FACTUAL,
                    "fact_key": "ukey",
                    "fact_type": "preference",
                    "text": "用户偏好",
                    "created_at": 1,
                }
            ]
        )
        for i in range(12):
            self.fake.upsert(
                [
                    {
                        "chunk_id": f"fa_s{i}",
                        "memory_scope": self.scope,
                        "kind": KIND_FACTUAL,
                        "fact_key": f"skey{i}",
                        "fact_type": "decision",
                        "text": f"决策{i}",
                        "created_at": 2000 + i,
                    }
                ]
            )
        with mock.patch.object(settings, "CHAT_MEMORY_HARD_MAX_VECTORS", 5):
            ma.enforce_rolling_cap(self.uid, self.aid, self.sid)
        keys = [r["chunk_id"] for r in self.fake.rows.values()]
        self.assertIn("fa_user", keys)
        self.assertEqual(len([k for k in keys if k.startswith("fa_s")]), 5)


def HumanMessage_(text):  # noqa: N802 - 测试内简写
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=text)


def AIMessage_(text):  # noqa: N802 - 测试内简写
    from langchain_core.messages import AIMessage

    return AIMessage(content=text)


class _FakeEmbedder:
    def get_text_embeddings(self, texts):
        return [[0.01] * 8 for _ in texts]


class RawArchiveTest(unittest.TestCase):
    """raw 兜底通道（默认关闭）：价值闸门、失败轮识别、近重复拦截。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls.factory = factory
        cls._orig = (store_mod.SessionLocal, storage_mod.SessionLocal, storage_mod.cache, ma.storage)
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        ma.storage = storage_mod.ConversationStorage()
        cls.storage = ma.storage

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal, storage_mod.SessionLocal, storage_mod.cache, ma.storage = cls._orig

    def setUp(self):
        self.uid, self.aid = 960001, 960001
        self.sid = f"raw_{self.id().split('.')[-1]}"
        self.fake = _FakeMilvus()
        patches = [
            mock.patch.object(ma, "get_chat_memory_milvus", return_value=self.fake),
            mock.patch.object(settings, "EMBEDDING_API_KEY", "sk-test"),
            mock.patch.object(settings, "CHAT_MEMORY_ARCHIVE_RAW_ENABLED", True),
            mock.patch.object(settings, "CHAT_MEMORY_MIN_VALUE_SCORE", 0.25),
            mock.patch.object(settings, "CHAT_MEMORY_DEDUP_WINDOW", 20),
            mock.patch(
                "app.kb.multimodal_embedding.get_multimodal_embedding_service",
                return_value=_FakeEmbedder(),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _turn(self, q: str, a: str, *, error: str | None = None):
        self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage_(q)])
        self.storage.append_messages(
            self.uid,
            self.aid,
            self.sid,
            [AIMessage_(a)],
            extra_message_data=[{"error_text": error}] if error else None,
        )

    def _compact_first(self, n: int):
        """把前 n 轮标记为已压缩，使它们离开原文窗口、成为归档候选。"""
        keys = [
            r["message_id"] for r in self.storage.get_session_messages(self.uid, self.aid, self.sid)
        ][0::2]
        ref = self.storage.get_session_ref_id(self.uid, self.aid, self.sid)
        upsert_segment(
            ref,
            from_index=0,
            to_index=n,
            from_turn_key=keys[0],
            to_turn_key=keys[n - 1],
            summary="已压缩摘要",
            tokens=10,
        )
        return keys

    def test_substantive_turn_archived(self):
        self._turn(
            "帮我把会话压缩的触发阈值改成按 token 计算",
            "已完成：trigger = window - 8000 - 6000，并加了连续失败 3 次的熔断保护。" * 4,
        )
        self._turn("第二轮问题也需要足够长才能通过闸门", "第二轮回答内容" * 30)
        self._compact_first(1)
        out = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertGreater(out["inserted"], 0)
        rows = list(self.fake.rows.values())
        self.assertTrue(all(r["kind"] == KIND_RAW for r in rows))
        self.assertTrue(all(r["memory_scope"].startswith("s:") for r in rows))

    def test_error_turn_gated(self):
        """失败轮不得入库：error_text 只存在于 DB 列，回放成消息后会丢失，须按行 id 判定。"""
        self._turn("一个足够长的问题，用来通过长度检查" * 3, "回答内容" * 30, error="upstream timeout")
        self._turn("第二轮问题也需要足够长才能通过闸门", "第二轮回答内容" * 30)
        self._compact_first(1)
        out = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertEqual(out["inserted"], 0)
        self.assertEqual(out["gated"], 1)

    def test_greeting_turn_gated(self):
        self._turn("好的", "已完成")
        self._turn("第二轮问题也需要足够长才能通过闸门", "第二轮回答内容" * 30)
        self._compact_first(1)
        out = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertEqual(out["gated"], 1)
        self.assertEqual(out["inserted"], 0)

    def test_near_duplicate_suppressed(self):
        q = "请帮我把会话压缩的触发阈值改成按 token 计算，并且加上熔断机制"
        a = "已经改好了，主要改动集中在预算计算与摘要落库两处。" * 10
        self._turn(q, a)
        self._turn(q + "。", a)
        self._turn("第三轮问题也需要足够长才能通过闸门", "第三轮回答内容" * 30)
        self._compact_first(2)
        out = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertEqual(out["dup"], 1)

    def test_disabled_by_default(self):
        self._turn("一个足够长的问题，用来通过长度检查" * 3, "回答内容" * 30)
        self._compact_first(1)
        with mock.patch.object(settings, "CHAT_MEMORY_ARCHIVE_RAW_ENABLED", False):
            out = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertEqual(out, {"inserted": 0, "gated": 0, "dup": 0})
        self.assertEqual(self.fake.rows, {})

    def test_idempotent_rerun(self):
        self._turn("一个足够长的问题，用来通过长度检查" * 3, "回答内容" * 30)
        self._turn("第二轮问题也需要足够长才能通过闸门", "第二轮回答内容" * 30)
        self._compact_first(1)
        first = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        second = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertGreater(first["inserted"], 0)
        self.assertEqual(second["inserted"], 0)

    def test_evicted_turns_not_rearchived(self):
        """被滚动上限删掉的轮次要前移水位线，否则下一轮又会被重新归档。"""
        self._turn("一个足够长的问题，用来通过长度检查" * 3, "回答内容" * 30)
        self._turn("第二轮问题也需要足够长才能通过闸门", "第二轮回答内容" * 30)
        keys = self._compact_first(2)
        ma.archive_raw_turns(self.uid, self.aid, self.sid)
        meta = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        evicted = keys[0]

        def _patch(m):
            return {ma._META_EVICTED_BELOW: evicted, ma._META_ARCHIVED_TURN_KEYS: []}

        self.storage.mutate_session_metadata(self.uid, self.aid, self.sid, _patch)
        self.fake.rows.clear()
        out = ma.archive_raw_turns(self.uid, self.aid, self.sid)
        self.assertEqual(out["inserted"], 0)
        self.assertIn(evicted, meta.get("memory_archived_turn_keys") or [])


class RetrievalFilterTest(unittest.TestCase):
    """检索侧：episodic/factual 双路 + raw 白名单超长时的降级过滤。"""

    def setUp(self):
        self.fake = _FakeMilvus()
        patches = [
            mock.patch("app.chat.memory_search.get_chat_memory_milvus", return_value=self.fake),
            mock.patch(
                "app.chat.memory_search.get_multimodal_embedding_service", return_value=_FakeEmbedder()
            ),
            mock.patch("app.chat.memory_search.rewrite_memory_query", side_effect=lambda q, cfg: q),
            mock.patch.object(settings, "EMBEDDING_API_KEY", "sk-test"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sess = session_memory_scope(1, 2, "s1")
        self.usr = user_memory_scope(1, 2)
        self.llm = {"api_key": "k", "model_name": "m"}

    def _seed(self, scope, kind, chunk_id, **kw):
        self.fake.upsert(
            [
                {
                    "chunk_id": chunk_id,
                    "memory_scope": scope,
                    "kind": kind,
                    "text": f"内容 {chunk_id}",
                    **kw,
                }
            ]
        )

    def _retrieve(self, **kw):
        from app.chat.memory_search import retrieve_session_memory_hits

        args = dict(
            user_id=1,
            agent_id=2,
            session_id="s1",
            llm_config=self.llm,
            top_k=5,
        )
        args.update(kw)
        return retrieve_session_memory_hits("查询", **args)

    def test_episodic_restricted_to_reachable_segments(self):
        """只有当前路径可达的段能被召回，其他分支的段必须被过滤掉。"""
        self._seed(self.sess, KIND_EPISODIC, "ep_ok", to_turn_key=100)
        self._seed(self.sess, KIND_EPISODIC, "ep_other_branch", to_turn_key=999)
        hits, _ = self._retrieve(allowed_to_keys=[100])
        ids = {h["chunk_id"] for h in hits}
        self.assertIn("ep_ok", ids)
        self.assertNotIn("ep_other_branch", ids)

    def test_empty_allowed_to_keys_blocks_episodic(self):
        self._seed(self.sess, KIND_EPISODIC, "ep1", to_turn_key=100)
        with mock.patch.object(settings, "CHAT_MEMORY_FACTUAL_ENABLED", False):
            hits, _ = self._retrieve(allowed_to_keys=[])
        self.assertEqual(hits, [])

    def test_facts_come_from_user_scope(self):
        """用户级事实跨会话可见，但绝不跨用户/跨智能体。"""
        self._seed(self.usr, KIND_FACTUAL, "fa_mine", fact_type="preference")
        self._seed(user_memory_scope(999, 2), KIND_FACTUAL, "fa_other_user", fact_type="preference")
        self._seed(user_memory_scope(1, 777), KIND_FACTUAL, "fa_other_agent", fact_type="preference")
        hits, _ = self._retrieve(allowed_to_keys=[])
        ids = {h["chunk_id"] for h in hits}
        self.assertEqual(ids, {"fa_mine"})

    def test_session_facts_not_leaked_to_other_sessions(self):
        self._seed(self.sess, KIND_FACTUAL, "fa_sess", fact_type="decision")
        self._seed(session_memory_scope(1, 2, "别的会话"), KIND_FACTUAL, "fa_other", fact_type="decision")
        hits, _ = self._retrieve(allowed_to_keys=[], include_facts=False)
        self.assertEqual(hits, [])  # 会话级 decision 不走用户级检索通道

    def test_facts_disabled_by_config(self):
        self._seed(self.usr, KIND_FACTUAL, "fa1", fact_type="preference")
        with mock.patch.object(settings, "CHAT_MEMORY_FACTUAL_ENABLED", False):
            hits, _ = self._retrieve(allowed_to_keys=[])
        self.assertEqual(hits, [])

    def test_raw_filtered_in_python_when_allowlist_too_long(self):
        """白名单超过上限时不下推表达式，改为过采样 + Python 侧精确过滤。"""
        self._seed(self.sess, KIND_RAW, "raw_in", turn_key=5)
        self._seed(self.sess, KIND_RAW, "raw_out", turn_key=7777)
        long_allow = list(range(1, 400))
        with mock.patch.object(settings, "CHAT_MEMORY_ARCHIVE_RAW_ENABLED", True), mock.patch.object(
            settings, "CHAT_MEMORY_FILTER_MAX_KEYS", 50
        ):
            hits, _ = self._retrieve(allowed_to_keys=[], allowed_turn_keys=long_allow)
        ids = {h["chunk_id"] for h in hits}
        self.assertIn("raw_in", ids)
        self.assertNotIn("raw_out", ids)

    def test_raw_skipped_when_disabled(self):
        self._seed(self.sess, KIND_RAW, "raw1", turn_key=5)
        with mock.patch.object(settings, "CHAT_MEMORY_ARCHIVE_RAW_ENABLED", False), mock.patch.object(
            settings, "CHAT_MEMORY_FACTUAL_ENABLED", False
        ):
            hits, _ = self._retrieve(allowed_to_keys=[], allowed_turn_keys=[5])
        self.assertEqual(hits, [])

    def test_no_embedding_key_returns_empty(self):
        with mock.patch.object(settings, "EMBEDDING_API_KEY", ""):
            hits, rew = self._retrieve(allowed_to_keys=[1])
        self.assertEqual((hits, rew), ([], ""))


if __name__ == "__main__":
    unittest.main()
