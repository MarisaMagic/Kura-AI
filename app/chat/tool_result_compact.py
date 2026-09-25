"""轮内 micro-compact：清理旧的、可重新获取的工具结果。

为什么需要：一次提问里模型可能连续调用知识库/网页/附件工具，每个 tool_result 可达上万字符，
全部原样堆到本轮结束。轮间压缩（compact.py）只在轮首生效，管不到轮内增长——这正是
Claude Code micro-compact 解决的问题，且它是**零 API 成本**的一层。

安全边界：
- 只清理「可重新获取」的白名单工具（检索/读页/读附件）；MCP 工具一律不动，
  因为其中可能有写操作，其结果不可重复获取，砍掉就是真的丢了；
- 永不改动原消息对象，只构造新的 ToolMessage，保证 tool_call_id 配对不变；
- 未达阈值时零改动，不干扰短对话。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from app.chat.context_budget import budget_for, estimate_messages_tokens, estimate_tokens, resolve_window
from app.chat.message_codec import msg_content_to_str
from app.chat.tools import emit_rag_step
from app.settings import settings

logger = logging.getLogger(__name__)

CLEARED_PLACEHOLDER = "[旧工具结果已清理 / Old tool result content cleared：如需该内容请重新调用本工具]"

# 可重新获取的工具结果才允许清理（对齐 Claude Code 的 COMPACTABLE_TOOLS 思路）
COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "search_knowledge_base",
        "search_knowledge_by_image",
        "web_search",
        "fetch_url",
        "web_image_search",
        "read_session_attachment",
        "search_session_attachment",
        "list_session_attachments_brief",
        "search_session_memory",
        "read_session_history",
    }
)


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _float_setting(name: str, default: float) -> float:
    try:
        v = float(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def is_compactable_tool(name: str | None) -> bool:
    """工具名是否在可清理白名单内（名字缺失时按不可清理处理，宁可少清也不要误删）。"""
    return str(name or "").strip() in COMPACTABLE_TOOLS


def _tool_call_names(messages: list[BaseMessage]) -> dict[str, str]:
    """由 AIMessage.tool_calls 建 tool_call_id → 工具名映射（ToolMessage.name 缺失时兜底）。"""
    out: dict[str, str] = {}
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or "").strip()
            if cid:
                out[cid] = str(tc.get("name") or "")
    return out


def _resolve_tool_name(msg: ToolMessage, id_to_name: dict[str, str]) -> str:
    name = str(getattr(msg, "name", "") or "").strip()
    if name:
        return name
    return id_to_name.get(str(getattr(msg, "tool_call_id", "") or ""), "")


def _clip_keep_ends(text: str, max_chars: int) -> str:
    """超长工具结果裁剪：保留头尾、省略中段（结论常在尾部）。"""
    s = text or ""
    if len(s) <= max_chars:
        return s
    if max_chars <= 40:
        return s[:max_chars] + "…"
    head = int(max_chars * 0.25)
    tail = max_chars - head - 24
    return s[:head] + "\n…（中段已省略）…\n" + s[-tail:]


def clear_old_tool_results(
    messages: list[BaseMessage],
    *,
    keep_recent: int = 5,
    max_result_tokens: int = 6000,
) -> tuple[list[BaseMessage], int, int]:
    """把白名单工具的旧结果替换为占位符，并裁剪仍然过大的近期结果。

    :return: (新消息列表, 被清理条数, 被裁剪条数)；无改动时原样返回入参列表
    """
    if not messages:
        return messages, 0, 0
    id_to_name = _tool_call_names(messages)
    clearable_idx: list[int] = []
    for i, m in enumerate(messages):
        if not isinstance(m, ToolMessage):
            continue
        if not is_compactable_tool(_resolve_tool_name(m, id_to_name)):
            continue
        clearable_idx.append(i)
    if not clearable_idx:
        return messages, 0, 0

    keep = max(1, int(keep_recent))
    to_clear = set(clearable_idx[:-keep]) if len(clearable_idx) > keep else set()
    max_chars = max(200, int(max_result_tokens) * 4)  # 保守按 4 字符/token 折算

    cleared = clipped = 0
    out: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if not isinstance(m, ToolMessage):
            out.append(m)
            continue
        name = _resolve_tool_name(m, id_to_name)
        if not is_compactable_tool(name):
            out.append(m)
            continue
        if i in to_clear:
            out.append(
                ToolMessage(
                    content=CLEARED_PLACEHOLDER,
                    tool_call_id=getattr(m, "tool_call_id", "") or "",
                    name=name or None,
                    status=getattr(m, "status", "success") or "success",
                )
            )
            cleared += 1
            continue
        body = msg_content_to_str(m.content)
        if estimate_tokens(body) <= max_result_tokens:
            out.append(m)
            continue
        clipped_body = _clip_keep_ends(body, max_chars)
        if clipped_body == body:
            out.append(m)
            continue
        out.append(
            ToolMessage(
                content=clipped_body,
                tool_call_id=getattr(m, "tool_call_id", "") or "",
                name=name or None,
                status=getattr(m, "status", "success") or "success",
            )
        )
        clipped += 1
    if cleared == 0 and clipped == 0:
        return messages, 0, 0
    return out, cleared, clipped


def compact_tool_results_if_needed(
    messages: list[BaseMessage], *, context_window: Any = None
) -> tuple[list[BaseMessage], int, int]:
    """达到轮内阈值才执行清理；未达阈值零改动。"""
    if not getattr(settings, "CHAT_MICROCOMPACT_ENABLED", True):
        return messages, 0, 0
    if not messages:
        return messages, 0, 0
    budget = budget_for(resolve_window(context_window))
    ratio = min(max(0.05, _float_setting("CHAT_MICROCOMPACT_TRIGGER_RATIO", 0.6)), 0.98)
    trigger = max(1024, int(budget.effective * ratio))
    if estimate_messages_tokens(messages) < trigger:
        return messages, 0, 0
    return clear_old_tool_results(
        messages,
        keep_recent=_int_setting("CHAT_MICROCOMPACT_KEEP_RECENT", 5),
        max_result_tokens=_int_setting("CHAT_MICROCOMPACT_MAX_RESULT_TOKENS", 6000),
    )


def try_micro_compact_middleware(context_window: Any = None) -> Any | None:
    """构造 LangChain AgentMiddleware：每次模型调用前投影出精简视图（不改原历史）。"""
    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError:
        return None

    state = {"emitted": False}

    def _rewrite(messages: Any) -> Any:
        if not (isinstance(messages, list) and messages and isinstance(messages[0], BaseMessage)):
            return messages
        out, cleared, clipped = compact_tool_results_if_needed(messages, context_window=context_window)
        if out is messages:
            return messages
        if (cleared or clipped) and not state["emitted"]:
            state["emitted"] = True
            emit_rag_step(
                "🧹",
                "轮内上下文清理",
                f"已清理 {cleared} 条旧工具结果，裁剪 {clipped} 条超长结果",
            )
        return out

    def _rewrite_request(request: Any) -> Any:
        messages = getattr(request, "messages", None)
        if not messages:
            return request
        rewritten = _rewrite(messages)
        if rewritten is messages:
            return request
        override = getattr(request, "override", None)
        if callable(override):
            try:
                return override(messages=rewritten)
            except TypeError:
                pass
        try:
            request.messages = rewritten
        except Exception:  # noqa: BLE001
            pass
        return request

    class _MicroCompactToolResults(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            return handler(_rewrite_request(request))

        async def awrap_model_call(self, request, handler):
            return await handler(_rewrite_request(request))

    try:
        return _MicroCompactToolResults()
    except Exception:  # noqa: BLE001
        logger.debug("构造 micro-compact 中间件失败", exc_info=True)
        return None


def wrap_model_micro_compact(model: Any, context_window: Any = None) -> Any:
    """中间件不可用时的回退：直接包装模型的 _generate/_agenerate（非流式路径）。"""

    def _prep(messages: Any) -> Any:
        if not (isinstance(messages, list) and messages and isinstance(messages[0], BaseMessage)):
            return messages
        out, _, _ = compact_tool_results_if_needed(messages, context_window=context_window)
        return out

    orig_generate = getattr(model, "_generate", None)
    if orig_generate is not None:

        def _generate(messages, *args, **kwargs):
            return orig_generate(_prep(messages), *args, **kwargs)

        model._generate = _generate

    orig_agenerate = getattr(model, "_agenerate", None)
    if orig_agenerate is not None:

        async def _agenerate(messages, *args, **kwargs):
            return await orig_agenerate(_prep(messages), *args, **kwargs)

        model._agenerate = _agenerate

    return model


__all__ = [
    "CLEARED_PLACEHOLDER",
    "COMPACTABLE_TOOLS",
    "clear_old_tool_results",
    "compact_tool_results_if_needed",
    "is_compactable_tool",
    "try_micro_compact_middleware",
    "wrap_model_micro_compact",
]
