"""Agent 工具共享状态（知识库/记忆/联网检索由各自工具模块动态绑定）。

并发安全：全部状态挂在按请求创建的 _RequestState 上，经 ContextVar 分发。
asyncio.create_task / asyncio.to_thread / LangChain run_in_executor 均会复制当前
contextvars 上下文（携带同一状态对象引用），同步路径整体运行于单线程内，
因此并发请求互不可见，杜绝跨请求（跨用户）RAG 上下文与检索步骤串扰。
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, Optional


class _RequestState:
    """单次对话请求内的工具共享状态（每请求一个实例，随上下文隔离）。"""

    __slots__ = (
        "last_rag_context",
        "knowledge_calls",
        "memory_calls",
        "image_kb_calls",
        "web_search_calls",
        "rag_step_queue",
        "rag_step_loop",
        "pending_mcp_confirmations",
        "approved_mcp_pending_id",
    )

    def __init__(self) -> None:
        self.last_rag_context: dict | None = None
        self.knowledge_calls = 0
        self.memory_calls = 0
        self.image_kb_calls = 0
        self.web_search_calls = 0
        self.rag_step_queue: Any = None
        self.rag_step_loop: asyncio.AbstractEventLoop | None = None
        self.pending_mcp_confirmations: list[dict] = []
        self.approved_mcp_pending_id: str | None = None


_REQUEST_STATE: contextvars.ContextVar[_RequestState | None] = contextvars.ContextVar(
    "kura_agent_tool_request_state", default=None
)


def _state() -> _RequestState:
    state = _REQUEST_STATE.get()
    if state is None:
        state = _RequestState()
        _REQUEST_STATE.set(state)
    return state


def _set_last_rag_context(context: dict) -> None:
    _state().last_rag_context = context


def get_last_rag_context(clear: bool = True) -> Optional[dict]:
    state = _state()
    context = state.last_rag_context
    if clear:
        state.last_rag_context = None
    return context


def add_pending_mcp_confirmation(pending: dict) -> dict:
    from app.settings import settings

    state = _state()
    items = state.pending_mcp_confirmations
    key = (pending.get("tool_name"), pending.get("args_hash"))
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
    items = list(state.pending_mcp_confirmations)
    if clear:
        state.pending_mcp_confirmations = []
    return items


def set_approved_mcp_pending_id(pending_id: str | None) -> None:
    _state().approved_mcp_pending_id = (pending_id or "").strip() or None


def get_approved_mcp_pending_id(clear: bool = True) -> str | None:
    state = _state()
    pending_id = state.approved_mcp_pending_id
    if clear:
        state.approved_mcp_pending_id = None
    return pending_id


def reset_tool_call_guards() -> None:
    state = _state()
    state.knowledge_calls = 0
    state.memory_calls = 0
    state.image_kb_calls = 0
    state.web_search_calls = 0


def try_acquire_knowledge_tool_slot() -> bool:
    """同一轮对话仅允许一次知识库检索；成功占用返回 True。"""
    state = _state()
    if state.knowledge_calls >= 1:
        return False
    state.knowledge_calls += 1
    return True


def try_acquire_memory_tool_slot() -> bool:
    """同一轮对话仅允许一次会话记忆检索；成功占用返回 True。"""
    state = _state()
    if state.memory_calls >= 1:
        return False
    state.memory_calls += 1
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
    if state.image_kb_calls >= max_n:
        return False
    state.image_kb_calls += 1
    return True


def try_acquire_web_search_tool_slot() -> bool:
    """同一轮对话限制联网搜索次数（防 ReAct 循环刷限流）；成功占用返回 True。"""
    from app.settings import settings

    state = _state()
    max_n = max(1, int(getattr(settings, "WEB_SEARCH_MAX_CALLS_PER_TURN", 2)))
    if state.web_search_calls >= max_n:
        return False
    state.web_search_calls += 1
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
