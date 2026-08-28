"""Agent 工具共享状态（知识库/记忆/联网检索由各自工具模块动态绑定）。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

_LAST_RAG_CONTEXT: dict | None = None
_KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
_MEMORY_TOOL_CALLS_THIS_TURN = 0
_IMAGE_KB_TOOL_CALLS_THIS_TURN = 0
_WEB_SEARCH_TOOL_CALLS_THIS_TURN = 0
_RAG_STEP_QUEUE: Any = None
_RAG_STEP_LOOP: asyncio.AbstractEventLoop | None = None


def _set_last_rag_context(context: dict) -> None:
    global _LAST_RAG_CONTEXT
    _LAST_RAG_CONTEXT = context


def get_last_rag_context(clear: bool = True) -> Optional[dict]:
    global _LAST_RAG_CONTEXT
    context = _LAST_RAG_CONTEXT
    if clear:
        _LAST_RAG_CONTEXT = None
    return context


def reset_tool_call_guards() -> None:
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN, _MEMORY_TOOL_CALLS_THIS_TURN, _IMAGE_KB_TOOL_CALLS_THIS_TURN
    global _WEB_SEARCH_TOOL_CALLS_THIS_TURN
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
    _MEMORY_TOOL_CALLS_THIS_TURN = 0
    _IMAGE_KB_TOOL_CALLS_THIS_TURN = 0
    _WEB_SEARCH_TOOL_CALLS_THIS_TURN = 0


def try_acquire_knowledge_tool_slot() -> bool:
    """同一轮对话仅允许一次知识库检索；成功占用返回 True。"""
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN
    if _KNOWLEDGE_TOOL_CALLS_THIS_TURN >= 1:
        return False
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN += 1
    return True


def try_acquire_memory_tool_slot() -> bool:
    """同一轮对话仅允许一次会话记忆检索；成功占用返回 True。"""
    global _MEMORY_TOOL_CALLS_THIS_TURN
    if _MEMORY_TOOL_CALLS_THIS_TURN >= 1:
        return False
    _MEMORY_TOOL_CALLS_THIS_TURN += 1
    return True


def try_acquire_image_kb_tool_slot(user_id: int, agent_id: int, session_id: str) -> bool:
    """
    以图知识库检索：每轮成功次数不超过当前会话中图片类附件数量。
    无图片附件时返回 False。
    """
    from app.chat.attachment_service import count_session_image_attachments

    global _IMAGE_KB_TOOL_CALLS_THIS_TURN
    max_n = count_session_image_attachments(user_id, agent_id, session_id)
    if max_n <= 0:
        return False
    if _IMAGE_KB_TOOL_CALLS_THIS_TURN >= max_n:
        return False
    _IMAGE_KB_TOOL_CALLS_THIS_TURN += 1
    return True


def try_acquire_web_search_tool_slot() -> bool:
    """同一轮对话限制联网搜索次数（防 ReAct 循环刷限流）；成功占用返回 True。"""
    from app.settings import settings

    global _WEB_SEARCH_TOOL_CALLS_THIS_TURN
    max_n = max(1, int(getattr(settings, "WEB_SEARCH_MAX_CALLS_PER_TURN", 2)))
    if _WEB_SEARCH_TOOL_CALLS_THIS_TURN >= max_n:
        return False
    _WEB_SEARCH_TOOL_CALLS_THIS_TURN += 1
    return True


def set_rag_step_queue(queue: Any, *, sync: bool = False) -> None:
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    _RAG_STEP_QUEUE = queue
    if queue is None:
        _RAG_STEP_LOOP = None
    elif sync:
        _RAG_STEP_LOOP = None
    else:
        try:
            _RAG_STEP_LOOP = asyncio.get_running_loop()
        except RuntimeError:
            _RAG_STEP_LOOP = asyncio.get_event_loop()


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
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    if _RAG_STEP_QUEUE is None:
        return
    step = {"icon": icon, "label": label, "detail": detail}
    if _RAG_STEP_LOOP is not None:
        try:
            if not _RAG_STEP_LOOP.is_closed():
                _RAG_STEP_LOOP.call_soon_threadsafe(_RAG_STEP_QUEUE.put_nowait, step)
        except Exception:
            pass
    else:
        try:
            _RAG_STEP_QUEUE.put_nowait(step)
        except Exception:
            pass
