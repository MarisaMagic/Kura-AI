"""以图为查询的轻量 RAG 子图：解析附件 → 密集检索。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.chat.attachment_service import attachment_object_key, classify_kind, get_attachment_row
from app.chat.tools import emit_rag_step
from app.core import object_storage as obs


class ImageRAGState(TypedDict, total=False):
    attachment_id: str
    user_id: int
    agent_id: int
    session_id: str
    kb_scope: str
    focus: str
    top_k: int
    resolved_image_path: Optional[str]
    error: Optional[str]
    docs: List[dict[str, Any]]
    meta: dict[str, Any]
    rag_trace: Optional[dict[str, Any]]


def _cleanup_temp_image(path: Optional[str]) -> None:
    """删除 resolve 节点下载的临时图片文件（对象存储改造后 resolved_image_path 必为临时文件）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _node_resolve_attachment(state: ImageRAGState) -> ImageRAGState:
    aid = (state.get("attachment_id") or "").strip()
    user_id = state["user_id"]
    agent_id = state["agent_id"]
    session_id = state["session_id"]
    row = get_attachment_row(aid, user_id=user_id, agent_id=agent_id, session_id=session_id)
    if not row:
        return {**state, "resolved_image_path": None, "error": "附件不存在或不属于当前会话。"}
    kind = (getattr(row, "kind", None) or "") or classify_kind(row.original_filename or "")
    if kind != "image":
        return {**state, "resolved_image_path": None, "error": "该附件不是图片，无法用于以图检索。"}
    # 附件本体在对象存储：下载为本地临时文件（DashScope embedding 走 file://），末节点负责清理
    try:
        raw = obs.read_bytes(attachment_object_key(row.stored_relpath))
    except obs.ObjectNotFoundError:
        return {**state, "resolved_image_path": None, "error": f"图片文件已丢失或不可读: {row.stored_relpath}"}
    suffix = Path(row.original_filename or "").suffix or ".png"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    emit_rag_step("📷", "以图知识库检索", f"已解析 attachment: {aid[:8]}…")
    return {**state, "resolved_image_path": tmp_path, "error": None}


def _node_retrieve(state: ImageRAGState) -> ImageRAGState:
    if state.get("error") or not state.get("resolved_image_path"):
        return {
            **state,
            "docs": [],
            "meta": {
                "retrieval_mode": "skipped",
                "candidate_count": 0,
            },
        }
    from app.kb.rag_utils import retrieve_documents_by_image

    path = state["resolved_image_path"]
    kb_scope = state["kb_scope"]
    top_k = int(state.get("top_k") or 5)
    focus = (state.get("focus") or "mixed").lower()
    if focus not in ("text", "image", "mixed"):
        focus = "mixed"

    emit_rag_step("🔍", "以图向量化 + Milvus 密集检索", f"focus={focus}, top_k={top_k}")
    out = retrieve_documents_by_image(
        path,
        kb_scope,
        top_k=top_k,
        focus=focus,  # type: ignore[arg-type]
    )
    return {**state, "docs": out.get("docs", []), "meta": out.get("meta", {})}


def _node_assemble_rag_trace(state: ImageRAGState) -> ImageRAGState:
    try:
        docs = state.get("docs") or []
        meta = state.get("meta") or {}
        err = state.get("error")
        if err:
            trace = {
                "tool_used": True,
                "tool_name": "search_knowledge_by_image",
                "kb_scope": state.get("kb_scope"),
                "attachment_id": state.get("attachment_id"),
                "focus": state.get("focus"),
                "error": err,
                "retrieved_chunks": [],
                "retrieval_stage": "image_rag",
            }
            return {**state, "rag_trace": trace}
        trace = {
            "tool_used": True,
            "tool_name": "search_knowledge_by_image",
            "kb_scope": state.get("kb_scope"),
            "attachment_id": state.get("attachment_id"),
            "focus": state.get("focus"),
            "query": f"(image:{state.get('attachment_id')})",
            "expanded_query": f"(image:{state.get('attachment_id')})",
            "retrieved_chunks": docs,
            "initial_retrieved_chunks": docs,
            "retrieval_stage": "image_rag",
            "retrieval_mode": meta.get("retrieval_mode"),
            "rerank_applied": meta.get("rerank_applied"),
            "rerank_error": meta.get("rerank_error"),
            "candidate_k": meta.get("candidate_k"),
        }
        emit_rag_step("✅", "以图检索完成", f"命中 {len(docs)} 个片段")
        return {**state, "rag_trace": trace}
    finally:
        _cleanup_temp_image(state.get("resolved_image_path"))


def build_image_rag_graph():
    graph = StateGraph(ImageRAGState)
    graph.add_node("resolve_attachment", _node_resolve_attachment)
    graph.add_node("retrieve_by_image", _node_retrieve)
    graph.add_node("assemble_rag_trace", _node_assemble_rag_trace)
    graph.set_entry_point("resolve_attachment")
    graph.add_edge("resolve_attachment", "retrieve_by_image")
    graph.add_edge("retrieve_by_image", "assemble_rag_trace")
    graph.add_edge("assemble_rag_trace", END)
    return graph.compile()


image_rag_graph = build_image_rag_graph()


def run_image_rag_graph(
    attachment_id: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    kb_scope: str,
    *,
    focus: str = "mixed",
    top_k: int = 5,
) -> dict[str, Any]:
    out = image_rag_graph.invoke(
        {
            "attachment_id": attachment_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "kb_scope": kb_scope,
            "focus": focus,
            "top_k": top_k,
        }
    )
    if not isinstance(out, dict):
        return {"docs": [], "rag_trace": None, "error": "image_rag_empty"}
    return {
        "docs": out.get("docs", []),
        "rag_trace": out.get("rag_trace"),
        "error": out.get("error"),
        "meta": out.get("meta", {}),
    }
