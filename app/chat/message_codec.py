"""
在 LangChain 消息 和 可持久化的 JSON 之间做编解码，并处理 多模态里的 image_ref（不存 base64，只存引用）。
"""

from __future__ import annotations

import copy
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


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


def _prepare_vision_image_bytes(raw: bytes, *, mime: str | None = None) -> tuple[bytes, str]:
    """
    将原图缩边并压成 JPEG，仅供发给视觉模型；失败则原样返回。
    :return: (bytes, mime)
    """
    fallback_mime = (mime or "image/png").strip() or "image/png"
    if not raw:
        return raw, fallback_mime
    try:
        from io import BytesIO

        from PIL import Image

        from app.settings import settings

        max_edge = max(64, int(getattr(settings, "CHAT_VISION_MAX_EDGE", 1568) or 1568))
        quality = max(30, min(95, int(getattr(settings, "CHAT_VISION_JPEG_QUALITY", 80) or 80)))
        max_bytes = max(10 * 1024, int(getattr(settings, "CHAT_VISION_MAX_BYTES", 400 * 1024) or 400 * 1024))

        img = Image.open(BytesIO(raw))
        img.load()
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1] if rgba.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        longest = max(width, height)
        if longest > max_edge:
            scale = max_edge / float(longest)
            img = img.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )

        def _encode(q: int) -> bytes:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            return buf.getvalue()

        out = _encode(quality)
        q = quality
        while len(out) > max_bytes and q > 40:
            q = max(40, q - 10)
            out = _encode(q)
        return out, "image/jpeg"
    except Exception:
        return raw, fallback_mime


def _image_url_placeholder(block: dict[str, Any]) -> dict[str, Any]:
    fn = ""
    inner = block.get("image_url")
    if isinstance(inner, dict):
        fn = str(inner.get("filename") or "").strip()
    if not fn:
        fn = str(block.get("filename") or "").strip()
    label = f"[图片 {fn}]" if fn else "[图片]"
    return {"type": "text", "text": label}


def strip_image_urls_after_tools(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    本轮已出现 ToolMessage 时，把 Human 里的 image_url 换成短占位，避免后续 ReAct 再传原图。
    无工具结果则原样返回（可同一列表对象）。
    """
    if not any(isinstance(m, ToolMessage) for m in messages):
        return messages
    out: list[BaseMessage] = []
    changed_any = False
    for m in messages:
        if not isinstance(m, HumanMessage) or not isinstance(m.content, list):
            out.append(m)
            continue
        new_blocks: list[Any] = []
        changed = False
        for block in m.content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                new_blocks.append(_image_url_placeholder(block))
                changed = True
            else:
                new_blocks.append(block)
        if changed:
            changed_any = True
            out.append(HumanMessage(content=new_blocks))
        else:
            out.append(m)
    return out if changed_any else messages


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
            payload, out_mime = _prepare_vision_image_bytes(raw, mime=str(mime))
            b64 = base64.b64encode(payload).decode("ascii")
            image_url: dict[str, Any] = {"url": f"data:{out_mime};base64,{b64}"}
            if fn:
                image_url["filename"] = fn
            new_blocks.append({"type": "image_url", "image_url": image_url, "filename": fn})
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
    expand_images: bool = True,
) -> list:
    """
    调用模型前展开 human 消息中的 image_ref。
    images_on_last_human_only=True 时仅最后一条 Human 展开为 data URL。
    expand_images=False 时全部 image_ref 变占位文本（两阶段读图：agent 阶段不见图）。
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
            expand = expand_images
            if expand and images_on_last_human_only:
                expand = i == last_human_i
            expanded = expand_human_image_refs(
                m.content,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                expand_images=expand,
            )
            out.append(HumanMessage(content=expanded))
        else:
            out.append(m)
    return out
