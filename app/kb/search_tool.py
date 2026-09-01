"""绑定 kb_scope 与智能体 LLM 配置的 search_knowledge_base 工具（多模态：文本 + 图片元数据来自 PostgreSQL）。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
)
from app.kb.kb_tool_formatting import format_knowledge_retrieval_tool_output


def make_search_knowledge_tool(
    kb_scope: str,
    llm_config: dict[str, Any],
    *,
    knowledge_base_document_filter: list[str] | None = None,
) -> StructuredTool:
    """
    knowledge_base_document_filter 由对话入口「前置选档」注入：
    - None：全 knowledge scope 检索（不加 filename 子句）；
    - 非空 list：仅在这些 file_key 上检索。
    智能体工具侧不再提供 document_filenames 参数，避免与前置范围冲突。
    """

    class _SearchKbArgs(BaseModel):
        query: str = Field(description="用于检索的自然语言问题或关键词，应与用户意图一致。")

    def _search_knowledge_base(query: str) -> str:
        from app.chat.tools import (
            is_knowledge_allowed_this_turn,
            knowledge_disabled_this_turn_msg,
            try_acquire_knowledge_tool_slot,
        )

        if not is_knowledge_allowed_this_turn():
            limit_msg = knowledge_disabled_this_turn_msg("search_knowledge_base")
            log_kb_tool_return_to_terminal(limit_msg, tool_label="search_knowledge_base")
            return limit_msg

        if not try_acquire_knowledge_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
                "Use existing retrieval result and provide final final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="search_knowledge_base")
            return limit_msg

        if knowledge_base_document_filter:
            emit_rag_step("📄", "文档范围过滤", f"file_key 数量: {len(knowledge_base_document_filter)}")

        try:
            from app.kb.rag_pipeline import run_rag_graph

            rag_result = run_rag_graph(
                question=query.strip(),
                kb_scope=kb_scope,
                llm_config=llm_config,
                document_filenames=knowledge_base_document_filter,
            )
        except Exception as e:
            emit_rag_step("⚠️", "知识库检索失败", str(e)[:200])
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "search_knowledge_base",
                        "query": query,
                        "document_filenames": knowledge_base_document_filter,
                        "error": str(e),
                    }
                }
            )
            err_msg = f"知识库检索出错：{e}"
            log_kb_tool_return_to_terminal(err_msg, tool_label="search_knowledge_base")
            return err_msg

        docs = rag_result.get("docs", []) if isinstance(rag_result, dict) else []
        rag_trace = rag_result.get("rag_trace", {}) if isinstance(rag_result, dict) else {}
        no_answer = bool(rag_result.get("no_answer")) if isinstance(rag_result, dict) else False

        if no_answer:
            # 二次门控判定知识库无相关资料：返回拒答指令，禁止模型在低质量上下文上硬编
            refuse_msg = (
                "KNOWLEDGE_BASE_NO_RELEVANT_INFO: 本知识库经两次检索与逐块相关性评估，未找到与用户问题相关的资料。"
                "请如实告知用户：知识库中没有相关资料，可建议用户上传补充相关资料后重试；"
                "不要引用任何检索片段，也不要编造任何知识库结论。"
            )
            emit_rag_step("🚫", "向用户说明知识库无相关资料")
            log_kb_tool_return_to_terminal(refuse_msg, tool_label="search_knowledge_base")
            _set_last_rag_context({"rag_trace": rag_trace, "image_references": [], "kb_sources": []})
            return refuse_msg

        if not docs:
            empty_msg = "No relevant documents found in knowledge base."
            log_kb_tool_return_to_terminal(empty_msg, tool_label="search_knowledge_base")
            _set_last_rag_context({"rag_trace": rag_trace, "image_references": [], "kb_sources": []})
            return empty_msg

        out, image_references, kb_sources = format_knowledge_retrieval_tool_output(docs)
        log_kb_tool_return_to_terminal(out, tool_label="search_knowledge_base")

        _set_last_rag_context(
            {
                "rag_trace": rag_trace,
                "image_references": image_references,
                "kb_sources": kb_sources,
            }
        )

        return out

    return StructuredTool.from_function(
        name="search_knowledge_base",
        description=(
            "在本智能体「知识库」中检索与用户问题相关的文档片段（多模态：文本 + 图片）。"
            "本回合可检索的文档范围已由系统预先确定，你只需传 query，不要尝试指定文件名或换库。"
            "每个图片块都会给出一行现成的 Markdown，形如 `![说明](/api/v1/media/...?exp=...&sig=...)`。"
            "回答用户时若需配图，必须把该行原样复制到回答中；不得自行改写括号内内容，"
            "不得填入文档名/页码/(Page n) 标题/stored_relpath，不得改成 http(s) 绝对地址或 image://、file://。"
            "回答中凡引用检索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致），让用户可追溯出处。"
            "调用约束：同一用户提问轮次内最多成功检索一次；得到工具返回后应直接整合为最终回答，勿重复检索。"
        ),
        args_schema=_SearchKbArgs,
        func=_search_knowledge_base,
    )
