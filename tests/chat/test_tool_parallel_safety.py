"""并行工具共享状态（_RequestState）的并发安全回归。

LangGraph 会并行执行同一条 AIMessage 里的多个 tool_calls：同步路径经
ContextThreadPoolExecutor 多线程、异步路径 asyncio.gather。这些工具共享同一个
_RequestState（contextvars 复制的是同一对象引用），因此配额计数必须原子、
RAG 上下文必须合并而非互相覆盖。

本测试用 copy_context() + ThreadPoolExecutor 复现「多线程共享同一 state」，
不依赖 PostgreSQL/Redis/Milvus 等外部服务。
"""

from __future__ import annotations

import contextvars
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.chat.tools import (
    _set_last_rag_context,
    add_pending_mcp_confirmation,
    get_approved_mcp_pending_id,
    get_last_rag_context,
    get_pending_mcp_confirmations,
    reset_tool_call_guards,
    set_approved_mcp_pending_id,
    try_acquire_fetch_url_tool_slot,
    try_acquire_knowledge_tool_slot,
    try_acquire_web_search_tool_slot,
)


def run_concurrently(fn, n, *, switch_interval: float | None = 1e-6):
    """在 n 个线程中「同时」执行 fn(i)，并强制共享调用方线程的 contextvars 绑定。

    ThreadPoolExecutor 默认不复制 context，故先在本线程 copy_context()，
    再把每个 Context 交给对应线程 run —— 各 Context 绑定到同一个 _RequestState 实例，
    从而复现真实的并行工具场景。
    """
    prev = sys.getswitchinterval()
    if switch_interval is not None:
        sys.setswitchinterval(switch_interval)
    try:
        contexts = [contextvars.copy_context() for _ in range(n)]
        barrier = threading.Barrier(n)

        def worker(i: int):
            barrier.wait()
            return contexts[i].run(fn, i)

        with ThreadPoolExecutor(max_workers=n) as ex:
            return list(ex.map(worker, range(n)))
    finally:
        sys.setswitchinterval(prev)


class _StateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # 在当前线程上下文绑定一个 _RequestState，并清空上一测试遗留状态
        reset_tool_call_guards()
        get_last_rag_context(clear=True)
        get_pending_mcp_confirmations(clear=True)
        set_approved_mcp_pending_id(None)

    def tearDown(self) -> None:
        get_last_rag_context(clear=True)
        get_pending_mcp_confirmations(clear=True)


class QuotaGuardConcurrencyTests(_StateTestBase):
    def test_knowledge_slot_only_one_winner(self):
        results = run_concurrently(lambda _i: try_acquire_knowledge_tool_slot(), 16)
        self.assertEqual(sum(1 for r in results if r), 1)

    def test_web_search_slot_respects_cap(self):
        from app.settings import settings

        cap = max(1, int(getattr(settings, "WEB_SEARCH_MAX_CALLS_PER_TURN", 2)))
        results = run_concurrently(lambda _i: try_acquire_web_search_tool_slot(), 16)
        self.assertEqual(sum(1 for r in results if r), cap)

    def test_fetch_url_slot_respects_cap(self):
        from app.settings import settings

        cap = max(1, int(getattr(settings, "WEB_SEARCH_FETCH_MAX_CALLS_PER_TURN", 3)))
        results = run_concurrently(lambda _i: try_acquire_fetch_url_tool_slot(), 16)
        self.assertEqual(sum(1 for r in results if r), cap)

    def test_state_lock_is_reentrant(self):
        from app.chat.tools import _state

        lock = _state()._lock
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
        finally:
            lock.release()


