"""search_session_memory：仅当前会话的向量记忆检索。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
    try_acquire_memory_tool_slot,
)


def make_search_session_memory_tool(
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
) -> StructuredTool:
    """
    创建会话记忆检索工具
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param llm_config: LLM配置
    :return: 会话记忆检索工具
    """
    scope_key = f"{user_id}:{agent_id}:{session_id}"  # 会话记忆检索的隔离键, 只能检索当前会话的记忆
    
    # 会话记忆检索工具的函数
    def _search_session_memory(query: str) -> str:
        from app.chat.memory_search import search_session_memory

        # 同一轮对话仅允许一次会话记忆检索；成功占用返回 True。
        if not try_acquire_memory_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: search_session_memory has already been called once in this turn. "
                "Use the existing retrieval result and answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="search_session_memory")
            return limit_msg

        emit_rag_step("🔎", "会话记忆检索", (query or "")[:120])
        try:
            text, trace = search_session_memory(
                query.strip(),
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                llm_config=llm_config,
            )
            _set_last_rag_context({"rag_trace": {**trace, "memory_scope_bound": scope_key}})
        except Exception as e:
            emit_rag_step("⚠️", "会话记忆检索失败", str(e)[:200])
            err = f"会话记忆检索出错：{e}"
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "search_session_memory",
                        "query": query,
                        "error": str(e),
                    }
                }
            )
            log_kb_tool_return_to_terminal(err, tool_label="search_session_memory")
            return err

        log_kb_tool_return_to_terminal(text, tool_label="search_session_memory")
        return text

    # 返回会话记忆检索工具, 用于检索本会话的记忆
    return StructuredTool.from_function(
        name="search_session_memory",
        description=(
            "在本会话中检索「滑动窗口之前」已归档的较早对话（向量混合检索）。"
            "何时应主动调用（不必等用户说「检索历史」）："
            "（1）用户用到指代——如「刚才/之前/上次/前面说过/那个约定/你提到的数」而最近几轮正文里找不到；"
            "（2）追问本会话早期出现过的具体事实——编号、人名、代码片段、公式、结论、用户偏好；"
            "（3）任务依赖多轮之前才出现过的上下文，而当前可见消息不足以回答。"
            "何时不必调用：答案已出现在最近几轮用户与助手消息中；或问题仅需知识库文档/会话附件（请用 search_knowledge_base / read_session_attachment）。"
            "说明：系统可能已自动注入部分较早摘录；若仍不足再使用本工具补充检索。"
            "约束：同一用户提问轮次内最多成功调用一次；得到结果后整合为最终回答，勿重复检索。"
        ),
        func=_search_session_memory,
    )
