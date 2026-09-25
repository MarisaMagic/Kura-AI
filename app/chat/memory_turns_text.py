"""轮次消息 → 纯文本（供压缩摘要输入与 raw 归档共用）。

独立成模块是为了打破 compact ↔ memory_archive 的循环导入：
两侧都要把「一轮对话」渲染成文本，实现必须只有一份。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.chat.message_codec import msg_content_to_str


def clip_keep_ends(text: str, max_chars: int) -> str:
    """超长文本裁剪：保留头尾、省略中段。

    朴素的 ``text[:max_chars] + "…"`` 会把结论/最新进展（通常在尾部）整段切掉，
    无论用于摘要器输入还是向量归档都是丢最有价值的部分，故改为两端保留。
    """
    s = text or ""
    if len(s) <= max_chars:
        return s
    if max_chars <= 40:
        return s[:max_chars] + "…"
    head = int(max_chars * 0.25)
    tail = max_chars - head - 20
    return s[:head] + "\n…（中段已省略）…\n" + s[-tail:]


def format_tool_message(msg: ToolMessage) -> str:
    """格式化工具消息。"""
    name = (getattr(msg, "name", None) or "") or ""
    body = clip_keep_ends(msg_content_to_str(msg.content), 2000)
    return f"工具({name}): {body}" if name else f"工具: {body}"


def format_ai_message(msg: AIMessage) -> str:
    """格式化助手消息。"""
    parts: list[str] = []
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        parts.append(f"[tool_calls] {tool_calls}")
    body = msg_content_to_str(getattr(msg, "content", ""))
    if body:
        parts.append(body)
    return f"助手: {clip_keep_ends(chr(10).join(parts), 12000)}"


def turn_to_text(turn: list[BaseMessage]) -> str:
    """将一轮消息渲染为纯文本。"""
    lines: list[str] = []
    for msg in turn:
        if isinstance(msg, HumanMessage):
            lines.append(f"用户: {clip_keep_ends(msg_content_to_str(msg.content), 8000)}")
        elif isinstance(msg, AIMessage):
            lines.append(format_ai_message(msg))
        elif isinstance(msg, ToolMessage):
            lines.append(format_tool_message(msg))
        else:
            t = msg_content_to_str(getattr(msg, "content", ""))
            lines.append(f"{msg.__class__.__name__}: {clip_keep_ends(t, 4000)}")
    return "\n".join(lines).strip()
