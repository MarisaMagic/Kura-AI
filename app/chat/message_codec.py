"""
在 LangChain 消息 和 可持久化的 JSON 之间做编解码，并处理 多模态里的 image_ref（不存 base64，只存引用）。
"""

from __future__ import annotations

import copy
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage


def msg_content_to_str(content: Any) -> str:
    """
    将消息内容转为可展示/可摘要的纯文本（含 image_ref 占位说明）。
    用于调试打印与会话记忆归档时把多模态内容变成可喂给模型的纯文本。
    :param content: 消息内容
    :return: 可展示/可摘要的纯文本
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                t = block.get("type")
                if t == "text":
                    parts.append(str(block.get("text", "")))
                elif t == "image_ref":
                    fn = (block.get("filename") or "").strip()
                    parts.append(f"[图片{f' {fn}' if fn else ''}]".strip())
                elif t == "file_ref":
                    fn = (block.get("filename") or block.get("attachment_id") or "").strip()
                    parts.append(f"[附件 {fn}]" if fn else "[附件]")
                elif t == "image_url":
                    parts.append("[图片]")
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _serialize_lc_content(content: Any) -> Any:
    """
    序列化为可存入 JSON 的形态（与 LangChain 结构一致，含 image_ref）。
    :param content: 消息内容
    :return: 可存入 JSON 的形态
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[Any] = []
        for block in content:
            if isinstance(block, dict):
                out.append(dict(block))
            else:
                out.append(block)
        return out
    return str(content)


def serialize_message_envelope(msg: BaseMessage) -> dict[str, Any]:
    """
    将单条消息序列化为 envelope：{"v":1,"role":"human|ai|system","lc":...}
    用于存库，LangChain 消息整块 JSON（含多模态 image_ref 等）
    :param msg: 消息
    :return: envelope
    """
    if isinstance(msg, HumanMessage):
        return {"v": 1, "role": "human", "lc": _serialize_lc_content(msg.content)}
    if isinstance(msg, AIMessage):
        return {"v": 1, "role": "ai", "lc": _serialize_lc_content(msg.content)}
    if isinstance(msg, SystemMessage):
        return {"v": 1, "role": "system", "lc": _serialize_lc_content(msg.content)}
    return {"v": 1, "role": msg.type, "lc": _serialize_lc_content(getattr(msg, "content", ""))}


def envelope_to_langchain_message(envelope: dict[str, Any]) -> BaseMessage:
    """
    从 envelope 还原 LangChain 消息（仍为 image_ref，调用方再 expand）。
    :param envelope: envelope
    :return: LangChain 消息
    """
    lc = envelope.get("lc")
    role = envelope.get("role") or "human"
    if role == "human":
        return HumanMessage(content=lc)
    if role == "ai":
        return AIMessage(content=lc)
    if role == "system":
        return SystemMessage(content=lc)
    return HumanMessage(content=lc)


def expand_human_image_refs(
    content: Any,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    expand_images: bool = True,
) -> Any:
    """
    对 image_ref：默认读盘后编成 OpenAI 兼容 data URL。
    expand_images=False 时改为短占位，避免历史图撑满上下文。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content

    from app.chat.attachment_service import file_bytes_for_attachment

    new_blocks: list[Any] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "file_ref":
            continue
        if isinstance(block, dict) and block.get("type") == "image_ref":
            aid = str(block.get("attachment_id") or "").strip()
            if not aid:
                continue
            fn = (block.get("filename") or "").strip()
            if not expand_images:
                label = f"[图片 attachment_id={aid}"
                if fn:
                    label += f" filename={fn}"
                label += "]"
                new_blocks.append({"type": "text", "text": label})
                continue
            raw = file_bytes_for_attachment(aid, user_id=user_id, agent_id=agent_id, session_id=session_id)
            if not raw:
                new_blocks.append({"type": "text", "text": f"[图片附件 {aid} 已不可用]"})
                continue
            import base64

            mime = block.get("mime") or "image/png"
            b64 = base64.b64encode(raw).decode("ascii")
            new_blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        else:
            new_blocks.append(copy.deepcopy(block))
    return new_blocks


def expand_messages_for_model(
    messages: list[BaseMessage],
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    images_on_last_human_only: bool = False,
) -> list:
    """
    调用模型前展开 human 消息中的 image_ref。
    images_on_last_human_only=True 时仅最后一条 Human 展开为 data URL。
    """
    last_human_i = None
    if images_on_last_human_only:
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                last_human_i = i
                break
    out: list = []
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            expand_images = True
            if images_on_last_human_only:
                expand_images = i == last_human_i
            expanded = expand_human_image_refs(
                m.content,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                expand_images=expand_images,
            )
            out.append(HumanMessage(content=expanded))
        else:
            out.append(m)
    return out
