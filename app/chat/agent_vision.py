"""视觉 payload：工具轮去图、两阶段读图判定。"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.chat.message_codec import strip_image_urls_after_tools
from app.chat.tools import emit_rag_step
from app.models.user_agent import UserAgent
from app.settings import settings


def _human_content_has_image_ref(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "image_ref" for b in content)


def _emit_understanding_image_step(messages: list[BaseMessage]) -> None:
    last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if last_human and _human_content_has_image_ref(last_human.content):
        emit_rag_step("🖼️", "正在理解图片", "已附加图片，等待模型分析")


def _should_run_vision_caption(
    messages: list[BaseMessage],
    ua: UserAgent,
    *,
    use_knowledge_retrieval: bool,
    use_web_search: bool,
    has_mcp_tools: bool,
) -> bool:
    """两阶段读图触发条件：本轮带图 + 支持视觉 + 启用了检索类工具 + 配置开启。"""
    if not getattr(settings, "CHAT_VISION_CAPTION_ENABLED", True):
        return False
    if not bool(getattr(ua, "supports_vision", False)):
        return False
    if not (use_web_search or use_knowledge_retrieval or has_mcp_tools):
        return False
    last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    return bool(last_human and _human_content_has_image_ref(last_human.content))


def _wrap_model_strip_images_after_tools(model: Any) -> Any:
    """每次模型调用前：若已有 ToolMessage 则去掉 image_url。bind_tools 仍共用同一实例。"""

    def _prep(messages: Any) -> Any:
        if isinstance(messages, list) and messages and isinstance(messages[0], BaseMessage):
            return strip_image_urls_after_tools(messages)
        return messages

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

    orig_stream = getattr(model, "_stream", None)
    if orig_stream is not None:

        def _stream(messages, *args, **kwargs):
            return orig_stream(_prep(messages), *args, **kwargs)

        model._stream = _stream

    orig_astream = getattr(model, "_astream", None)
    if orig_astream is not None:

        async def _astream(messages, *args, **kwargs):
            async for chunk in orig_astream(_prep(messages), *args, **kwargs):
                yield chunk

        model._astream = _astream

    return model


def _try_strip_images_middleware() -> Any | None:
    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError:
        return None

    def _rewrite_request(request: Any) -> Any:
        messages = getattr(request, "messages", None)
        if not messages:
            return request
        stripped = strip_image_urls_after_tools(messages)
        if stripped is messages:
            return request
        override = getattr(request, "override", None)
        if callable(override):
            try:
                return override(messages=stripped)
            except TypeError:
                pass
        try:
            request.messages = stripped
        except Exception:
            pass
        return request

    class _StripImagesAfterTools(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            return handler(_rewrite_request(request))

        async def awrap_model_call(self, request, handler):
            return await handler(_rewrite_request(request))

    try:
        return _StripImagesAfterTools()
    except Exception:
        return None

