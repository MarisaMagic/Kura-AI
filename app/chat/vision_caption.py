"""
两阶段读图：阶段 1 无工具视觉调用，产出图片描述（caption）。

仅在「本轮带图且启用了检索类工具」时由 agent_service 调用；
产出的描述作为文本注入阶段 2 的纯文本 agent，避免「看图 + 选工具」挤在同一次慢调用里。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.chat.message_codec import expand_human_image_refs, msg_content_to_str
from app.models.user_agent import UserAgent
from app.settings import settings

_CAPTION_SYSTEM_PROMPT = """你是图片理解助手。用户会附带图片和一段问题，你的任务是「读图」，不是直接回答。

要求：
- 结合用户问题，描述图片中与之相关的内容；
- 图中的关键文字、名称、数字、日期、网址等逐条列出，尽量准确转写；
- 能认出的角色名、作品名、品牌、文物名，用常见中文及英文或日文检索名写出，便于后续搜图；
- 另行列出 3–8 个适合搜图的外观标签（发色、服装主色、标志物等）；
- 认不出专名时只写外观标签，不要编造名称；
- 看不清或不确定的内容如实说明，不要编造；
- 不要调用任何工具，不要联网；
- 输出纯文本，控制在 {max_chars} 字以内。"""


def _caption_max_chars() -> int:
    return max(200, int(getattr(settings, "CHAT_VISION_CAPTION_MAX_CHARS", 1200) or 1200))


def _caption_timeout_seconds() -> int:
    return max(5, int(getattr(settings, "CHAT_VISION_CAPTION_TIMEOUT_SECONDS", 60) or 60))


def _build_caption_model(ua: UserAgent) -> Any:
    """无工具视觉模型：主配置 + 客户端超时（同步/异步共用）。"""
    from app.chat.agent_service import _llm_config_from_ua

    cfg = _llm_config_from_ua(ua)
    base_url = (cfg.get("base_url") or "").strip() or None
    from app.utils.egress import pinned_llm_client_kwargs

    return init_chat_model(
        model=cfg["model_name"],
        model_provider="openai",
        api_key=cfg["api_key"],
        base_url=base_url,
        timeout=_caption_timeout_seconds(),
        **pinned_llm_client_kwargs(base_url),
    )


def _build_caption_messages(
    human_msg: HumanMessage,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> list[BaseMessage]:
    """System 看图指令 + 展开 image_ref 后的用户消息（压图在展开时完成）。"""
    expanded = expand_human_image_refs(
        human_msg.content,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        expand_images=True,
    )
    system = SystemMessage(content=_CAPTION_SYSTEM_PROMPT.format(max_chars=_caption_max_chars()))
    return [system, HumanMessage(content=expanded)]


def _chunk_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


async def iter_image_caption_chunks(
    ua: UserAgent,
    human_msg: HumanMessage,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> AsyncIterator[str]:
    """流式产出图片描述文本块；异常直接抛出，由调用方回退单阶段。"""
    model = _build_caption_model(ua)
    messages = _build_caption_messages(
        human_msg, user_id=user_id, agent_id=agent_id, session_id=session_id
    )
    async for chunk in model.astream(messages):
        text = _chunk_text(getattr(chunk, "content", ""))
        if text:
            yield text


def generate_image_caption(
    ua: UserAgent,
    human_msg: HumanMessage,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> str:
    """同步产出完整图片描述；异常直接抛出，由调用方回退单阶段。"""
    model = _build_caption_model(ua)
    messages = _build_caption_messages(
        human_msg, user_id=user_id, agent_id=agent_id, session_id=session_id
    )
    result = model.invoke(messages)
    return msg_content_to_str(getattr(result, "content", "")).strip()
