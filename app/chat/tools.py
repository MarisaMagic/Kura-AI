"""Agent 工具共享状态（知识库/记忆/联网检索由各自工具模块动态绑定）。

并发安全：全部状态挂在按请求创建的 _RequestState 上，经 ContextVar 分发。
asyncio.create_task / asyncio.to_thread / LangChain ContextThreadPoolExecutor /
run_in_executor 均会复制当前 contextvars 上下文（携带同一状态对象引用），
因此并发请求互不可见，杜绝跨请求（跨用户）RAG 上下文与检索步骤串扰。

注意：同一请求内 LangGraph 会并行执行一条 AIMessage 里的多个 tool_calls
（同步路径多线程、异步路径 asyncio.gather），这些工具共享同一个 _RequestState。
故所有配额计数与上下文写入均由 _RequestState 上的 RLock 保护，保证
check-and-set 原子、来源列表合并而非互相覆盖。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from typing import Any, Optional


class _RequestState:
    """单次对话请求内的工具共享状态（每请求一个实例，随上下文隔离）。"""

    __slots__ = (
        "_lock",
        "last_rag_context",
        "knowledge_calls",
        "memory_calls",
        "memory_write_calls",
        "memory_forget_calls",
        "history_calls",
        "image_kb_calls",
        "web_search_calls",
        "web_image_search_calls",
        "fetch_url_calls",
        "rag_step_queue",
        "rag_step_loop",
        "pending_mcp_confirmations",
        "approved_mcp_pending_id",
        "allow_knowledge_retrieval",
        "allow_web_search",
    )

    def __init__(self) -> None:
        # 同一轮内并行工具在多线程中共享本实例，用可重入锁保护所有可变状态。
        # 临界区均为纯内存操作、无 await/阻塞，不会造成死锁或事件循环卡顿。
        self._lock = threading.RLock()
        self.last_rag_context: dict | None = None
        self.knowledge_calls = 0
        self.memory_calls = 0
        self.memory_write_calls = 0
        self.memory_forget_calls = 0
        self.history_calls = 0
        self.image_kb_calls = 0
        self.web_search_calls = 0
        self.web_image_search_calls = 0
        self.fetch_url_calls = 0
        self.rag_step_queue: Any = None
        self.rag_step_loop: asyncio.AbstractEventLoop | None = None
        self.pending_mcp_confirmations: list[dict] = []
        self.approved_mcp_pending_id: str | None = None
        self.allow_knowledge_retrieval = True
        self.allow_web_search = False


_REQUEST_STATE: contextvars.ContextVar[_RequestState | None] = contextvars.ContextVar(
    "kura_agent_tool_request_state", default=None
)


def _state() -> _RequestState:
    state = _REQUEST_STATE.get()
    if state is None:
        state = _RequestState()
        _REQUEST_STATE.set(state)
    return state


_SOURCE_LIST_KEYS = ("kb_sources", "web_sources", "image_references")
_KB_TRACE_TOOL_NAMES = frozenset({"search_knowledge_base", "search_knowledge_by_image"})


def get_state_lock() -> "threading.RLock":
    """返回当前请求状态的锁，供工具模块对 last_rag_context 做原子读-改-写。"""
    return _state()._lock


def _source_identity(item: Any) -> tuple[str, str]:
    """来源去重标识：优先图片 URL / 页面 URL，其次标题，最后整体序列化。"""
    if isinstance(item, dict):
        for key in ("image_url", "url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return key, value.strip()
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            return "title", title.strip()
    try:
        return "raw", json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return "raw", repr(item)


def _merge_source_list(existing: Any, incoming: Any) -> list:
    """合并来源列表：保留已有顺序，追加未出现过的新来源（按标识去重）。"""
    kept: list = list(existing) if isinstance(existing, list) else []
    seen = {_source_identity(item) for item in kept}
    for item in incoming if isinstance(incoming, list) else []:
        sig = _source_identity(item)
        if sig in seen:
            continue
        seen.add(sig)
        kept.append(item)
    return kept


def _is_kb_context(context: dict, trace: Any) -> bool:
    """知识库检索上下文（正文或图片检索）判定：其 trace 优先级高于记忆/联网。"""
    if isinstance(trace, dict) and trace.get("tool_name") in _KB_TRACE_TOOL_NAMES:
        return True
    return any(key in context for key in ("kb_sources", "image_references"))


def _merge_rag_context(existing: dict | None, incoming: dict) -> dict:
    """合并本轮工具产出的 RAG 上下文。

    - 来源列表（kb_sources / web_sources / image_references）累加去重，绝不互相覆盖；
    - rag_trace 单通道：知识库检索优先覆盖；无知识库结果时保留已有，
      仅在当前为空时写入非知识库 trace（如会话记忆）。
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key in _SOURCE_LIST_KEYS:
        if key in incoming:
            merged[key] = _merge_source_list(merged.get(key), incoming.get(key))
    incoming_trace = incoming.get("rag_trace")
    if incoming_trace:
        if _is_kb_context(incoming, incoming_trace) or not merged.get("rag_trace"):
            merged["rag_trace"] = incoming_trace
    for key, value in incoming.items():
        if key in _SOURCE_LIST_KEYS or key == "rag_trace":
            continue
        merged[key] = value
    return merged


