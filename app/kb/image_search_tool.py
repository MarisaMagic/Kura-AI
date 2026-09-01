"""以图检索知识库：search_knowledge_by_image。"""

from __future__ import annotations

import os
from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.chat.attachment_service import _abs_path, classify_kind, get_attachment_row
from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    is_knowledge_allowed_this_turn,
    knowledge_disabled_this_turn_msg,
    log_kb_tool_return_to_terminal,
    try_acquire_image_kb_tool_slot,
)
from app.kb.kb_tool_formatting import format_knowledge_retrieval_tool_output
from app.kb.rag_image_pipeline import run_image_rag_graph

_MAX_KB_IMAGE_TOPK = 20


def make_search_knowledge_by_image_tool(
    kb_scope: str,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> StructuredTool:
    class _ImageKbArgs(BaseModel):
        attachment_id: str = Field(description="本会话已上传图片附件的 ID（见会话附件列表中的 attachment_id）。")
        focus: Literal["text", "image", "mixed"] = Field(
            "mixed",
            description="text=仅返回文本块；image=仅返回知识库内图片块；mixed=文本+图片。",
        )
        top_k: int = Field(5, ge=1, le=_MAX_KB_IMAGE_TOPK, description="返回片段条数上限。")

    def _search_knowledge_by_image(attachment_id: str, focus: str = "mixed", top_k: int = 5) -> str:
        if not is_knowledge_allowed_this_turn():
            limit_msg = knowledge_disabled_this_turn_msg("search_knowledge_by_image")
            log_kb_tool_return_to_terminal(limit_msg, tool_label="search_knowledge_by_image")
            return limit_msg

        aid = (attachment_id or "").strip()
        row = get_attachment_row(aid, user_id=user_id, agent_id=agent_id, session_id=session_id)
        if not row:
            return "错误：附件不存在或不属于当前会话。"
        kind = (getattr(row, "kind", None) or "") or classify_kind(row.original_filename or "")
        if kind != "image":
            return "错误：该附件不是图片，无法以图检索知识库。"
        if not os.path.isfile(_abs_path(row.stored_relpath)):
            return "错误：图片文件在服务器上不可读，请重新上传。"

        if not try_acquire_image_kb_tool_slot(user_id, agent_id, session_id):
            from app.chat.attachment_service import count_session_image_attachments

            n = count_session_image_attachments(user_id, agent_id, session_id)
            if n <= 0:
                limit_msg = (
                    "TOOL_CALL_LIMIT: 以图知识库检索需要本会话至少有一张图片附件；"
                    "请先上传图片或检查 attachment_id 是否为图片。"
                )
            else:
                limit_msg = (
                    f"TOOL_CALL_LIMIT: 以图知识库检索在本轮已用尽额度（{n} 次，与会话中图片附件数量相同）。"
                    "请直接基于已返回结果作答。"
                )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="search_knowledge_by_image")
            return limit_msg

        cap = min(int(top_k or 5), _MAX_KB_IMAGE_TOPK)
        foc = (focus or "mixed").lower()
        if foc not in ("text", "image", "mixed"):
            foc = "mixed"

        try:
            rag_result = run_image_rag_graph(
                aid,
                user_id,
                agent_id,
                session_id,
                kb_scope,
                focus=foc,
                top_k=cap,
            )
        except Exception as e:
            emit_rag_step("⚠️", "以图知识库检索失败", str(e)[:200])
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "search_knowledge_by_image",
                        "attachment_id": aid,
                        "error": str(e),
                    },
                    "image_references": [],
                }
            )
            err_msg = f"以图知识库检索出错：{e}"
            log_kb_tool_return_to_terminal(err_msg, tool_label="search_knowledge_by_image")
            return err_msg

        err_early = rag_result.get("error")
        if err_early:
            log_kb_tool_return_to_terminal(err_early, tool_label="search_knowledge_by_image")
            _set_last_rag_context(
                {
                    "rag_trace": rag_result.get("rag_trace")
                    or {
                        "tool_name": "search_knowledge_by_image",
                        "error": err_early,
                    },
                    "image_references": [],
                }
            )
            return err_early

        docs = rag_result.get("docs", [])
        rag_trace = rag_result.get("rag_trace") or {}

        if not docs:
            empty_msg = "No relevant documents found in knowledge base."
            log_kb_tool_return_to_terminal(empty_msg, tool_label="search_knowledge_by_image")
            _set_last_rag_context({"rag_trace": rag_trace, "image_references": [], "kb_sources": []})
            return empty_msg

        out, image_references, kb_sources = format_knowledge_retrieval_tool_output(docs)
        log_kb_tool_return_to_terminal(out, tool_label="search_knowledge_by_image")

        _set_last_rag_context(
            {
                "rag_trace": rag_trace,
                "image_references": image_references,
                "kb_sources": kb_sources,
            }
        )
        return out

    return StructuredTool.from_function(
        name="search_knowledge_by_image",
        description=(
            "用「本会话已上传的某张图片」作为查询，在智能体知识库中做以图搜文/以图搜图（多模态向量相似度）。"
            "图片块 URL 以工具返回的现成 Markdown 行为准（![说明](/api/v1/media/...?exp=...&sig=...)），回答中配图须原样复制该行，勿改写路径或加域名。"
            "需传入 attachment_id；每轮以图成功检索次数不超过当前会话中图片类附件张数；"
            "与 search_knowledge_base（以文检索）的额度相互独立。"
            "回答中凡引用检索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致），让用户可追溯出处。"
        ),
        args_schema=_ImageKbArgs,
        func=_search_knowledge_by_image,
    )
