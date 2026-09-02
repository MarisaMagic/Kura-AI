"""会话压缩：按字符预算触发，摘要进 session metadata，原文窗口 append-only。"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.chat.memory_turns import group_turns, split_system_prefix
from app.chat.message_codec import msg_content_to_str
from app.settings import settings

logger = logging.getLogger(__name__)

COMPACT_TITLE = "【会话压缩摘要】"
_META_SUMMARY = "compact_summary"
_META_UNTIL = "compact_until_turn_index"
_SUMMARIZE_INPUT_MAX_CHARS = 60000
_SUMMARY_MAX_CHARS = 4000

_SUMMARIZE_PROMPT = """你是会话压缩助手。将「已有摘要」与「被移出上下文的旧对话」合并成一份给后续对话用的稳定摘要。

要求：
- 只输出摘要正文，不要标题、不要解释、不要 Markdown 代码块；
- 保留：用户目标与约束、已做决定、未完成事项、关键实体（文件名、file_key、URL、ID、数字与结论）；
- 不要写入本轮检索开关、选档范围或附件列表；
- 不要抄写知识库/网页大段原文，结论与来源线索即可；
- 控制在 {max_chars} 字以内。

已有摘要：
{old_summary}

被移出的对话：
{dropped}
"""


def _messages_chars(messages: list[BaseMessage]) -> int:
    n = 0
    for m in messages:
        n += len(msg_content_to_str(getattr(m, "content", "")))
    return n


def load_compact_state(metadata: dict | None) -> tuple[str, int]:
    meta = metadata if isinstance(metadata, dict) else {}
    summary = str(meta.get(_META_SUMMARY) or "").strip()
    raw = meta.get(_META_UNTIL, -1)
    try:
        until = int(raw)
    except (TypeError, ValueError):
        until = -1
    if until < -1:
        until = -1
    return summary, until


def verbatim_keep_from_index(until_turn_index: int, turn_count: int) -> int:
    if turn_count <= 0:
        return 0
    if until_turn_index < 0:
        return 0
    return min(until_turn_index + 1, turn_count)


def verbatim_keep_from_for_session(
    user_id: int,
    agent_id: int,
    session_id: str,
    turn_count: int | None = None,
) -> int:
    """当前原文窗口起始轮次（不含已压缩轮）。turn_count 未知时只根据 metadata。"""
    from app.chat.storage import storage

    _, until = load_compact_state(storage.get_session_metadata(user_id, agent_id, session_id))
    if turn_count is None:
        return until + 1 if until >= 0 else 0
    return verbatim_keep_from_index(until, turn_count)


def _choose_keep_from(turns: list[list[BaseMessage]], keep_chars: int) -> int:
    if not turns:
        return 0
    acc = 0
    keep_from = len(turns) - 1
    budget = max(1, int(keep_chars or 1))
    for i in range(len(turns) - 1, -1, -1):
        c = _messages_chars(turns[i])
        if i < len(turns) - 1 and acc + c > budget:
            break
        acc += c
        keep_from = i
    return keep_from


def _flatten(prefix: list[BaseMessage], turns: list[list[BaseMessage]]) -> list[BaseMessage]:
    out = list(prefix)
    for t in turns:
        out.extend(t)
    return out


def _summary_message(text: str) -> HumanMessage:
    return HumanMessage(content=f"{COMPACT_TITLE}\n\n{text.strip()}")


def _turns_plain(turns: list[list[BaseMessage]]) -> str:
    from app.chat.memory_archive import turn_to_text

    parts = []
    for i, t in enumerate(turns):
        body = turn_to_text(t)
        if body:
            parts.append(f"--- 轮次偏移 {i} ---\n{body}")
    return "\n\n".join(parts)


def _run_summarizer(old_summary: str, dropped_text: str, llm_config: dict[str, Any]) -> str | None:
    key = (llm_config.get("api_key") or "").strip()
    if not key:
        return None
    dropped = (dropped_text or "").strip()
    if len(dropped) > _SUMMARIZE_INPUT_MAX_CHARS:
        dropped = dropped[:_SUMMARIZE_INPUT_MAX_CHARS] + "\n…（已截断）"
    old = (old_summary or "").strip() or "（无）"
    prompt = _SUMMARIZE_PROMPT.format(
        max_chars=_SUMMARY_MAX_CHARS,
        old_summary=old,
        dropped=dropped or "（无）",
    )
    try:
        from langchain.chat_models import init_chat_model
        from langchain_core.messages import HumanMessage as HM

        from app.utils.egress import pinned_llm_client_kwargs

        model = init_chat_model(
            model=(llm_config.get("model_name") or "gpt-4"),
            model_provider="openai",
            api_key=key,
            base_url=(llm_config.get("base_url") or "").strip() or None,
            temperature=0,
            stream_usage=False,
            **pinned_llm_client_kwargs((llm_config.get("base_url") or "").strip() or None),
        )
        out = model.invoke([HM(content=prompt)])
        text = (getattr(out, "content", None) or str(out)).strip()
        if not text:
            return None
        if len(text) > _SUMMARY_MAX_CHARS:
            text = text[:_SUMMARY_MAX_CHARS] + "…"
        return text
    except Exception:
        logger.exception("会话压缩摘要失败")
        return None


def _estimated_prompt_chars(
    *,
    system_chars: int,
    summary: str,
    verbatim: list[BaseMessage],
) -> int:
    trigger_tools = max(0, int(getattr(settings, "CHAT_COMPACT_TOOLS_ESTIMATE_CHARS", 8000) or 0))
    headroom = max(0, int(getattr(settings, "CHAT_COMPACT_HEADROOM_CHARS", 12000) or 0))
    return (
        trigger_tools
        + max(0, int(system_chars))
        + len(summary or "")
        + _messages_chars(verbatim)
        + headroom
    )


def build_compacted_model_messages(
    messages: list[BaseMessage],
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
    system_chars: int,
) -> list[BaseMessage]:
    """
    构建送给主模型的压缩视图（不修改 storage 中的原文）。
    达阈值时用子智能体摘要被挤出窗口的轮次，写入 session metadata 后跨轮原样重放。
    """
    from app.chat.storage import storage
    from app.chat.tools import emit_rag_step

    prefix, body = split_system_prefix(messages)
    turns = group_turns(body)
    if not turns:
        return list(messages)

    meta = storage.get_session_metadata(user_id, agent_id, session_id)
    summary, until = load_compact_state(meta)
    keep_from = verbatim_keep_from_index(until, len(turns))
    keep_chars = max(1000, int(getattr(settings, "CHAT_COMPACT_KEEP_CHARS", 24000) or 24000))
    trigger = max(keep_chars + 1, int(getattr(settings, "CHAT_COMPACT_TRIGGER_CHARS", 80000) or 80000))

    verbatim_turns = turns[keep_from:]
    verbatim_msgs = _flatten([], verbatim_turns)
    est = _estimated_prompt_chars(
        system_chars=system_chars,
        summary=summary,
        verbatim=prefix + verbatim_msgs,
    )

    if est >= trigger and keep_from < len(turns) - 1:
        new_keep = _choose_keep_from(turns, keep_chars)
        new_keep = max(new_keep, keep_from)
        if new_keep > keep_from:
            dropped = turns[keep_from:new_keep]
            dropped_text = _turns_plain(dropped)
            new_summary = _run_summarizer(summary, dropped_text, llm_config)
            if new_summary:
                until = new_keep - 1
                summary = new_summary
                keep_from = new_keep
                storage.patch_session_metadata(
                    user_id,
                    agent_id,
                    session_id,
                    {_META_SUMMARY: summary, _META_UNTIL: until},
                )
                emit_rag_step("📦", "会话压缩", f"已摘要较早对话，原文自轮次 {keep_from} 起")
            else:
                # 压缩失败：本轮只截断原文窗口，不更新 metadata
                keep_from = new_keep
                emit_rag_step("⚠️", "会话压缩失败", "本轮仅截断较早原文，下次再试")

    out = list(prefix)
    if summary:
        out.append(_summary_message(summary))
    for t in turns[keep_from:]:
        out.extend(t)
    return out
