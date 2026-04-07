"""Agent 工具（天气与知识库为占位，后续可接真实实现）。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from langchain_core.tools import tool

_LAST_RAG_CONTEXT: dict | None = None
_KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
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
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0


def set_rag_step_queue(queue: Any) -> None:
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    _RAG_STEP_QUEUE = queue
    if queue is not None:
        try:
            _RAG_STEP_LOOP = asyncio.get_running_loop()
        except RuntimeError:
            _RAG_STEP_LOOP = asyncio.get_event_loop()
    else:
        _RAG_STEP_LOOP = None


def emit_rag_step(icon: str, label: str, detail: str = "") -> None:
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    if _RAG_STEP_QUEUE is not None and _RAG_STEP_LOOP is not None:
        step = {"icon": icon, "label": label, "detail": detail}
        try:
            if not _RAG_STEP_LOOP.is_closed():
                _RAG_STEP_LOOP.call_soon_threadsafe(_RAG_STEP_QUEUE.put_nowait, step)
        except Exception:
            pass


@tool("get_current_weather")
def get_current_weather(location: str, extensions: Optional[str] = "base") -> str:
    """获取指定地点的天气信息。"""
    return (
        "天气服务当前为占位模式，未接入高德等外部 API。"
        f"（请求参数：location={location!r}, extensions={extensions!r}）"
    )


@tool("search_knowledge_base")
def search_knowledge_base(query: str) -> str:
    """在知识库中检索与查询相关的文档片段（混合检索）。"""
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN
    if _KNOWLEDGE_TOOL_CALLS_THIS_TURN >= 1:
        return (
            "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
            "Use the existing retrieval result and provide the final answer directly."
        )
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN += 1

    emit_rag_step("📚", "知识库检索", "占位模式：尚未接入向量库与 RAG 流水线")
    _set_last_rag_context(
        {
            "rag_trace": {
                "tool_used": True,
                "tool_name": "search_knowledge_base",
                "query": query,
                "retrieval_mode": "placeholder",
            }
        }
    )
    return (
        "知识库检索功能尚未接入（占位）。请直接根据用户问题用你的通用知识回答，"
        "并说明当前无法访问企业内部文档库。"
    )
