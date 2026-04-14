"""绑定 kb_scope 与智能体 LLM 配置的 search_knowledge_base 工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
)


def make_search_knowledge_tool(kb_scope: str, llm_config: dict[str, Any]) -> StructuredTool:
    def _search_knowledge_base(query: str) -> str:
        from app.chat.tools import try_acquire_knowledge_tool_slot

        if not try_acquire_knowledge_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
                "Use the existing retrieval result and provide the final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg)
            return limit_msg

        try:
            from app.kb.rag_pipeline import run_rag_graph

            rag_result = run_rag_graph(query.strip(), kb_scope, llm_config)
        except Exception as e:
            emit_rag_step("⚠️", "知识库检索失败", str(e)[:200])
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "search_knowledge_base",
                        "query": query,
                        "error": str(e),
                    }
                }
            )
            err_msg = f"知识库检索出错：{e}"
            log_kb_tool_return_to_terminal(err_msg)
            return err_msg

        docs = rag_result.get("docs", []) if isinstance(rag_result, dict) else []
        rag_trace = rag_result.get("rag_trace", {}) if isinstance(rag_result, dict) else {}
        if rag_trace:
            _set_last_rag_context({"rag_trace": rag_trace})

        if not docs:
            empty_msg = "No relevant documents found in the knowledge base."
            log_kb_tool_return_to_terminal(empty_msg)
            return empty_msg

        formatted = []
        for i, result in enumerate(docs, 1):
            source = result.get("filename", "Unknown")
            page = result.get("page_number", "N/A")
            text = result.get("text", "")
            formatted.append(f"[{i}] {source} (Page {page}):\n{text}")

        out = "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)
        log_kb_tool_return_to_terminal(out)
        return out

    return StructuredTool.from_function(
        name="search_knowledge_base",
        description=(
            "在本智能体「知识库」中检索与用户问题相关的文档片段（向量+稀疏混合检索）。"
            "适用于：问题可能依赖已入库的领域文档、产品说明、政策条文等。"
            "不适用于：仅依赖当前对话里临时上传的会话附件列表（那是另一类附件，见 read_session_attachment）。"
            "调用约束：同一用户提问轮次内最多成功检索一次；得到工具返回后应直接整合为最终回答，勿重复检索。"
            "若返回无相关片段，应如实说明。"
        ),
        func=_search_knowledge_base,
    )