def _set_last_rag_context(context: dict) -> None:
    state = _state()
    with state._lock:
        state.last_rag_context = _merge_rag_context(state.last_rag_context, context)


def get_last_rag_context(clear: bool = True) -> Optional[dict]:
    state = _state()
    with state._lock:
        context = state.last_rag_context
        if clear:
            state.last_rag_context = None
    return context


def add_pending_mcp_confirmation(pending: dict) -> dict:
    from app.settings import settings

    state = _state()
    key = (pending.get("tool_name"), pending.get("args_hash"))
    with state._lock:
        items = state.pending_mcp_confirmations
        for item in items:
            if (item.get("tool_name"), item.get("args_hash")) == key:
                return {"status": "duplicate", "pending": item}
        max_n = max(1, int(getattr(settings, "MCP_" + "CONFIRMATION_MAX_PER_TURN", 3)))
        if len(items) >= max_n:
            return {"status": "capped"}
        items.append(dict(pending))
        return {"status": "added", "pending": pending}


def get_pending_mcp_confirmations(clear: bool = True) -> list[dict]:
    state = _state()
    with state._lock:
        items = list(state.pending_mcp_confirmations)
        if clear:
            state.pending_mcp_confirmations = []
    return items


def set_approved_mcp_pending_id(pending_id: str | None) -> None:
    state = _state()
    with state._lock:
        state.approved_mcp_pending_id = (pending_id or "").strip() or None


def get_approved_mcp_pending_id(clear: bool = True) -> str | None:
    state = _state()
    with state._lock:
        pending_id = state.approved_mcp_pending_id
        if clear:
            state.approved_mcp_pending_id = None
    return pending_id


def reset_tool_call_guards() -> None:
    state = _state()
    with state._lock:
        state.knowledge_calls = 0
        state.memory_calls = 0
        state.memory_write_calls = 0
        state.memory_forget_calls = 0
        state.history_calls = 0
        state.image_kb_calls = 0
        state.web_search_calls = 0
        state.web_image_search_calls = 0
        state.fetch_url_calls = 0


def set_turn_tool_policy(*, use_knowledge_retrieval: bool, use_web_search: bool) -> None:
    """本轮检索开关（工具始终挂载，禁用时由工具函数返回 TOOL_DISABLED_THIS_TURN）。"""
    state = _state()
    with state._lock:
        state.allow_knowledge_retrieval = bool(use_knowledge_retrieval)
        state.allow_web_search = bool(use_web_search)


def is_knowledge_allowed_this_turn() -> bool:
    return bool(_state().allow_knowledge_retrieval)


def is_web_search_allowed_this_turn() -> bool:
    return bool(_state().allow_web_search)


def knowledge_disabled_this_turn_msg(tool_name: str) -> str:
    return (
        f"TOOL_DISABLED_THIS_TURN: {tool_name} is not enabled for this turn. "
        "Do not call knowledge-base retrieval tools; answer without them."
    )


def web_search_disabled_this_turn_msg() -> str:
    return (
        "TOOL_DISABLED_THIS_TURN: web_search is not enabled for this turn. "
        "Do not call web_search or fetch_url; answer without live web results."
    )


def fetch_url_disabled_this_turn_msg() -> str:
    return (
        "TOOL_DISABLED_THIS_TURN: fetch_url is not enabled for this turn. "
        "Do not call fetch_url or web_search; answer without live web results."
    )


def web_image_search_disabled_this_turn_msg() -> str:
    return (
        "TOOL_DISABLED_THIS_TURN: web_image_search is not enabled for this turn. "
        "Do not call web_image_search; answer without live web images."
    )


def try_acquire_knowledge_tool_slot() -> bool:
    """同一轮对话仅允许一次知识库检索；成功占用返回 True。"""
    state = _state()
    with state._lock:
        if state.knowledge_calls >= 1:
            return False
        state.knowledge_calls += 1
        return True


