"""
分段摘要存储与压缩主流程单测：SQLite 内存库替代 PostgreSQL。
覆盖段链前缀匹配、跨分支隔离、归并判定、幂等 upsert、熔断、失败降级、端到端压缩视图。
不打真实 LLM（摘要器被 mock）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.chat.compact_store as store_mod
import app.chat.storage as storage_mod
from app.chat.compact import (
    _failures_patch,
    _should_merge_chain,
    attempt_compaction,
    build_compacted_model_messages,
    is_breaker_tripped,
    legacy_summary_and_keep,
    render_summary_block,
    verbatim_keep_from_for_session,
)
from app.chat.compact_store import (
    chain_for_path,
    covered_turn_count,
    delete_segments,
    is_valid_for_path,
    load_segments,
    seg_id_of,
    upsert_segment,
)
from app.chat.context_budget import budget_for
from app.chat.db_models import ChatCompactSegment, ChatMessage as ChatMessageRow, ChatSession as ChatSessionRow
from app.chat.tool_result_compact import CLEARED_PLACEHOLDER
from app.settings import settings


class _MemoryCache:
    """Redis 不可用时的内存桩。"""

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


def _seg(from_index, to_index, from_key, to_key, summary="s", level=0, seg_id=None):
    return {
        "seg_id": seg_id or seg_id_of(from_key, to_key),
        "from_index": from_index,
        "to_index": to_index,
        "from_turn_key": from_key,
        "to_turn_key": to_key,
        "level": level,
        "summary": summary,
        "tokens": len(summary),
    }


class ChainMatchTest(unittest.TestCase):
    """段链匹配是纯函数，不需要数据库。"""

    def test_contiguous_chain_selected(self):
        segs = [_seg(0, 3, 10, 14), _seg(3, 6, 16, 20)]
        chain = chain_for_path(segs, [10, 12, 14, 16, 18, 20, 22])
        self.assertEqual(len(chain), 2)
        self.assertEqual(covered_turn_count(chain), 6)

    def test_gap_breaks_chain(self):
        segs = [_seg(0, 3, 10, 14), _seg(4, 6, 18, 22)]
        chain = chain_for_path(segs, [10, 12, 14, 16, 18, 20, 22])
        self.assertEqual(len(chain), 1)
        self.assertEqual(covered_turn_count(chain), 3)

    def test_segment_not_on_path_is_skipped(self):
        """分支分叉后，覆盖旧分支轮次的段不得生效。"""
        segs = [_seg(0, 3, 10, 99)]  # 99 不在当前路径
        self.assertEqual(chain_for_path(segs, [10, 12, 14, 16]), [])

    def test_higher_level_wins_same_from_index(self):
        segs = [_seg(0, 6, 10, 20, summary="细", level=0), _seg(0, 6, 10, 20, summary="粗", level=1)]
        chain = chain_for_path(segs, [10, 12, 14, 16, 18, 20])
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["summary"], "粗")

    def test_out_of_range_indices_rejected(self):
        self.assertFalse(is_valid_for_path(_seg(0, 99, 10, 12), [10, 12]))
        self.assertFalse(is_valid_for_path(_seg(-1, 2, 10, 12), [10, 12]))
        self.assertFalse(is_valid_for_path(_seg(2, 2, 10, 12), [10, 12, 14]))

    def test_empty_inputs(self):
        self.assertEqual(chain_for_path([], [10, 12]), [])
        self.assertEqual(chain_for_path([_seg(0, 1, 10, 10)], []), [])
        self.assertEqual(covered_turn_count([]), 0)


class LegacyFallbackTest(unittest.TestCase):
    def test_legacy_state_used_when_no_segments(self):
        meta = {"compact_summary": "旧摘要", "compact_until_turn_index": 1}
        summary, keep = legacy_summary_and_keep(meta, [10, 12, 14], 3)
        self.assertEqual(summary, "旧摘要")
        self.assertEqual(keep, 2)

    def test_no_state_at_all(self):
        self.assertEqual(legacy_summary_and_keep({}, [10, 12], 2), ("", 0))


class MergeDecisionTest(unittest.TestCase):
    def test_no_merge_when_chain_short(self):
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_SEGMENTS", 8), mock.patch.object(
            settings, "CHAT_COMPACT_SEGMENTS_TOKEN_BUDGET", 4000
        ):
            self.assertFalse(_should_merge_chain([]))
            self.assertFalse(_should_merge_chain([_seg(0, 3, 10, 14, summary="x" * 100)]))

    def test_merge_when_too_many_segments(self):
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_SEGMENTS", 3):
            segs = [_seg(i, i + 1, 100 + i, 100 + i, summary="x") for i in range(3)]
            self.assertTrue(_should_merge_chain(segs))

    def test_merge_when_summary_tokens_over_budget(self):
        """段链摘要总 token 超预算即归并；预算有 512 的下限保护。"""
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_SEGMENTS", 8), mock.patch.object(
            settings, "CHAT_COMPACT_SEGMENTS_TOKEN_BUDGET", 600
        ):
            a = _seg(0, 2, 10, 12, summary="长" * 400, level=0)
            b = _seg(2, 4, 14, 16, summary="长" * 400, level=0)
            self.assertEqual(a["tokens"], 400)
            self.assertTrue(_should_merge_chain([a, b]))  # 800 > 600
            self.assertFalse(_should_merge_chain([a]))  # 400 < 600

    def test_token_budget_has_floor(self):
        """配置成极小值时仍按 512 下限判定，避免每轮都归并。"""
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_SEGMENTS", 8), mock.patch.object(
            settings, "CHAT_COMPACT_SEGMENTS_TOKEN_BUDGET", 1
        ):
            segs = [_seg(i, i + 1, 10 + i, 10 + i, summary="x" * 100) for i in range(4)]
            self.assertFalse(_should_merge_chain(segs))  # 400 < 512


class BreakerTest(unittest.TestCase):
    def test_not_tripped_initially(self):
        self.assertFalse(is_breaker_tripped({}))
        self.assertFalse(is_breaker_tripped(None))

    def test_trips_at_threshold(self):
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_CONSECUTIVE_FAILURES", 3):
            meta = {}
            for _ in range(2):
                meta.update(_failures_patch(meta, failed=True, error="boom"))
                self.assertFalse(is_breaker_tripped(meta))
            meta.update(_failures_patch(meta, failed=True, error="boom"))
            self.assertTrue(is_breaker_tripped(meta))

    def test_success_resets(self):
        meta = {"compact_failures": {"count": 5, "tripped_at": "x"}}
        patch = _failures_patch(meta, failed=False)
        self.assertEqual(patch["compact_failures"]["count"], 0)
        self.assertFalse(is_breaker_tripped(patch))


class SegmentStoreSqliteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls._orig_store_sl = store_mod.SessionLocal
        cls._orig_storage_sl = storage_mod.SessionLocal
        cls._orig_cache = storage_mod.cache
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal = cls._orig_store_sl
        storage_mod.SessionLocal = cls._orig_storage_sl
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        self.uid, self.aid = 910001, 910001
        self.sid = f"seg_{self.id().split('.')[-1]}"
        # 建会话行 + 6 轮对话（12 条消息）
        for i in range(6):
            self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content=f"问{i}")])
            self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage(content=f"答{i}")])
        self.ref = self.storage.get_session_ref_id(self.uid, self.aid, self.sid)
        self.path_ids = [r["message_id"] for r in self.storage.get_session_messages(self.uid, self.aid, self.sid)]
        self.turn_keys = self.path_ids[0::2]
        self.assertIsNotNone(self.ref)

    def test_upsert_and_load(self):
        sid = upsert_segment(
            self.ref,
            from_index=0,
            to_index=3,
            from_turn_key=self.turn_keys[0],
            to_turn_key=self.turn_keys[2],
            summary="摘要A",
            tokens=3,
        )
        self.assertEqual(sid, seg_id_of(self.turn_keys[0], self.turn_keys[2]))
        segs = load_segments(self.ref)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["summary"], "摘要A")

    def test_upsert_is_idempotent(self):
        """同区间重复写入必须更新而非新增（并发/重试安全）。"""
        for text in ("v1", "v2", "v3"):
            upsert_segment(
                self.ref,
                from_index=0,
                to_index=2,
                from_turn_key=self.turn_keys[0],
                to_turn_key=self.turn_keys[1],
                summary=text,
            )
        segs = load_segments(self.ref)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["summary"], "v3")

    def test_empty_summary_rejected(self):
        self.assertEqual(
            upsert_segment(
                self.ref,
                from_index=0,
                to_index=2,
                from_turn_key=self.turn_keys[0],
                to_turn_key=self.turn_keys[1],
                summary="   ",
            ),
            "",
        )
        self.assertEqual(load_segments(self.ref), [])

    def test_delete_segments(self):
        a = upsert_segment(
            self.ref, from_index=0, to_index=2, from_turn_key=self.turn_keys[0], to_turn_key=self.turn_keys[1], summary="A"
        )
        upsert_segment(
            self.ref, from_index=2, to_index=4, from_turn_key=self.turn_keys[2], to_turn_key=self.turn_keys[3], summary="B"
        )
        self.assertEqual(delete_segments(self.ref, [a]), 1)
        self.assertEqual([s["summary"] for s in load_segments(self.ref)], ["B"])

    def test_chain_from_db_matches_path(self):
        upsert_segment(
            self.ref, from_index=0, to_index=3, from_turn_key=self.turn_keys[0], to_turn_key=self.turn_keys[2], summary="A"
        )
        chain = chain_for_path(load_segments(self.ref), self.turn_keys)
        self.assertEqual(covered_turn_count(chain), 3)

    def test_keep_from_uses_segments(self):
        upsert_segment(
            self.ref, from_index=0, to_index=4, from_turn_key=self.turn_keys[0], to_turn_key=self.turn_keys[3], summary="A"
        )
        keep = verbatim_keep_from_for_session(
            self.uid, self.aid, self.sid, path_turn_keys=self.turn_keys, meta={}, session_ref_id=self.ref
        )
        self.assertEqual(keep, 4)

    def test_keep_from_falls_back_to_legacy_meta(self):
        keep = verbatim_keep_from_for_session(
            self.uid,
            self.aid,
            self.sid,
            path_turn_keys=self.turn_keys,
            meta={"compact_summary": "旧", "compact_until_turn_index": 1},
            session_ref_id=self.ref,
        )
        self.assertEqual(keep, 2)

    def test_other_session_segments_isolated(self):
        upsert_segment(
            self.ref, from_index=0, to_index=2, from_turn_key=self.turn_keys[0], to_turn_key=self.turn_keys[1], summary="A"
        )
        self.assertEqual(load_segments(self.ref + 999999), [])
        self.assertEqual(load_segments(None), [])


class AttemptCompactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls._orig_store_sl = store_mod.SessionLocal
        cls._orig_storage_sl = storage_mod.SessionLocal
        cls._orig_cache = storage_mod.cache
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal = cls._orig_store_sl
        storage_mod.SessionLocal = cls._orig_storage_sl
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        self.uid, self.aid = 920001, 920001
        self.sid = f"ac_{self.id().split('.')[-1]}"
        # 每轮约 2400 token（中文按 1 字≈1 token），配合 keep_tokens=1000 才能真的挤出轮次
        for i in range(6):
            self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content=f"问题{i}" + "问" * 1200)])
            self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage(content=f"回答{i}" + "答" * 1200)])
        self.ref = self.storage.get_session_ref_id(self.uid, self.aid, self.sid)
        recs = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        self.path_ids = [r["message_id"] for r in recs]
        self.turn_keys = self.path_ids[0::2]
        self.turns = [
            [HumanMessage(content=recs[i]["content"]), AIMessage(content=recs[i + 1]["content"])]
            for i in range(0, len(recs), 2)
        ]
        self.llm = {"api_key": "sk-test", "model_name": "m1", "base_url": ""}
        # keep_tokens 压小，使 _choose_keep_from 真的会丢掉较早轮次
        self._keep_patch = mock.patch.object(settings, "CHAT_COMPACT_KEEP_TOKENS", 1000)
        self._cjk_patch = mock.patch.object(settings, "CHAT_TOKENS_PER_CJK_CHAR", 1.0)
        self._keep_patch.start()
        self._cjk_patch.start()
        self.addCleanup(self._keep_patch.stop)
        self.addCleanup(self._cjk_patch.stop)

    def _call(self, keep_from=0, chain=None, meta=None, **kw):
        args = dict(
            user_id=self.uid,
            agent_id=self.aid,
            session_id=self.sid,
            turns=self.turns,
            turn_keys=self.turn_keys,
            chain=chain or [],
            legacy_summary="",
            keep_from=keep_from,
            budget=budget_for(128_000),
            factor=1.0,
            llm_config=self.llm,
            meta=meta if meta is not None else {},
            session_ref_id=self.ref,
            estimated=999_999,
        )
        args.update(kw)
        return attempt_compaction(**args)

    def test_success_writes_segment_and_advances_keep_from(self):
        with mock.patch(
            "app.chat.compact.run_summarizer",
            return_value=("1. Primary Request and Intent\n- 用户要 X", []),
        ):
            res = self._call(keep_from=0)
        self.assertTrue(res["ok"])
        self.assertGreater(res["keep_from"], 0)
        self.assertEqual(len(res["chain"]), 1)
        segs = load_segments(self.ref)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["to_index"], res["keep_from"])

    def test_emits_visible_compacting_status(self):
        """同步压缩会阻塞首 token，必须先 emit 一条「进行中」状态供前端展示。"""
        with mock.patch(
            "app.chat.compact.run_summarizer", return_value=("摘要", [])
        ), mock.patch("app.chat.tools.emit_rag_step") as emit:
            res = self._call(keep_from=0)
        self.assertTrue(res["ok"])
        labels = [c.args[1] for c in emit.call_args_list if len(c.args) > 1]
        self.assertIn("正在进行上下文压缩", labels)

    def test_success_keeps_legacy_keys_as_branch_fallback(self):
        """成功压缩不清理 v1 键：v2 段链优先，v1 作为其它分支的兜底摘要保留。"""
        # 先把 v1 metadata 落库，才能验证压缩后它未被抹掉
        self.storage.mutate_session_metadata(
            self.uid,
            self.aid,
            self.sid,
            lambda m: {
                "compact_summary": "旧",
                "compact_until_turn_index": 1,
                "compact_states": [{"covered_turn_keys": [1], "summary": "旧分支"}],
            },
        )
        meta = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        with mock.patch("app.chat.compact.run_summarizer", return_value=("摘要", [])):
            res = self._call(keep_from=2, meta=meta, legacy_summary="旧")
        self.assertTrue(res["ok"])
        after = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        # v1 键保留，供尚未 v2 化的分支兜底
        self.assertEqual(after.get("compact_summary"), "旧")
        self.assertEqual(after.get("compact_until_turn_index"), 1)
        self.assertEqual(after.get("compact_states"), [{"covered_turn_keys": [1], "summary": "旧分支"}])
        # 压缩历史与熔断计数照常写入
        self.assertEqual(len(after["compact_history"]), 1)
        self.assertEqual(after["compact_history"][0]["auto"], True)
        self.assertEqual(after["compact_failures"]["count"], 0)

    def test_failure_increments_breaker_and_degrades(self):
        with mock.patch("app.chat.compact.run_summarizer", return_value=None):
            res = self._call(keep_from=0)
        self.assertFalse(res["ok"])
        self.assertTrue(res["degraded"])
        self.assertEqual(res["reason"], "summarizer_failed")
        self.assertEqual(load_segments(self.ref), [])
        after = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        self.assertEqual(after["compact_failures"]["count"], 1)
        # 失败时仍按新窗口截断本轮，避免直接溢出
        self.assertGreater(res["keep_from"], 0)

    def test_tripped_breaker_skips_llm(self):
        meta = {"compact_failures": {"count": 3, "tripped_at": "x"}}
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_CONSECUTIVE_FAILURES", 3), mock.patch(
            "app.chat.compact.run_summarizer"
        ) as m:
            res = self._call(keep_from=0, meta=meta)
        m.assert_not_called()
        self.assertEqual(res["reason"], "breaker_tripped")
        self.assertTrue(res["degraded"])

    def test_missing_api_key_does_not_trip_breaker(self):
        """缺 Key 是配置问题，不是压缩失败：绝不能计入熔断，否则预压缩会把压缩永久熔断。"""
        with mock.patch("app.chat.compact.run_summarizer") as m:
            res = self._call(keep_from=0, llm_config={"api_key": "", "model_name": "m1"})
        m.assert_not_called()
        self.assertEqual(res["reason"], "no_api_key")
        self.assertTrue(res["degraded"])
        after = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        self.assertNotIn("compact_failures", after)
        # 连续多轮缺 Key 后仍不得熔断
        for _ in range(5):
            self._call(keep_from=0, llm_config={"api_key": "   ", "model_name": "m1"})
        after = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        self.assertFalse(is_breaker_tripped(after))

    def test_real_failure_still_trips_breaker(self):
        """对照：真正的摘要失败要照常累加熔断计数。"""
        with mock.patch("app.chat.compact.run_summarizer", return_value=None):
            for _ in range(3):
                self._call(keep_from=0)
        after = self.storage.get_session_metadata(self.uid, self.aid, self.sid)
        self.assertTrue(is_breaker_tripped(after))

    def test_manual_instructions_bypass_breaker(self):
        meta = {"compact_failures": {"count": 9, "tripped_at": "x"}}
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_CONSECUTIVE_FAILURES", 3), mock.patch(
            "app.chat.compact.run_summarizer", return_value=("摘要", [])
        ) as m:
            res = self._call(keep_from=0, meta=meta, auto=False, instructions="重点保留 schema")
        self.assertTrue(res["ok"])
        self.assertTrue(m.called)
        prompt_kwargs = m.call_args.kwargs
        self.assertIn("重点保留 schema", prompt_kwargs["dropped_text"])
        self.assertFalse(prompt_kwargs["suppress_follow_up"])

    def test_nothing_to_drop(self):
        res = self._call(keep_from=len(self.turns))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no_turns_to_drop")

    def test_merge_chain_replaces_old_segments(self):
        """段数超限时，本次压缩把旧链归并成一段并删除被合并的旧段。"""
        old = []
        for i in range(3):
            sid = upsert_segment(
                self.ref,
                from_index=i,
                to_index=i + 1,
                from_turn_key=self.turn_keys[i],
                to_turn_key=self.turn_keys[i],
                summary=f"旧段{i}",
            )
            old.append(_seg(i, i + 1, self.turn_keys[i], self.turn_keys[i], summary=f"旧段{i}", seg_id=sid))
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_SEGMENTS", 3), mock.patch(
            "app.chat.compact.run_summarizer", return_value=("归并后的摘要", [])
        ) as m:
            res = self._call(keep_from=3, chain=old)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["chain"]), 1)
        self.assertEqual(res["chain"][0]["level"], 1)
        self.assertEqual(res["chain"][0]["from_index"], 0)
        segs = load_segments(self.ref)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["summary"], "归并后的摘要")
        # 归并时旧链摘要作为 old_summary 输入，避免信息凭空消失
        self.assertIn("旧段0", m.call_args.kwargs["old_summary"])

    def test_legacy_v1_summary_folded_into_anchored_segment(self):
        """存量 v1 单摘要迁到 v2：必须并入本次摘要输入，且新段锚定到 0。

        否则新段 from_index>0，段链「必须从 0 连续」的匹配会返回空链，
        下一轮刚写的摘要被整体忽略、全部原文重新涌回上下文。
        """
        with mock.patch("app.chat.compact.run_summarizer", return_value=("迁移后的摘要", [])) as m:
            res = self._call(keep_from=2, legacy_summary="存量v1摘要内容")
        self.assertTrue(res["ok"])
        self.assertEqual(res["chain"][0]["from_index"], 0)
        self.assertIn("存量v1摘要内容", m.call_args.kwargs["old_summary"])
        self.assertEqual(res["chain"][0]["level"], 0)
        # 段链可从 0 连续匹配，原文窗口按新段推进（而不是回落到 0）
        keep = verbatim_keep_from_for_session(
            self.uid,
            self.aid,
            self.sid,
            path_turn_keys=self.turn_keys,
            meta={},
            session_ref_id=self.ref,
        )
        self.assertEqual(keep, res["keep_from"])
        self.assertGreater(keep, 2)

    def test_no_merge_appends_segment(self):
        first = upsert_segment(
            self.ref,
            from_index=0,
            to_index=2,
            from_turn_key=self.turn_keys[0],
            to_turn_key=self.turn_keys[1],
            summary="段一",
        )
        chain = [_seg(0, 2, self.turn_keys[0], self.turn_keys[1], summary="段一", seg_id=first)]
        with mock.patch.object(settings, "CHAT_COMPACT_MAX_SEGMENTS", 8), mock.patch.object(
            settings, "CHAT_COMPACT_SEGMENTS_TOKEN_BUDGET", 4000
        ), mock.patch("app.chat.compact.run_summarizer", return_value=("段二", [])):
            res = self._call(keep_from=2, chain=chain)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["chain"]), 2)
        self.assertEqual(res["chain"][1]["level"], 0)
        self.assertEqual(len(load_segments(self.ref)), 2)


class BuildCompactedViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls._orig_store_sl = store_mod.SessionLocal
        cls._orig_storage_sl = storage_mod.SessionLocal
        cls._orig_cache = storage_mod.cache
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal = cls._orig_store_sl
        storage_mod.SessionLocal = cls._orig_storage_sl
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        self.uid, self.aid = 930001, 930001
        self.sid = f"bv_{self.id().split('.')[-1]}"
        for i in range(5):
            self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content=f"问{i}")])
            self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage(content=f"答{i}")])
        self.ref = self.storage.get_session_ref_id(self.uid, self.aid, self.sid)
        recs = self.storage.get_session_messages(self.uid, self.aid, self.sid)
        self.path_ids = [r["message_id"] for r in recs]
        self.turn_keys = self.path_ids[0::2]
        self.messages = [
            m for r in recs for m in (
                [HumanMessage(content=r["content"])] if r["type"] == "human" else [AIMessage(content=r["content"])]
            )
        ]
        self.llm = {"api_key": "sk-test", "model_name": "m1", "base_url": ""}

    def _build(self, **kw):
        args = dict(
            user_id=self.uid,
            agent_id=self.aid,
            session_id=self.sid,
            llm_config=self.llm,
            system_prompt="系统提示词",
            tools_tokens=100,
            context_window=128_000,
            path_ids=self.path_ids,
        )
        args.update(kw)
        return build_compacted_model_messages(self.messages, **args)

    def test_below_trigger_returns_verbatim(self):
        out = self._build()
        self.assertEqual(len(out), len(self.messages))
        self.assertNotIn("会话压缩摘要", str(out[0].content))

    def test_budget_out_reports_estimate(self):
        holder = {}
        self._build(budget_out=holder)
        self.assertEqual(holder["window"], 128_000)
        self.assertEqual(holder["turn_count"], 5)
        self.assertEqual(holder["model_name"], "m1")
        self.assertGreater(holder["estimated"], 0)
        self.assertEqual(holder["trigger"], holder["effective"] - holder["buffer"] if "buffer" in holder else holder["trigger"])

    def test_over_trigger_compacts_and_injects_summary(self):
        upsert_segment(
            self.ref,
            from_index=0,
            to_index=3,
            from_turn_key=self.turn_keys[0],
            to_turn_key=self.turn_keys[2],
            summary="已有摘要内容",
        )
        out = self._build()
        blob = "\n".join(str(m.content) for m in out)
        self.assertIn("会话压缩摘要", blob)
        self.assertIn("已有摘要内容", blob)
        # 前 3 轮已被摘要覆盖，原文只剩后 2 轮 + 摘要消息
        self.assertEqual(len(out), 1 + 2 * 2)
        self.assertNotIn("问0", blob)
        self.assertIn("问4", blob)

    def test_summary_injected_as_human_message_after_system_prefix(self):
        msgs = [SystemMessage(content="sys")] + self.messages
        ids = [0] + self.path_ids
        upsert_segment(
            self.ref,
            from_index=0,
            to_index=4,
            from_turn_key=self.turn_keys[0],
            to_turn_key=self.turn_keys[3],
            summary="摘要Z",
        )
        out = build_compacted_model_messages(
            msgs,
            user_id=self.uid,
            agent_id=self.aid,
            session_id=self.sid,
            llm_config=self.llm,
            system_prompt="sys",
            context_window=128_000,
            path_ids=ids,
        )
        self.assertIsInstance(out[0], SystemMessage)
        self.assertIsInstance(out[1], HumanMessage)
        self.assertIn("摘要Z", str(out[1].content))

    def test_empty_messages_passthrough(self):
        self.assertEqual(
            build_compacted_model_messages(
                [],
                user_id=self.uid,
                agent_id=self.aid,
                session_id=self.sid,
                llm_config=self.llm,
                context_window=128_000,
            ),
            [],
        )

    def test_render_summary_block_used_for_injection(self):
        upsert_segment(
            self.ref,
            from_index=0,
            to_index=2,
            from_turn_key=self.turn_keys[0],
            to_turn_key=self.turn_keys[1],
            summary="段甲",
        )
        chain = load_segments(self.ref)
        text = render_summary_block(chain)
        self.assertIn("段甲", text)
        self.assertIn("延续", text)


class MicroCompactPreprocessingTest(unittest.TestCase):
    """压缩前的 micro-compact 预处理：先零成本清理，再决定是否付 LLM 摘要。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        ChatSessionRow.__table__.create(bind=cls.engine)
        ChatMessageRow.__table__.create(bind=cls.engine)
        ChatCompactSegment.__table__.create(bind=cls.engine)
        factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        cls._orig_store_sl = store_mod.SessionLocal
        cls._orig_storage_sl = storage_mod.SessionLocal
        cls._orig_cache = storage_mod.cache
        store_mod.SessionLocal = factory
        storage_mod.SessionLocal = factory
        storage_mod.cache = _MemoryCache()
        cls.storage = storage_mod.ConversationStorage()

    @classmethod
    def tearDownClass(cls):
        store_mod.SessionLocal = cls._orig_store_sl
        storage_mod.SessionLocal = cls._orig_storage_sl
        storage_mod.cache = cls._orig_cache

    def setUp(self):
        self.uid, self.aid = 950001, 950001
        self.sid = f"mc_{self.id().split('.')[-1]}"
        self.storage.append_messages(self.uid, self.aid, self.sid, [HumanMessage(content="开场")])
        self.storage.append_messages(self.uid, self.aid, self.sid, [AIMessage(content="好的")])
        self.llm = {"api_key": "sk-test", "model_name": "m1", "base_url": ""}

    def _messages_with_tools(self, sizes: list[int]) -> list:
        msgs: list = [SystemMessage(content="系统提示词"), HumanMessage(content="问题1")]
        for i, size in enumerate(sizes):
            msgs.append(
                AIMessage(
                    content="",
                    tool_calls=[{"name": "web_search", "args": {"query": f"q{i}"}, "id": f"c{i}"}],
                )
            )
            msgs.append(
                ToolMessage(content=f"T{i}-" + "x" * size, tool_call_id=f"c{i}", name="web_search")
            )
        msgs.extend([AIMessage(content="回答1"), HumanMessage(content="问题2"), AIMessage(content="回答2")])
        return msgs

    def _build(self, messages: list) -> tuple[list, dict]:
        holder: dict = {}
        out = build_compacted_model_messages(
            messages,
            user_id=self.uid,
            agent_id=self.aid,
            session_id=self.sid,
            llm_config=self.llm,
            system_prompt="系统提示词",
            context_window=128_000,
            path_ids=list(range(1000, 1000 + len(messages))),
            budget_out=holder,
        )
        return out, holder

    def test_projection_avoids_unnecessary_summary(self):
        """原始估算超触发点，但清理旧工具结果后已低于触发点 → 不调摘要器。"""
        messages = self._messages_with_tools([300_000, 300_000])
        with mock.patch.object(settings, "CHAT_MICROCOMPACT_ENABLED", True), mock.patch.object(
            settings, "CHAT_MICROCOMPACT_TRIGGER_RATIO", 0.05
        ), mock.patch.object(settings, "CHAT_MICROCOMPACT_KEEP_RECENT", 1), mock.patch(
            "app.chat.compact.run_summarizer"
        ) as rs:
            out, holder = self._build(messages)
        rs.assert_not_called()
        blob = "\n".join(str(m.content) for m in out)
        self.assertIn(CLEARED_PLACEHOLDER, blob)
        self.assertNotIn("会话压缩摘要", blob)
        self.assertGreaterEqual(holder["microcompacted"]["cleared"], 1)

    def test_summarizer_sees_cleaned_input_when_still_over_trigger(self):
        """清理后仍超触发点 → 摘要器输入是清理后的文本（旧工具结果只剩占位符）。"""
        # 25 个工具结果，保留最近 20 个（每个被截断到 6000 token，合计仍超触发点）
        messages = self._messages_with_tools([30_000] * 25)
        with mock.patch.object(settings, "CHAT_MICROCOMPACT_ENABLED", True), mock.patch.object(
            settings, "CHAT_MICROCOMPACT_TRIGGER_RATIO", 0.05
        ), mock.patch.object(settings, "CHAT_MICROCOMPACT_KEEP_RECENT", 20), mock.patch.object(
            settings, "CHAT_COMPACT_KEEP_TOKENS", 1000
        ), mock.patch("app.chat.compact.run_summarizer", return_value=("摘要", [])) as rs:
            out, holder = self._build(messages)
        self.assertTrue(rs.called)
        dropped_text = rs.call_args.kwargs["dropped_text"]
        self.assertIn(CLEARED_PLACEHOLDER, dropped_text)
        self.assertNotIn("T0-", dropped_text)


if __name__ == "__main__":
    unittest.main()
