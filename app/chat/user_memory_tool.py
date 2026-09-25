"""read_user_memory：按需读取跨会话用户长期记忆（偏好/约束）。

不做向量检索、不做查询重写、不做预注入：事实条数天然很少，
模型在需要个性化或遵守长期约定时直接读 PG 即可。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
    try_acquire_user_memory_tool_slot,
)


def make_read_user_memory_tool(user_id: int, agent_id: int) -> StructuredTool:
    """创建用户长期记忆读取工具。

    :param user_id: **当前调用者**的 user_id（共享智能体时为共享用户，而非属主）
    """
    scope_key = f"{user_id}:{agent_id}"

    def _read_user_memory(keyword: str = "") -> str:
        # 同一轮对话仅允许一次长期记忆读取；成功占用返回 True。
        if not try_acquire_user_memory_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: read_user_memory has already been called once in this turn. "
                "Use the existing result and answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="read_user_memory")
            return limit_msg

        kw = (keyword or "").strip()
        emit_rag_step("🧠", "读取用户长期记忆", (kw or "全部")[:120])
        try:
            from app.chat.user_memory import format_user_facts, list_user_facts
            from app.settings import settings

            rows = list_user_facts(user_id, agent_id, keyword=kw)
            text = format_user_facts(
                rows,
                max_tokens=int(getattr(settings, "CHAT_MEMORY_TOOL_MAX_TOKENS", 3000) or 3000),
            )
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "read_user_memory",
                        "query": kw,
                        "hit_count": len(rows),
                        "memory_scope_bound": scope_key,
                    }
                }
            )
        except Exception as e:
            emit_rag_step("⚠️", "读取用户长期记忆失败", str(e)[:200])
            err = f"读取用户长期记忆出错：{e}"
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "read_user_memory",
                        "query": kw,
                        "error": str(e),
                    }
                }
            )
            log_kb_tool_return_to_terminal(err, tool_label="read_user_memory")
            return err

        log_kb_tool_return_to_terminal(text, tool_label="read_user_memory")
        return text

    return StructuredTool.from_function(
        name="read_user_memory",
        description=(
            "读取当前用户的**跨会话长期记忆**（稳定的偏好与硬约束，不是本会话历史）。"
            "何时应主动调用：（1）用户要求个性化，或问「你记得我的偏好吗/之前说过的习惯」；"
            "（2）任务可能受用户既有偏好/约束影响（语言、格式、技术栈禁忌、称呼等）；"
            "（3）执行较长任务前，确认是否存在需要遵守的长期约定。"
            "何时不必调用：答案只依赖当前会话内容或知识库文档。"
            "本会话较早的对话不在本工具范围：需要本会话历史或逐字原文请用 read_session_history。"
            "约束：同一用户提问轮次内最多成功调用一次；拿到结果后直接使用，不要重复调用。"
            "可选传 keyword 做包含匹配过滤（如「语言」「格式」），留空返回全部。"
        ),
        func=_read_user_memory,
    )
