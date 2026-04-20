"""Agent 工具（天气占位；知识库检索由 app.kb.search_tool 动态绑定）。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from langchain_core.tools import tool

_LAST_RAG_CONTEXT: dict | None = None
_KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
_MEMORY_TOOL_CALLS_THIS_TURN = 0
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
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN, _MEMORY_TOOL_CALLS_THIS_TURN
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
    _MEMORY_TOOL_CALLS_THIS_TURN = 0


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


def log_kb_tool_return_to_terminal(text: str) -> None:
    """在终端打印 search_knowledge_base 返回给模型的字符串（受 DEBUG_AGENT_KB_PROMPT 控制）。"""
    from app.settings import settings

    if not getattr(settings, "DEBUG_AGENT_KB_PROMPT", True):
        return
    sep = "=" * 72
    print(
        f"\n{sep}\n[智能体知识库] search_knowledge_base 工具输出（将注入对话上下文）:\n{sep}\n{text}\n{sep}\n",
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


@tool("get_current_weather")
def get_current_weather(location: str, extensions: Optional[str] = "base") -> str:
    """获取指定地点的天气信息。"""
    return (
        "天气服务当前为占位模式，未接入高德等外部 API。"
        f"（请求参数：location={location!r}, extensions={extensions!r}）"
    )


