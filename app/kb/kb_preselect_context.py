"""为知识库前置选档构建最近多轮对话文本（可消解指代、承接主题）。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.chat.memory_turns import group_turns, split_system_prefix
from app.chat.message_codec import msg_content_to_str
from app.settings import settings


def _truncate(s: str, max_chars: int) -> str:
    t = s.strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def _format_message_line(msg: BaseMessage, per_msg: int) -> str:
    if isinstance(msg, HumanMessage):
        return "[User]\n" + _truncate(msg_content_to_str(msg.content), per_msg)
    if isinstance(msg, AIMessage):
        body = _truncate(msg_content_to_str(getattr(msg, "content", "")), per_msg)
        tcs = getattr(msg, "tool_calls", None) or []
        if tcs:
            hint = f"（{len(tcs)} 个 tool 调用，略）"
            return "[Assistant]\n" + (body + "\n" if body else "") + hint
        return "[Assistant]\n" + body
    if isinstance(msg, ToolMessage):
        return "[Tool]\n" + _truncate(msg_content_to_str(getattr(msg, "content", "")), per_msg)
    return f"[{type(msg).__name__}]\n" + _truncate(
        msg_content_to_str(getattr(msg, "content", "")), per_msg
    )


def _format_one_turn(turn: list[BaseMessage], per_msg: int) -> str:
    lines: list[str] = []
    for m in turn:
        lines.append(_format_message_line(m, per_msg))
        lines.append("")
    return "\n".join(lines).strip()


def build_kb_preselect_conversation_context(
    messages: list[BaseMessage],
    *,
    user_text: str,
    regenerate: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    返回 (供选档 LLM 使用的前序对话纯文本, 小 meta)。

    普通新消息时 messages 尚不含本轮 user。
    Regenerate 时 messages 以最后一条 Human 结束，该条不计入前序。
    """
    n_turns = max(0, int(getattr(settings, "KB_PRESELECT_CONTEXT_TURNS", 3) or 0))
    meta: dict[str, Any] = {
        "context_turns_requested": n_turns,
        "regenerate": bool(regenerate),
    }
    if n_turns <= 0:
        meta["context_injected"] = False
        meta["context_chars"] = 0
        return "", meta

    _, body = split_system_prefix(list(messages or []))
    turns = group_turns(body)
    if not turns:
        meta["context_injected"] = False
        meta["context_chars"] = 0
        return "", meta

    per_msg = max(200, int(getattr(settings, "KB_PRESELECT_CONTEXT_MAX_MSG_CHARS", 1200) or 1200))
    max_total = max(500, int(getattr(settings, "KB_PRESELECT_CONTEXT_MAX_TOTAL_CHARS", 5000) or 5000))

    if regenerate and len(turns[-1]) == 1 and isinstance(turns[-1][0], HumanMessage):
        context_turns = turns[:-1][-n_turns:]
    else:
        context_turns = turns[-n_turns:]

    if not context_turns:
        meta["context_injected"] = False
        meta["context_chars"] = 0
        return "", meta

    parts: list[str] = []
    for i, t in enumerate(context_turns, start=1):
        block = f"（第 {i}/{len(context_turns)} 轮）\n{_format_one_turn(t, per_msg)}"
        parts.append(block)

    out = _truncate(
        "【前序对话】\n"
        "（仅用于理解指代与主题；与下方「当前用户问题」一致时，选 file_key 以当前问题为主。）\n\n"
        + "\n\n---\n\n".join(parts),
        max_total,
    )
    meta["context_injected"] = bool(out)
    meta["context_chars"] = len(out)
    meta["context_turns_used"] = len(context_turns)
    if regenerate and len(turns[-1]) == 1 and isinstance(turns[-1][0], HumanMessage):
        meta["current_user_effective"] = _truncate(msg_content_to_str(turns[-1][0].content), 200)
    else:
        meta["current_user_effective"] = _truncate((user_text or ""), 200)

    return out, meta