def try_acquire_user_memory_tool_slot() -> bool:
    """同一轮对话仅允许一次用户长期记忆读取（read_user_memory）；成功占用返回 True。"""
    state = _state()
    with state._lock:
        if state.memory_calls >= 1:
            return False
        state.memory_calls += 1
        return True


def try_acquire_user_memory_write_slot(limit: int = 3) -> bool:
    """同一轮对话允许有限次长期记忆写入（save_user_memory）；成功占用返回 True。"""
    state = _state()
    n = max(1, int(limit or 1))
    with state._lock:
        if state.memory_write_calls >= n:
            return False
        state.memory_write_calls += 1
        return True


def try_acquire_user_memory_forget_slot() -> bool:
    """同一轮对话仅允许一次长期记忆删除（forget_user_memory）；成功占用返回 True。"""
    state = _state()
    with state._lock:
        if state.memory_forget_calls >= 1:
            return False
        state.memory_forget_calls += 1
        return True


def try_acquire_history_tool_slot(limit: int = 2) -> bool:
    """同一轮对话允许有限次原文翻牌（read_session_history）；成功占用返回 True。

    与用户长期记忆读取各自独立计槽：长期记忆管跨会话偏好，原文翻牌管本会话精确取证。
    """
    state = _state()
    n = max(1, int(limit or 1))
    with state._lock:
        if state.history_calls >= n:
            return False
        state.history_calls += 1
        return True


def try_acquire_image_kb_tool_slot(user_id: int, agent_id: int, session_id: str) -> bool:
    """
    以图知识库检索：每轮成功次数不超过当前会话中图片类附件数量。
    无图片附件时返回 False。
    """
    from app.chat.attachment_service import count_session_image_attachments

    state = _state()
    max_n = count_session_image_attachments(user_id, agent_id, session_id)
    if max_n <= 0:
        return False
    with state._lock:
        if state.image_kb_calls >= max_n:
            return False
        state.image_kb_calls += 1
        return True


def try_acquire_web_search_tool_slot() -> bool:
    """同一轮对话限制联网搜索次数（防 ReAct 循环刷限流）；成功占用返回 True。"""
    from app.settings import settings

    state = _state()
    max_n = max(1, int(getattr(settings, "WEB_SEARCH_MAX_CALLS_PER_TURN", 2)))
    with state._lock:
        if state.web_search_calls >= max_n:
            return False
        state.web_search_calls += 1
        return True


def try_acquire_fetch_url_tool_slot() -> bool:
    """同一轮对话限制 fetch_url 次数；成功占用返回 True。"""
    from app.settings import settings

    state = _state()
    max_n = max(1, int(getattr(settings, "WEB_SEARCH_FETCH_MAX_CALLS_PER_TURN", 3)))
    with state._lock:
        if state.fetch_url_calls >= max_n:
            return False
        state.fetch_url_calls += 1
        return True


def try_acquire_web_image_search_tool_slot() -> bool:
    """同一轮对话限制文字搜图次数；成功占用返回 True。"""
    from app.settings import settings

    state = _state()
    max_n = max(1, int(getattr(settings, "WEB_IMAGE_SEARCH_MAX_CALLS_PER_TURN", 2)))
    with state._lock:
        if state.web_image_search_calls >= max_n:
            return False
        state.web_image_search_calls += 1
        return True


def set_rag_step_queue(queue: Any, *, sync: bool = False) -> None:
    state = _state()
    state.rag_step_queue = queue
    if queue is None:
        state.rag_step_loop = None
    elif sync:
        state.rag_step_loop = None
    else:
        try:
            state.rag_step_loop = asyncio.get_running_loop()
        except RuntimeError:
            state.rag_step_loop = asyncio.get_event_loop()


def log_kb_tool_return_to_terminal(text: str, *, tool_label: str = "search_knowledge_base") -> None:
    """在终端打印知识库/联网工具返回给模型的字符串（受 DEBUG_AGENT_KB_PROMPT 控制）。"""
    from app.settings import settings

    if not getattr(settings, "DEBUG_AGENT_KB_PROMPT", False):
        return
    sep = "=" * 72
    print(
        f"\n{sep}\n[智能体工具] {tool_label} 工具输出（将注入对话上下文）:\n{sep}\n{text}\n{sep}\n",
        flush=True,
    )


def emit_rag_step(icon: str, label: str, detail: str = "") -> None:
    state = _state()
    queue = state.rag_step_queue
    if queue is None:
        return
    step = {"icon": icon, "label": label, "detail": detail}
    loop = state.rag_step_loop
    if loop is not None:
        try:
            if not loop.is_closed():
                loop.call_soon_threadsafe(queue.put_nowait, step)
        except Exception:
            pass
    else:
        try:
            queue.put_nowait(step)
        except Exception:
            pass
