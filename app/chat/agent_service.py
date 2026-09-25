"""
LangChain Agent 对话（同步 invoke + 异步 SSE 流式）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import Any, AsyncIterator

from langchain.chat_models import init_chat_model
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)

from app.chat.attachment_service import build_storable_human_content, format_attachment_hint
from app.chat.attachment_tools import make_session_attachment_tools
from app.chat.history_tool import make_session_history_tools
from app.chat.memory_turns import apply_sliding_window_turns
from app.chat.user_memory_tool import make_read_user_memory_tool
from app.chat.message_codec import (
    expand_messages_for_model,
    msg_content_to_str,
)
from app.chat.storage import storage
from app.chat.tools import (
    emit_rag_step,
    get_last_rag_context,
    get_pending_mcp_confirmations,
    reset_tool_call_guards,
    set_approved_mcp_pending_id,
    set_rag_step_queue,
    set_turn_tool_policy,
)
from app.chat.web_search_tool import (
    make_fetch_url_tool,
    make_web_image_search_tool,
    make_web_search_tool,
)
from app.kb.image_search_tool import make_search_knowledge_by_image_tool
from app.kb.kb_scope import kb_scope_for
from app.kb.search_tool import make_search_knowledge_tool
from app.mcp_client.service import load_agent_mcp_tools
from app.models.user_agent import UserAgent
from app.settings import settings
from app.utils.api_key_crypto import decrypt_api_key_safe


from app.chat.agent_prompt import (
    _WEB_SEARCH_DISCIPLINE,  # noqa: F401  （tests 经由本模块引用此常量）
    _agent_invoke_config,
    _append_turn_context_message,
    _compose_system_prompt,
    _format_turn_context_block,
)
from app.chat.agent_vision import (
    _emit_understanding_image_step,
    _human_content_has_image_ref,
    _should_run_vision_caption,
    _try_strip_images_middleware,
    _wrap_model_strip_images_after_tools,
)

logger = logging.getLogger(__name__)


def _mcp_tools_allowed_for(ua: UserAgent, user_id: int) -> bool:
    """仅智能体属主会话允许加载其 MCP 工具。

    MCP 工具携带属主凭据（如预置的敏感请求头）且由模型自主调用、无用户确认；
    共享（非属主）会话加载后可被对话内容或注入文本驱动，以属主身份执行操作，故默认禁止。
    可通过 SHARE_CHAT_ALLOW_OWNER_MCP_TOOLS=true 恢复旧行为（不推荐）。
    """
    if int(getattr(ua, "user_id", 0) or 0) == int(user_id):
        return True
    return bool(getattr(settings, "SHARE_CHAT_ALLOW_OWNER_MCP_TOOLS", False))


def _wrap_async_tool_for_sync(tool: Any) -> Any:
    """
    将 async-only 工具（MCP 适配器产出）包装为同步可调用，供 chat_with_agent_sync 路径使用。
    调用方须保证运行在无活动事件循环的线程中（endpoint 经 asyncio.to_thread 包裹）。
    """
    from langchain_core.tools import StructuredTool

    def _sync_func(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(tool.ainvoke(kwargs if kwargs else (args[0] if args else {})))

    return StructuredTool.from_function(
        func=_sync_func,
        name=getattr(tool, "name", None) or "mcp_tool",
        description=getattr(tool, "description", "") or "",
        args_schema=getattr(tool, "args_schema", None),
    )


def _llm_config_from_ua(ua: UserAgent) -> dict[str, Any]:
    """
    从智能体配置中获取 LLM 配置
    :param ua: 智能体
    :return: LLM 配置
    """
    plain = decrypt_api_key_safe(ua.api_key_ciphertext)
    base_url = (ua.base_url or "").strip() or None
    if base_url:
        from app.utils.ssrf import assert_public_http_url

        assert_public_http_url(base_url)
    return {
        "api_key": (plain or "").strip(),
        "base_url": base_url,
        "model_name": ua.model_name,
    }


def _sub_llm_config_from_ua(ua: UserAgent) -> dict[str, Any]:
    """
    打杂任务（记忆重写、知识库选档、RAG 打分/改写/HyDE）使用的子智能体 LLM 配置。
    仅当 sub_model_name 非空且子 Key 可解密出非空值时使用子配置（整体判定，不混搭）；
    否则实时回退主配置，主配置变更自动跟随。
    :param ua: 智能体
    :return: LLM 配置
    """
    plain = decrypt_api_key_safe(ua.sub_api_key_ciphertext)
    sub_model = (ua.sub_model_name or "").strip()
    if sub_model and (plain or "").strip():
        base_url = (ua.sub_base_url or "").strip() or None
        if base_url:
            from app.utils.ssrf import assert_public_http_url

            assert_public_http_url(base_url)
        return {
            "api_key": plain.strip(),
            "base_url": base_url,
            "model_name": sub_model,
        }
    return _llm_config_from_ua(ua)


def _run_kb_document_preselect_with_context(
    messages: list[BaseMessage],
    current_question: str,
    *,
    regenerate: bool,
    ua: UserAgent,
    agent_id: int,
) -> tuple[list[str] | None, dict[str, Any]]:
    """知识库前置选档：可附带最近多轮对话，meta 含 context_* 与选档结果。选档范围按属主隔离。"""
    from app.kb.kb_document_preselect import run_kb_document_preselect
    from app.kb.kb_preselect_context import build_kb_preselect_conversation_context
    from app.kb.kb_scope import kb_scope_for

    ctx, ctx_meta = build_kb_preselect_conversation_context(
        messages,
        user_text=(current_question or "").strip(),
        regenerate=regenerate,
    )
    filt, pre_meta = run_kb_document_preselect(
        (current_question or "").strip(),
        kb_scope_for(ua.user_id, agent_id),
        _sub_llm_config_from_ua(ua),
        conversation_context=ctx or None,
    )
    return filt, {**ctx_meta, **pre_meta}


def _mcp_approval_note(
    pending_id: str | None,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> str | None:
    if not (pending_id or "").strip():
        return None
    from app.mcp_client.tool_policy import peek_approved_mcp_call

    approved = peek_approved_mcp_call(
        pending_id, user_id=user_id, agent_id=agent_id, session_id=session_id
    )
    if not approved:
        return None
    return (
        f"用户已批准执行 MCP 工具 {approved.get('server_name')}/{approved.get('tool_name')} 一次。"
        "请立即调用该工具一次；不要调用其他高危工具。"
    )


def _prepare_to_invoke_messages(
    messages: list[BaseMessage],
    ua: UserAgent,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool,
    use_web_search: bool,
    document_filter: list[str] | None,
    session_attachment_hint: str,
    mcp_approval_note: str | None = None,
    path_ids: list[int] | None = None,
    image_caption: str | None = None,
    tools_tokens: int = 0,
    budget_out: dict[str, Any] | None = None,
) -> list:
    """
    压缩或滑动窗口 → 展开多模态（仅本轮 Human 带图）→ 本轮上下文并入最后一条 Human。
    path_ids 为 messages 对应的存储行 id（等长），供压缩与记忆按分支定位。
    image_caption 非空时（两阶段读图）：不再展开图片，描述随本轮上下文注入，agent 全程纯文本。
    :param tools_tokens: 本轮绑定工具 schema 的实测 token 数（由 build_model_and_agent 回写）
    :param budget_out: 可选出参，回写本轮 token 预算/估算/校准，供落库后做 usage 校准
    """
    from app.chat.compact import build_compacted_model_messages
    from app.chat.context_budget import resolve_window

    llm_cfg = _sub_llm_config_from_ua(ua)
    system_prompt = _compose_system_prompt(
        ua,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
    )
    if getattr(settings, "CHAT_USE_SESSION_MEMORY", True) and getattr(settings, "CHAT_COMPACT_ENABLED", True):
        viewed = build_compacted_model_messages(
            messages,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            llm_config=llm_cfg,
            system_chars=len(system_prompt),
            system_prompt=system_prompt,
            tools_tokens=tools_tokens,
            context_window=resolve_window(getattr(ua, "context_window", None)),
            path_ids=path_ids,
            budget_out=budget_out,
        )
    else:
        viewed = apply_sliding_window_turns(messages)
    has_caption = bool((image_caption or "").strip())
    expanded = expand_messages_for_model(
        viewed,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        images_on_last_human_only=not has_caption,
        expand_images=not has_caption,
    )
    turn_block = _format_turn_context_block(
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        document_filter=document_filter,
        session_attachment_hint=session_attachment_hint,
        mcp_approval_note=mcp_approval_note,
        image_caption=image_caption,
    )
    return _append_turn_context_message(expanded, turn_block)


def _sort_tools(tools: list[Any]) -> list[Any]:
    return sorted(tools, key=lambda t: str(getattr(t, "name", "") or ""))


def build_model_and_agent(
    ua: UserAgent,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    knowledge_base_document_filter: list[str] | None = None,
    extra_tools: list[Any] | None = None,
    use_knowledge_retrieval: bool = True,
    use_web_search: bool = False,
    metrics_out: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """
    构建模型和智能体。检索工具始终挂载（本轮禁用由工具函数返回 TOOL_DISABLED_THIS_TURN）。
    知识库检索按属主隔离：使用他人已发布智能体时检索发布者的知识库。
    :param metrics_out: 可选出参，回写 tools_tokens / tools_count / system_tokens 供上下文预算使用
    """
    plain = decrypt_api_key_safe(ua.api_key_ciphertext)
    if not plain or not plain.strip():
        raise ValueError("智能体未配置有效的 API Key")

    base_url = (ua.base_url or "").strip() or None
    from app.utils.egress import pinned_llm_client_kwargs

    pinned_kwargs = pinned_llm_client_kwargs(base_url)
    model = init_chat_model(
        model=ua.model_name,
        model_provider="openai",
        api_key=plain.strip(),
        base_url=base_url,
        temperature=float(ua.temperature),
        stream_usage=True,
        **pinned_kwargs,
    )
    model = _wrap_model_strip_images_after_tools(model)
    kb_scope = kb_scope_for(ua.user_id, ua.id)
    llm_config = _sub_llm_config_from_ua(ua)
    tools: list[Any] = []
    tools.extend(make_session_attachment_tools(user_id, agent_id, session_id))
    if getattr(settings, "WEB_SEARCH_ENABLED", True):
        tools.append(make_web_search_tool())
        tools.append(make_fetch_url_tool())
        tools.append(make_web_image_search_tool())
    tools.append(
        make_search_knowledge_tool(
            kb_scope,
            llm_config,
            knowledge_base_document_filter=knowledge_base_document_filter,
        )
    )
    tools.append(make_search_knowledge_by_image_tool(kb_scope, user_id, agent_id, session_id))
    if getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        tools.append(make_read_user_memory_tool(user_id, agent_id))
        tools.extend(make_session_history_tools(user_id, agent_id, session_id))
    if extra_tools:
        tools.extend(extra_tools)
    tools = _sort_tools(tools)
    from langchain.agents import create_agent

    system_prompt = _compose_system_prompt(
        ua,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
    )
    if isinstance(metrics_out, dict):
        from app.chat.context_budget import estimate_tokens, tools_schema_tokens

        metrics_out["tools_tokens"] = tools_schema_tokens(tools)
        metrics_out["tools_count"] = len(tools)
        metrics_out["system_tokens"] = estimate_tokens(system_prompt)
        metrics_out["system_chars"] = len(system_prompt)
        # 缓存实测值，供上下文占用接口在未构建 agent 时估算工具 schema 开销
        try:
            from app.chat.cache import cache

            cache.set_json(
                f"chat_ctx_fixed:{agent_id}",
                {
                    "tools_tokens": metrics_out["tools_tokens"],
                    "system_tokens": metrics_out["system_tokens"],
                    "system_chars": metrics_out["system_chars"],
                    "use_knowledge_retrieval": use_knowledge_retrieval,
                    "use_web_search": use_web_search,
                },
                ttl=86400,
            )
        except Exception:  # noqa: BLE001
            logger.debug("缓存上下文固定开销失败", exc_info=True)
    agent_kwargs: dict[str, Any] = {
        "model": model,
        "tools": tools,
        "system_prompt": system_prompt,
    }
    middlewares = _build_agent_middlewares(getattr(ua, "context_window", None))
    if middlewares:
        try:
            agent = create_agent(**agent_kwargs, middleware=middlewares)
        except (TypeError, Exception):
            logger.debug("create_agent 不接受 middleware，回退为模型级包装", exc_info=True)
            agent = create_agent(**agent_kwargs)
            model = _wrap_model_micro_compact_fallback(model, ua)
    else:
        agent = create_agent(**agent_kwargs)
    return agent, model


def _wrap_model_micro_compact_fallback(model: Any, ua: UserAgent) -> Any:
    """create_agent 不支持 middleware 时的兜底：在模型层做轮内工具结果清理。"""
    if not getattr(settings, "CHAT_MICROCOMPACT_ENABLED", True):
        return model
    try:
        from app.chat.tool_result_compact import wrap_model_micro_compact

        return wrap_model_micro_compact(model, getattr(ua, "context_window", None))
    except Exception:  # noqa: BLE001
        logger.debug("micro-compact 模型级包装失败", exc_info=True)
        return model


def _build_agent_middlewares(context_window: Any = None) -> list[Any]:
    """收集模型调用中间件：工具轮去图 + 轮内旧工具结果清理（micro-compact）。"""
    out: list[Any] = []
    strip = _try_strip_images_middleware()
    if strip is not None:
        out.append(strip)
    if getattr(settings, "CHAT_MICROCOMPACT_ENABLED", True):
        from app.chat.tool_result_compact import try_micro_compact_middleware

        mc = try_micro_compact_middleware(context_window)
        if mc is not None:
            out.append(mc)
    return out


def _extract_response_content(result: Any) -> str:
    """
    提取响应内容
    :param result: 响应结果
    :return: 响应内容
    """
    # 如果响应结果是字典, 则返回响应内容
    if isinstance(result, dict):
        if "output" in result:
            return str(result["output"])
        # 如果响应结果包含消息, 则返回最后一条消息的内容
        if "messages" in result and result["messages"]:
            msg = result["messages"][-1]
            return str(getattr(msg, "content", msg))
        return str(result)
    # 如果响应结果有 content 属性, 则返回 content 属性
    if hasattr(result, "content"):
        return str(result.content)
    return str(result)


def _extract_usage_metadata(result: Any) -> Any:
    """
    从 invoke 结果中取最后一条带 usage_metadata 的 AI 消息用量。
    stream_usage=True 时服务端会回报真实 input_tokens，用于反推 token 估算校准系数。
    :param result: agent.invoke 的返回
    :return: usage_metadata（dict 或对象）；取不到返回 None
    """
    msgs = result.get("messages") if isinstance(result, dict) else None
    for msg in reversed(list(msgs or [])):
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            return usage
    return None


def _calibrate_token_estimate(
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    usage: Any,
    estimated: int,
    model_name: str,
) -> None:
    """用本轮真实 usage 校准 token 估算系数（同步小写入）。

    长期记忆由压缩摘要同一次 LLM 调用顺带抽取并落 PG，这里不再做归档。
    """
    from app.chat.compact import record_usage_calibration

    record_usage_calibration(
        user_id,
        agent_id,
        session_id,
        usage=usage,
        estimated=int(estimated or 0),
        model_name=str(model_name or ""),
    )


def chat_with_agent_sync(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
    use_web_search: bool = False,
    attachment_ids: list[str] | None = None,
    mcp_approved_pending_id: str | None = None,
) -> dict:
    """
    同步对话
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param use_web_search: 是否启用联网搜索工具（与知识库检索互斥）
    :param attachment_ids: 本会话已上传附件 ID（顺序与引用一致）
    :return: 响应结果
    """
    attachment_ids = attachment_ids or []
    # 加载会话消息
    messages = storage.load(user_id, agent_id, session_id)

    # 清空 RAG 上下文, 重置工具调用守卫
    get_last_rag_context(clear=True)
    reset_tool_call_guards()
    set_turn_tool_policy(use_knowledge_retrieval=use_knowledge_retrieval, use_web_search=use_web_search)
    set_approved_mcp_pending_id(mcp_approved_pending_id)

    session_attachment_hint = format_attachment_hint(user_id, agent_id, session_id)

    kb_preselect_meta: dict[str, Any] = {}
    retrieval_filter: list[str] | None = None
    if use_knowledge_retrieval:
        retrieval_filter, kb_preselect_meta = _run_kb_document_preselect_with_context(
            messages,
            (user_text or "").strip(),
            regenerate=False,
            ua=ua,
            agent_id=agent_id,
        )

    # 加载该智能体已启用的 MCP 工具（本函数在无线事件循环的线程中运行，asyncio.run 安全）；
    # MCP 工具为 async-only，包装为同步调用；单服务失败仅记录到 rag_steps，不中断对话。
    # 共享（非属主）会话跳过加载，避免属主凭据被共享用户对话驱动。
    mcp_tools: list[Any] = []
    mcp_errors: list[dict] = []
    if _mcp_tools_allowed_for(ua, user_id):
        try:
            raw_mcp_tools, mcp_errors = asyncio.run(load_agent_mcp_tools(agent_id, user_id=user_id, session_id=session_id))
            mcp_tools = [_wrap_async_tool_for_sync(t) for t in raw_mcp_tools]
        except RuntimeError:
            # 兜底：若意外处于运行中的事件循环（不应发生），跳过 MCP 不阻断对话
            mcp_tools, mcp_errors = [], [{"name": "(loader)", "error": "sync 路径处于活动事件循环，已跳过 MCP 加载"}]

    # 构建智能体和大模型（含会话附件工具）
    agent_metrics: dict[str, Any] = {}
    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        knowledge_base_document_filter=retrieval_filter if use_knowledge_retrieval else None,
        extra_tools=mcp_tools,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        metrics_out=agent_metrics,
    )

    human_content = build_storable_human_content(
        user_text,
        attachment_ids,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        supports_vision=bool(getattr(ua, "supports_vision", False)),
    )
    # 将用户消息添加到会话消息中（存库为 image_ref + 文本块 JSON）
    human_msg = HumanMessage(content=human_content)
    messages.append(human_msg)
    storage.append_messages(user_id, agent_id, session_id, [human_msg])

    # 当前路径各消息对应的存储行 id，供压缩/记忆按 turn_key 定位
    path_records = storage.get_session_messages(user_id, agent_id, session_id)
    path_ids: list[int] | None = [int(r.get("message_id") or 0) for r in path_records]
    if len(path_ids) != len(messages):
        path_ids = None

    class _SyncRagStepCollector:
        def __init__(self) -> None:
            self.steps: list[dict] = []

        def put_nowait(self, step: dict) -> None:
            self.steps.append(step)

    rag_collector = _SyncRagStepCollector()
    set_rag_step_queue(rag_collector, sync=True)

    # MCP 加载结果写入本轮步骤（供响应与历史展示）
    if mcp_tools:
        emit_rag_step("🧩", "MCP 工具已加载", f"{len(mcp_tools)} 个工具")
    for err in mcp_errors:
        emit_rag_step("⚠️", f"MCP 服务「{err['name']}」不可用", err["error"][:120])
    _emit_understanding_image_step(messages)

    # 两阶段读图（同步路径）：先无工具看图出描述，失败回退单阶段带图调用
    image_caption: str | None = None
    if _should_run_vision_caption(
        messages,
        ua,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        has_mcp_tools=bool(mcp_tools),
    ):
        from app.chat.vision_caption import generate_image_caption

        caption_human = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
        try:
            caption_text = generate_image_caption(
                ua,
                caption_human,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            )
        except Exception:
            caption_text = ""
        if caption_text:
            image_caption = caption_text
            emit_rag_step("🖼️", "图片理解完成", f"已预读图片内容（{len(caption_text)} 字）")
        else:
            emit_rag_step("⚠️", "图片理解失败", "改为直接带图问答")

    budget_info: dict[str, Any] = {}
    to_invoke = _prepare_to_invoke_messages(
        messages,
        ua,
        user_id,
        agent_id,
        session_id,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        document_filter=retrieval_filter if use_knowledge_retrieval else None,
        session_attachment_hint=session_attachment_hint,
        mcp_approval_note=_mcp_approval_note(
            mcp_approved_pending_id, user_id=user_id, agent_id=agent_id, session_id=session_id
        ),
        path_ids=path_ids,
        image_caption=image_caption,
        tools_tokens=int(agent_metrics.get("tools_tokens") or 0),
        budget_out=budget_info,
    )
    caught_exc: Exception | None = None
    response_content = ""
    result: Any = None
    try:
        # invoke 调用智能体（展开 image_ref 为 data URL）
        result = agent.invoke({"messages": to_invoke}, config=_agent_invoke_config())
        response_content = _extract_response_content(result)
    except Exception as e:
        caught_exc = e
    finally:
        set_rag_step_queue(None)

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None
    pending_mcp = get_pending_mcp_confirmations(clear=True)
    # 获取图片引用
    image_references = rag_context.get("image_references") if rag_context else None
    # 获取来源列表（知识库 / 联网搜索互斥，合并走统一 sources 通道）
    kb_sources = rag_context.get("kb_sources") if rag_context else None
    web_sources = rag_context.get("web_sources") if rag_context else None
    merged_sources = kb_sources or web_sources

    error_text = str(caught_exc) if caught_exc else None

    # 助手落库仅用纯文本；image_references 放 extra，避免多模态块写入历史导致下游 API（如智谱 1210）再次请求失败
    # thinking_items：步骤 + 两阶段读图的描述文本（插在「正在理解图片」步骤之后，保持交错顺序）
    thinking_items = [{"type": "step", **s} for s in rag_collector.steps]
    if image_caption:
        insert_at = next(
            (i + 1 for i, it in enumerate(thinking_items) if it.get("label") == "正在理解图片"),
            len(thinking_items),
        )
        thinking_items.insert(insert_at, {"type": "text", "text": image_caption})
    ai_msg = AIMessage(content=response_content)
    messages.append(ai_msg)
    storage.append_messages(
        user_id,
        agent_id,
        session_id,
        [ai_msg],
        extra_message_data=[
            {
                "rag_trace": rag_trace,
                "rag_steps": rag_collector.steps or None,
                "error_text": error_text,
                "image_references": image_references,
                "sources": merged_sources,
                "kb_preselect": kb_preselect_meta or None,
                "thinking_items": thinking_items or None,
            }
        ],
    )

    # 落库后收尾：用真实 usage 同步校准 token 估算系数（长期记忆由摘要调用顺带写入）
    _calibrate_token_estimate(
        user_id,
        agent_id,
        session_id,
        usage=_extract_usage_metadata(result),
        estimated=int(budget_info.get("estimated") or 0),
        model_name=str(budget_info.get("model_name") or ua.model_name or ""),
    )

    if caught_exc:
        raise caught_exc

    return {
        "response": response_content,
        "rag_trace": rag_trace,
        "sources": merged_sources,
        "kb_preselect": kb_preselect_meta or None,
        "pending_mcp_confirmations": pending_mcp or None,
    }


def _throttled_cancel_check(
    cancel_check: Callable[[], bool] | None,
) -> Callable[[], Any] | None:
    """
    取消检查节流包装：流式循环按 chunk 频率调用本函数，但实际读 Redis（线程跳转）
    最多每 CHAT_CANCEL_CHECK_INTERVAL 秒一次，期间返回上次结果。
    一旦观察到取消则永久为真（取消不可逆）。
    """
    if cancel_check is None:
        return None
    interval = max(0.05, float(getattr(settings, "CHAT_CANCEL_CHECK_INTERVAL", 0.25) or 0.25))
    state = {"last_at": 0.0, "flag": False}

    async def _probe() -> bool:
        if state["flag"]:
            return True
        now = time.monotonic()
        if now - state["last_at"] < interval:
            return False
        state["last_at"] = now
        try:
            state["flag"] = bool(await asyncio.to_thread(cancel_check))
        except Exception:
            state["flag"] = False
        return state["flag"]

    return _probe


async def iter_chat_stream_events(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
    use_web_search: bool = False,
    attachment_ids: list[str] | None = None,
    regenerate: bool = False,
    target_message_id: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
    mcp_approved_pending_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    异步对话：产出与 SSE 中 `data: {...}` 相同结构的 dict 事件（供直连 SSE 与后台 Job 复用）。
    最后依次产出 trace（若有）、写入存储后产出 done。
    :param ua: 智能体
    :param user_text: 用户文本（regenerate 时仅作校验用，模型输入以存储中目标用户消息为准）
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param use_knowledge_retrieval: 是否启用知识库检索
    :param use_web_search: 是否启用联网搜索工具（与知识库检索互斥）
    :param regenerate: 为 True 时不追加用户消息，在目标助手消息的父用户消息下生成新版本
    :param target_message_id: regenerate 时要重写的助手消息 ID；缺省为当前路径末尾助手消息
    :param cancel_check: 若返回 True 则协作停止生成（如同步读 Redis 取消标记）
    :return: 异步迭代器
    """
    attachment_ids = attachment_ids or []
    # 节流后的取消探测：chunk 级调用不再每块都打 Redis
    cancel_probe = _throttled_cancel_check(cancel_check)

    # 清空 RAG 上下文, 重置工具调用守卫
    get_last_rag_context(clear=True)
    reset_tool_call_guards()
    set_turn_tool_policy(use_knowledge_retrieval=use_knowledge_retrieval, use_web_search=use_web_search)
    set_approved_mcp_pending_id(mcp_approved_pending_id)

    # 重新生成的目标助手消息 id：显式传 target_message_id 时按它定位（MCP 续跑原地覆盖它）；
    # 未指定时回退为当前路径末尾的助手消息
    regen_target_ai_id: int | None = None
    if regenerate:
        if target_message_id:
            regen_target_ai_id = int(target_message_id)
        else:
            path_records = storage.get_session_messages(user_id, agent_id, session_id)
            target_rec = next((r for r in reversed(path_records) if r.get("type") == "ai"), None)
            if target_rec is not None:
                regen_target_ai_id = int(target_rec.get("message_id") or 0) or None

        if not regen_target_ai_id:
            yield {"type": "error", "content": "没有可重新生成的对话"}
            yield {"type": "done", "cancelled": False}
            return
        ctx = storage.get_regenerate_context(user_id, agent_id, session_id, regen_target_ai_id)
        if ctx is None:
            yield {"type": "error", "content": "无法重新生成：目标回复不存在或不属于当前会话"}
            yield {"type": "done", "cancelled": False}
            return
        messages, last_human_text, regen_path_ids = ctx
        req_text = (user_text or "").strip()
        if req_text and req_text != last_human_text.strip():
            yield {
                "type": "error",
                "content": "重新生成失败：请求文案与目标提问不一致，请刷新后重试",
            }
            yield {"type": "done", "cancelled": False}
            return
    else:
        messages = storage.load(user_id, agent_id, session_id)

    if regenerate:
        preselect_query = msg_content_to_str(messages[-1].content).strip()
    else:
        preselect_query = (user_text or "").strip()

    session_attachment_hint = format_attachment_hint(user_id, agent_id, session_id)

    kb_preselect_meta: dict[str, Any] = {}
    retrieval_filter: list[str] | None = None

    async def _prefetch_kb():
        if not use_knowledge_retrieval:
            return None, {}
        return await asyncio.to_thread(
            partial(
                _run_kb_document_preselect_with_context,
                messages,
                preselect_query,
                regenerate=regenerate,
                ua=ua,
                agent_id=agent_id,
            )
        )

    retrieval_filter, kb_preselect_meta = await _prefetch_kb()

    # 加载该智能体已启用的 MCP 工具（单服务失败仅记录，不中断对话）；
    # 共享（非属主）会话跳过加载，避免属主凭据被共享用户对话驱动。
    if _mcp_tools_allowed_for(ua, user_id):
        mcp_tools, mcp_errors = await load_agent_mcp_tools(agent_id, user_id=user_id, session_id=session_id)
    else:
        mcp_tools, mcp_errors = [], []

    agent_metrics: dict[str, Any] = {}
    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        knowledge_base_document_filter=retrieval_filter if use_knowledge_retrieval else None,
        extra_tools=mcp_tools,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        metrics_out=agent_metrics,
    )

    # 创建输出队列, 收集 RAG 步骤
    output_queue: asyncio.Queue = asyncio.Queue()
    rag_steps_collected: list[dict] = []
    thinking_items_collected: list[dict] = []

    # 创建 RAG 步骤代理, 将 RAG 步骤收集到输出队列
    class _RagStepProxy:
        def put_nowait(self, step: dict) -> None:
            rag_steps_collected.append(step)
            item = {"type": "step", **step}
            thinking_items_collected.append(item)
            output_queue.put_nowait({"type": "thinking_item", "item": item})

    # 设置 RAG 步骤队列（须在构造 to_invoke 之前，便于预注入步骤写入 rag_step）
    set_rag_step_queue(_RagStepProxy())

    # MCP 加载结果写入思考区步骤
    if mcp_tools:
        emit_rag_step("🧩", "MCP 工具已加载", f"{len(mcp_tools)} 个工具")
    for err in mcp_errors:
        emit_rag_step("⚠️", f"MCP 服务「{err['name']}」不可用", err["error"][:120])

    if not regenerate:
        human_content = build_storable_human_content(
            user_text,
            attachment_ids,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            supports_vision=bool(getattr(ua, "supports_vision", False)),
        )
        human_msg = HumanMessage(content=human_content)
        messages.append(human_msg)

        # 生成完成前即落库用户消息，刷新后仍可从历史会话看到提问（助手在结束时再写入）
        await asyncio.to_thread(storage.append_messages, user_id, agent_id, session_id, [human_msg])

    # 当前上下文各消息对应的存储行 id（与 messages 等长对齐），供压缩/记忆按 turn_key 定位
    if regenerate:
        path_ids: list[int] | None = regen_path_ids
    else:
        path_records = storage.get_session_messages(user_id, agent_id, session_id)
        path_ids = [int(r.get("message_id") or 0) for r in path_records]
        if len(path_ids) != len(messages):
            path_ids = None

    # 先 flush 已排队步骤（MCP 加载结果等），随后的两阶段读图事件直接产出，
    # 保证思考区顺序与实时性（队列事件要到生成阶段才被消费）
    while not output_queue.empty():
        yield output_queue.get_nowait()

    image_caption: str | None = None
    last_human_msg = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if last_human_msg is not None and _human_content_has_image_ref(last_human_msg.content):

        def _image_step_event(icon: str, label: str, detail: str = "") -> dict:
            step = {"icon": icon, "label": label, "detail": detail}
            rag_steps_collected.append(step)
            item = {"type": "step", **step}
            thinking_items_collected.append(item)
            return {"type": "thinking_item", "item": item}

        yield _image_step_event("🖼️", "正在理解图片", "已附加图片，等待模型分析")

        if _should_run_vision_caption(
            messages,
            ua,
            use_knowledge_retrieval=use_knowledge_retrieval,
            use_web_search=use_web_search,
            has_mcp_tools=bool(mcp_tools),
        ):
            from app.chat.vision_caption import iter_image_caption_chunks

            caption_parts: list[str] = []
            caption_ok = False
            try:
                async for text in iter_image_caption_chunks(
                    ua,
                    last_human_msg,
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                ):
                    if cancel_probe is not None and await cancel_probe():
                        get_last_rag_context(clear=True)
                        get_pending_mcp_confirmations(clear=True)
                        set_rag_step_queue(None)
                        yield {"type": "cancelled"}
                        yield {"type": "done", "cancelled": True}
                        return
                    caption_parts.append(text)
                    if thinking_items_collected and thinking_items_collected[-1].get("type") == "text":
                        thinking_items_collected[-1]["text"] += text
                    else:
                        thinking_items_collected.append({"type": "text", "text": text})
                    yield {
                        "type": "thinking_item",
                        "item": {"type": "text", "text": text},
                        "append": True,
                    }
                caption_ok = True
            except Exception:
                caption_ok = False
            caption = "".join(caption_parts).strip()
            if caption_ok and caption:
                image_caption = caption
                yield _image_step_event("🖼️", "图片理解完成", f"已预读图片内容（{len(caption)} 字）")
            else:
                yield _image_step_event("⚠️", "图片理解失败", "改为直接带图问答")

    budget_info: dict[str, Any] = {}
    # 压缩（含 micro-compact 前置清理）在 _prepare 内同步执行，期间会 emit_rag_step
    # （如「正在进行上下文压缩」）。放进 task 边等边把队列事件实时 yield，
    # 避免压缩阻塞首 token 时前端白屏。
    prep_task = asyncio.create_task(
        asyncio.to_thread(
            _prepare_to_invoke_messages,
            messages,
            ua,
            user_id,
            agent_id,
            session_id,
            use_knowledge_retrieval=use_knowledge_retrieval,
            use_web_search=use_web_search,
            document_filter=retrieval_filter if use_knowledge_retrieval else None,
            session_attachment_hint=session_attachment_hint,
            mcp_approval_note=_mcp_approval_note(
                mcp_approved_pending_id, user_id=user_id, agent_id=agent_id, session_id=session_id
            ),
            path_ids=path_ids,
            image_caption=image_caption,
            tools_tokens=int(agent_metrics.get("tools_tokens") or 0),
            budget_out=budget_info,
        )
    )
    try:
        while not prep_task.done():
            if cancel_probe is not None and await cancel_probe():
                prep_task.cancel()
                get_last_rag_context(clear=True)
                get_pending_mcp_confirmations(clear=True)
                set_rag_step_queue(None)
                yield {"type": "cancelled"}
                yield {"type": "done", "cancelled": True}
                return
            try:
                event = await asyncio.wait_for(output_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if event is not None:
                yield event
        to_invoke = await prep_task
    except (asyncio.CancelledError, GeneratorExit):
        prep_task.cancel()
        raise
    # flush 准备阶段剩余步骤（含压缩完成提示「📦 会话压缩」）
    while not output_queue.empty():
        event = output_queue.get_nowait()
        if event is not None:
            yield event

    # 初始化响应内容
    full_response = ""
    thinking_text_parts: list[str] = []
    stream_error: str | None = None
    cancelled_externally = False
    usage_meta: Any = None

    # 预流式阶段（KB 预选 / MCP 加载 / 记忆准备）完成后、启动生成前检查一次取消标记，
    # 使用户在阻塞阶段点击停止也能即时生效（此时用户消息已落库，与流式中取消行为一致）
    if cancel_probe is not None and await cancel_probe():
        get_last_rag_context(clear=True)
        get_pending_mcp_confirmations(clear=True)
        set_rag_step_queue(None)
        yield {"type": "cancelled"}
        yield {"type": "done", "cancelled": True}
        return

    # 创建异步任务, 调用智能体
    async def _agent_worker() -> None:
        nonlocal full_response, stream_error, cancelled_externally, usage_meta
        # 当前 AI 消息的流式分类状态：工具调用前的过渡文本需迁移到思考区
        current_msg_id: str | None = None
        msg_text_emitted = ""  # 当前消息已按 content 发出的文本
        msg_moved = False  # 当前消息已被判定为工具调用，剩余文本直发 thinking_text
        try:
            # 异步流式调用智能体
            async for msg, _metadata in agent.astream(
                {"messages": to_invoke},
                stream_mode="messages",
                config=_agent_invoke_config(),
            ):
                if cancel_probe is not None and await cancel_probe():
                    cancelled_externally = True
                    break
                if not isinstance(msg, AIMessageChunk):
                    continue

                # stream_usage=True 时用量随最后一个 chunk 到达；取最大 input_tokens 的那次
                chunk_usage = getattr(msg, "usage_metadata", None)
                if chunk_usage:
                    from app.chat.context_budget import extract_input_tokens

                    if extract_input_tokens(chunk_usage) >= extract_input_tokens(usage_meta):
                        usage_meta = chunk_usage

                content = ""
                if isinstance(msg.content, str):
                    content = msg.content
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, str):
                            content += block
                        elif isinstance(block, dict) and block.get("type") == "text":
                            content += block.get("text", "")

                has_tool_call = bool(getattr(msg, "tool_call_chunks", None))
                if msg.id is not None and msg.id != current_msg_id:
                    current_msg_id = msg.id
                    msg_text_emitted = ""
                    msg_moved = False

                if has_tool_call:
                    if msg_text_emitted:
                        # 本消息此前作为正文流出的文本实为工具调用前导句, 移交前端迁入思考区
                        thinking_text_parts.append(msg_text_emitted)
                        move_item = {"type": "text", "text": msg_text_emitted}
                        thinking_items_collected.append(move_item)
                        if full_response.endswith(msg_text_emitted):
                            full_response = full_response[: -len(msg_text_emitted)]
                        else:
                            pos = full_response.rfind(msg_text_emitted)
                            full_response = full_response[:pos] if pos >= 0 else full_response
                        await output_queue.put(
                            {"type": "thinking_item", "item": move_item, "moved_from_content": True}
                        )
                        msg_text_emitted = ""
                    if msg.id is not None:
                        msg_moved = True

                if content:
                    if msg_moved:
                        thinking_text_parts.append(content)
                        if thinking_items_collected and thinking_items_collected[-1].get("type") == "text":
                            thinking_items_collected[-1]["text"] += content
                        else:
                            thinking_items_collected.append({"type": "text", "text": content})
                        await output_queue.put(
                            {
                                "type": "thinking_item",
                                "item": {"type": "text", "text": content},
                                "append": True,
                            }
                        )
                    else:
                        full_response += content
                        if msg.id is not None:
                            msg_text_emitted += content
                        await output_queue.put({"type": "content", "content": content})
        except asyncio.CancelledError:
            cancelled_externally = True
            raise
        except Exception as e:
            stream_error = str(e)
            await output_queue.put({"type": "error", "content": stream_error})
        finally:
            await output_queue.put(None)

    # 创建异步任务, 调用智能体
    agent_task = asyncio.create_task(_agent_worker())

    # 取消看门狗：周期轮询取消标记，命中即中断生成任务。
    # 覆盖工具执行等无 chunk 流出的阶段（chunk 循环内的取消检查在这些阶段不会被触发）
    async def _cancel_watchdog() -> None:
        if cancel_probe is None:
            return
        while not agent_task.done():
            if await cancel_probe():
                agent_task.cancel()
                return
            await asyncio.sleep(0.3)

    watchdog_task = asyncio.create_task(_cancel_watchdog())

    try:
        while True:
            event = await output_queue.get()
            if event is None:
                break
            yield event
    except GeneratorExit:
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass
        raise
    finally:
        set_rag_step_queue(None)
        watchdog_task.cancel()
        if not agent_task.done():
            agent_task.cancel()

    if cancelled_externally:
        get_last_rag_context(clear=True)
        get_pending_mcp_confirmations(clear=True)
        yield {"type": "cancelled"}
        yield {"type": "done", "cancelled": True}
        return

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None
    pending_mcp = get_pending_mcp_confirmations(clear=True)
    # 获取图片引用
    image_references = rag_context.get("image_references") if rag_context else None
    # 获取来源列表（知识库 / 联网搜索互斥，合并走统一 sources 通道）
    kb_sources = rag_context.get("kb_sources") if rag_context else None
    web_sources = rag_context.get("web_sources") if rag_context else None
    merged_sources = kb_sources or web_sources

    # 如果 RAG 追踪存在, 则输出 RAG 追踪
    if rag_trace:
        yield {"type": "trace", "rag_trace": rag_trace}

    # 输出来源列表（供前端渲染「来源」）
    if merged_sources:
        yield {"type": "sources", "sources": merged_sources}

    for pending in pending_mcp:
        yield {"type": "mcp_confirmation_required", "confirmation": pending}

    ai_msg = AIMessage(content=full_response)
    messages.append(ai_msg)
    ai_extra = {
        "rag_trace": rag_trace,
        "rag_steps": rag_steps_collected or None,
        "error_text": stream_error,
        "image_references": image_references,
        "sources": merged_sources,
        "kb_preselect": kb_preselect_meta or None,
        "thinking_text": "".join(thinking_text_parts) or None,
        "thinking_items": thinking_items_collected or None,
    }
    if regenerate:
        # MCP 确认续跑原地覆盖目标助手消息；普通重生成插入兄弟版本并选中新版本
        saved = False
        if regen_target_ai_id is not None:
            if mcp_approved_pending_id:
                saved = storage.update_assistant_in_place(
                    user_id, agent_id, session_id, regen_target_ai_id, ai_msg, extra=ai_extra
                )
            else:
                saved = storage.insert_assistant_version(
                    user_id, agent_id, session_id, regen_target_ai_id, ai_msg, extra=ai_extra
                )
        if not saved:
            yield {"type": "error", "content": "重新生成失败：目标回复已被移除，请刷新后重试"}
            yield {"type": "done", "cancelled": False}
            return
    else:
        storage.append_messages(
            user_id, agent_id, session_id, [ai_msg], extra_message_data=[ai_extra]
        )

    # 落库后收尾：用真实 usage 同步校准 token 估算系数（长期记忆由摘要调用顺带写入）
    await asyncio.to_thread(
        _calibrate_token_estimate,
        user_id,
        agent_id,
        session_id,
        usage=usage_meta,
        estimated=int(budget_info.get("estimated") or 0),
        model_name=str(budget_info.get("model_name") or ua.model_name or ""),
    )

    yield {"type": "done", "cancelled": False}


async def chat_with_agent_stream(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
    use_web_search: bool = False,
    attachment_ids: list[str] | None = None,
    regenerate: bool = False,
    target_message_id: int | None = None,
    mcp_approved_pending_id: str | None = None,
) -> AsyncIterator[str]:
    """
    异步对话（SSE 字符串片段）
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param use_knowledge_retrieval: 是否启用知识库检索
    :param use_web_search: 是否启用联网搜索工具（与知识库检索互斥）
    :param regenerate: 是否重新生成助手回复
    :param target_message_id: regenerate 时要重写的助手消息 ID；缺省为当前路径末尾助手消息
    :return: 异步迭代器
    """
    async for ev in iter_chat_stream_events(
        ua,
        user_text,
        user_id,
        agent_id,
        session_id,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        attachment_ids=attachment_ids,
        regenerate=regenerate,
        target_message_id=target_message_id,
        mcp_approved_pending_id=mcp_approved_pending_id,
    ):
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