class RagContextMergeTests(_StateTestBase):
    def test_sources_are_merged_not_clobbered(self):
        _set_last_rag_context({"kb_sources": [{"index": 1, "url": "https://kb/1"}]})
        _set_last_rag_context({"web_sources": [{"index": 1, "url": "https://web/1"}]})
        ctx = get_last_rag_context()
        self.assertEqual([s["url"] for s in ctx["kb_sources"]], ["https://kb/1"])
        self.assertEqual([s["url"] for s in ctx["web_sources"]], ["https://web/1"])

    def test_empty_sources_do_not_wipe_existing(self):
        _set_last_rag_context({"web_sources": [{"index": 1, "url": "https://a/1"}]})
        _set_last_rag_context({"web_sources": []})
        ctx = get_last_rag_context()
        self.assertEqual(len(ctx["web_sources"]), 1)

    def test_duplicate_sources_are_deduped(self):
        _set_last_rag_context({"web_sources": [{"index": 1, "url": "https://a/1"}]})
        _set_last_rag_context({"web_sources": [{"index": 5, "url": "https://a/1"}]})
        ctx = get_last_rag_context()
        self.assertEqual(len(ctx["web_sources"]), 1)

    def test_kb_trace_has_priority_over_memory(self):
        _set_last_rag_context({"rag_trace": {"tool_name": "read_user_memory"}})
        _set_last_rag_context(
            {"rag_trace": {"tool_name": "search_knowledge_base"}, "kb_sources": []}
        )
        ctx = get_last_rag_context()
        self.assertEqual(ctx["rag_trace"]["tool_name"], "search_knowledge_base")

    def test_non_kb_trace_does_not_override_kb(self):
        _set_last_rag_context(
            {"rag_trace": {"tool_name": "search_knowledge_base"}, "kb_sources": [{"index": 1}]}
        )
        _set_last_rag_context({"rag_trace": {"tool_name": "read_user_memory"}})
        ctx = get_last_rag_context()
        self.assertEqual(ctx["rag_trace"]["tool_name"], "search_knowledge_base")

    def test_memory_trace_fills_when_empty(self):
        _set_last_rag_context({"rag_trace": {"tool_name": "read_user_memory"}})
        ctx = get_last_rag_context()
        self.assertEqual(ctx["rag_trace"]["tool_name"], "read_user_memory")

    def test_concurrent_sources_merge_all(self):
        def writer(i: int):
            if i % 2 == 0:
                _set_last_rag_context({"kb_sources": [{"index": i, "url": f"https://kb/{i}"}]})
            else:
                _set_last_rag_context({"web_sources": [{"index": i, "url": f"https://web/{i}"}]})

        run_concurrently(writer, 10)
        ctx = get_last_rag_context()
        self.assertEqual(len(ctx["kb_sources"]), 5)
        self.assertEqual(len(ctx["web_sources"]), 5)


class McpConfirmationConcurrencyTests(_StateTestBase):
    def test_same_pending_added_once(self):
        pending = {"tool_name": "send_email", "args_hash": "h1", "pending_id": "p1"}

        def add(_i: int):
            return add_pending_mcp_confirmation(dict(pending))

        results = run_concurrently(add, 8)
        statuses = [r["status"] for r in results]
        self.assertEqual(statuses.count("added"), 1)
        self.assertEqual(statuses.count("duplicate"), 7)
        self.assertEqual(len(get_pending_mcp_confirmations(clear=False)), 1)

    def test_cap_enforced(self):
        from app.settings import settings

        cap = max(1, int(getattr(settings, "MCP_CONFIRMATION_MAX_PER_TURN", 3)))
        seen = {"added": 0, "capped": 0}
        for i in range(cap + 5):
            gate = add_pending_mcp_confirmation(
                {"tool_name": "mcp_write", "args_hash": f"h{i}", "pending_id": f"p{i}"}
            )
            seen[gate["status"]] = seen.get(gate["status"], 0) + 1
        self.assertEqual(seen["added"], cap)
        self.assertEqual(seen["capped"], 5)

    def test_approved_id_consumed_once(self):
        set_approved_mcp_pending_id("abc")

        results = run_concurrently(lambda _i: get_approved_mcp_pending_id(clear=True), 8)
        self.assertEqual([r for r in results if r], ["abc"])


if __name__ == "__main__":
    unittest.main()
