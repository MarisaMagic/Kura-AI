"""
LangChain Agent 对话（同步 invoke + 异步 SSE 流式）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from functools import partial
from typing import Any, AsyncIterator

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.chat.attachment_service import build_storable_human_content, format_attachment_hint
from app.chat.attachment_tools import make_session_attachment_tools
from app.chat.memory_archive import schedule_archive_session_memory
from app.chat.memory_tool import make_search_session_memory_tool
from app.chat.memory_turns import apply_sliding_window_turns
from app.chat.message_codec import expand_messages_for_model, msg_content_to_str
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
from app.chat.web_search_tool import make_fetch_url_tool, make_web_search_tool
from app.kb.image_search_tool import make_search_knowledge_by_image_tool
from app.kb.kb_scope import kb_scope_for
from app.kb.search_tool import make_search_knowledge_tool
from app.mcp_client.service import load_agent_mcp_tools
from app.models.user_agent import UserAgent
from app.settings import settings
from app.utils.api_key_crypto import decrypt_api_key_safe


def _format_one_message_for_debug(msg: BaseMessage) -> str:
    """
    格式化一条消息，用于调试
    :param msg: 消息
    :return: 字符串
    """
    if isinstance(msg, SystemMessage):
        return f"[System]\n{msg_content_to_str(msg.content)}"
    if isinstance(msg, HumanMessage):
        return f"[Human]\n{msg_content_to_str(msg.content)}"
    if isinstance(msg, AIMessage):
        blocks = ["[AI]"]
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            blocks.append(f"tool_calls: {json.dumps(tool_calls, ensure_ascii=False)}")
        body = msg_content_to_str(getattr(msg, "content", ""))
        if body:
            blocks.append(body)
        return "\n".join(blocks)
    if isinstance(msg, ToolMessage):
        name = getattr(msg, "name", "") or ""
        return f"[Tool:{name}]\n{msg_content_to_str(msg.content)}"
    return f"[{type(msg).__name__}]\n{msg_content_to_str(getattr(msg, 'content', ''))}"


class _KbPromptDebugCallback(BaseCallbackHandler):
    """在每次 LLM 调用前打印完整输入消息列表（受 DEBUG_AGENT_KB_PROMPT 控制）。"""

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs: Any,
    ) -> None:
        from app.settings import settings

        if not getattr(settings, "DEBUG_AGENT_KB_PROMPT", False):
            return
        for batch in messages:
            if not batch:
                continue
            sep = "=" * 72
            lines: list[str] = []
            for i, m in enumerate(batch):
                lines.append(f"--- message[{i}] ---")
                lines.append(_format_one_message_for_debug(m))
            out = "\n".join(lines)
            print(
                f"\n{sep}\n[智能体 LLM] 本轮发给模型的完整消息列表:\n{sep}\n{out}\n{sep}\n",
                flush=True,
            )


def _agent_invoke_config() -> dict[str, Any]:
    return {"recursion_limit": 30, "callbacks": [_KbPromptDebugCallback()]}


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


_WEB_SEARCH_DISCIPLINE = (
    "联网搜索作答纪律：回答必须仅依据 web_search / fetch_url 工具的返回内容与多轮对话上下文；"
    "用户给出 http(s) 链接时优先调用 fetch_url；搜到结果后若需精读某一页也用 fetch_url。"
    "凡引用搜索或读页内容，必须以 [来源N] 标注（N 与工具返回中的编号一致），并保证引用的 URL 与工具返回逐字一致。"
    "当工具返回明确提示失败或无结果（TOOL_CALL_LIMIT_REACHED / WEB_SEARCH_NO_RESULTS / FETCH_URL_FAILED / 联网搜索出错）"
    "或本轮禁用（TOOL_DISABLED_THIS_TURN）时，"
    "必须如实告知用户「联网搜索未找到相关内容」或「未能打开该链接」并可建议换个问法重试，"
    "不得编造搜索结果、页面正文、实时数据或来源链接；"
    "注意区分搜索结论与你的一般常识推断，后者不得冒充联网检索结果。"
)

_KB_IMAGE_DISCIPLINE = (
    "知识库检索结果中，每个图片 chunk 都会单独给出一行现成的 Markdown：`![说明](/api/v1/media/...?exp=...&sig=...)`。"
    "展示图片时必须把那一行原样复制到回答中，括号内必须是以 /api/v1/media/ 开头并带 ?exp=&sig= 的地址。"
    "禁止自行改写或拼接括号内内容：不得填入文档名、页码、`[i] ... (Page n)` 标题、stored_relpath、"
    "不得改成 http(s) 绝对地址、image://、file://、kb_image://，也不得用 [1][2] 或序号代替。"
)

_KB_ANSWER_DISCIPLINE = (
    "知识库作答纪律：回答必须仅依据知识库检索工具的返回内容与多轮对话上下文；"
    "凡引用检索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致）。"
    "当工具返回明确提示知识库无相关资料（或检索未命中）或本轮禁用（TOOL_DISABLED_THIS_TURN）时，"
    "必须如实告知用户「知识库中未找到相关资料」"
    "并说明可补充资料后重试，不得编造知识库结论或凭想象作答；"
    "若无确凿资料支撑，宁可说明「知识库中未找到相关资料」，也不要虚构。"
    "注意区分知识库中的结论与你的一般常识推断，后者不得冒充知识库内容。"
)


def _format_kb_scope_for_turn(document_filter: list[str] | None) -> str:
    """本回合选档范围（写入最后一条 Human，不进 system）。"""
    if document_filter is None:
        return (
            "【本回合知识库检索范围】未限定在单批 file_key（将按当前智能体整库检索；无文档时无结果）。"
            "调用 search_knowledge_base 时只需传入 query。"
        )
    lines = "\n".join(f"- `{x}`" for x in document_filter)
    return (
        "【本回合知识库检索范围】已在回复前由系统根据用户问题将检索**限定在以下文档**（file_key）。\n"
        f"{lines}\n"
        "调用 search_knowledge_base 时只传 query，不要尝试指定或更换 file_key / 范围。"
    )


def _format_turn_context_block(
    *,
    use_knowledge_retrieval: bool,
    use_web_search: bool,
    document_filter: list[str] | None,
    session_attachment_hint: str,
    memory_inject: str | None,
    mcp_approval_note: str | None,
) -> str:
    """拼装仅本回合有效、挂在最后一条 Human 末尾的上下文（不落库）。"""
    parts: list[str] = ["【本轮上下文（仅本回合有效）】"]
    if use_web_search:
        parts.append(
            "本轮允许调用 web_search / fetch_url；不要调用 search_knowledge_base / search_knowledge_by_image。"
        )
    elif use_knowledge_retrieval:
        parts.append(
            "本轮允许调用 search_knowledge_base / search_knowledge_by_image；不要调用 web_search / fetch_url。"
        )
    else:
        parts.append(
            "本轮不要调用 web_search、fetch_url、search_knowledge_base、search_knowledge_by_image。"
        )
    if use_knowledge_retrieval:
        parts.append(_format_kb_scope_for_turn(document_filter))
    hint = (session_attachment_hint or "").strip()
    if hint:
        parts.append(hint)
    if (memory_inject or "").strip():
        parts.append(
            "【本回合根据用户最新输入自动检索的较早会话摘录（仅供参考；"
            "若仍不足可再调用 search_session_memory 工具）】\n\n"
            + memory_inject.strip()
        )
    if (mcp_approval_note or "").strip():
        parts.append(mcp_approval_note.strip())
    return "\n\n".join(parts)


def _append_text_to_last_human(messages: list, block: str) -> list:
    """把文本接到最后一条 Human（字符串拼接或多模态 text 块）。无 Human 则原样返回。"""
    text = (block or "").strip()
    if not text:
        return list(messages)
    last_i = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_i = i
            break
    if last_i is None:
        return list(messages)
    content = messages[last_i].content
    if isinstance(content, str):
        new_content: Any = content + "\n\n" + text
    elif isinstance(content, list):
        new_content = list(content) + [{"type": "text", "text": text}]
    else:
        new_content = str(content) + "\n\n" + text
    out = list(messages)
    out[last_i] = HumanMessage(content=new_content)
    return out


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
    user_query_for_memory: str,
    *,
    use_knowledge_retrieval: bool,
    use_web_search: bool,
    document_filter: list[str] | None,
    session_attachment_hint: str,
    mcp_approval_note: str | None = None,
) -> list:
    """
    滑动窗口 → 可选会话记忆预检索 → 展开多模态 → 本轮上下文接到最后一条 Human。
    """
    from app.chat.memory_search import proactive_session_memory_inject_text
    from app.chat.tools import emit_rag_step

    windowed = apply_sliding_window_turns(messages)
    llm_cfg = _sub_llm_config_from_ua(ua)
    inj = proactive_session_memory_inject_text(
        (user_query_for_memory or "").strip(),
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        llm_config=llm_cfg,
    )
    if inj:
        emit_rag_step("📌", "会话记忆预注入", "已附加较早轮次摘录")
    expanded = expand_messages_for_model(
        windowed,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    turn_block = _format_turn_context_block(
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        document_filter=document_filter,
        session_attachment_hint=session_attachment_hint,
        memory_inject=inj,
        mcp_approval_note=mcp_approval_note,
    )
    return _append_text_to_last_human(expanded, turn_block)


def _compose_system_prompt(ua: UserAgent) -> str:
    """
    稳定系统提示：人设 + 联网/知识库纪律（跨轮不变；本轮开关写在最后一条 Human）。
    """
    parts: list[str] = []
    base = (ua.system_prompt or "").strip()
    if base:
        parts.append(base)
    else:
        parts.append("You are a helpful assistant.")
    parts.append(_WEB_SEARCH_DISCIPLINE)
    parts.append(_KB_IMAGE_DISCIPLINE)
    parts.append(_KB_ANSWER_DISCIPLINE)
    return "\n\n".join(parts)


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
) -> tuple[Any, Any]:
    """
    构建模型和智能体。检索工具始终挂载（本轮禁用由工具函数返回 TOOL_DISABLED_THIS_TURN）。
    知识库检索按属主隔离：使用他人已发布智能体时检索发布者的知识库。
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
    kb_scope = kb_scope_for(ua.user_id, ua.id)
    llm_config = _sub_llm_config_from_ua(ua)
    tools: list[Any] = []
    tools.extend(make_session_attachment_tools(user_id, agent_id, session_id))
    if getattr(settings, "WEB_SEARCH_ENABLED", True):
        tools.append(make_web_search_tool())
        tools.append(make_fetch_url_tool())
    tools.append(
        make_search_knowledge_tool(
            kb_scope,
            llm_config,
            knowledge_base_document_filter=knowledge_base_document_filter,
        )
    )
    tools.append(make_search_knowledge_by_image_tool(kb_scope, user_id, agent_id, session_id))
    if getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        tools.append(make_search_session_memory_tool(user_id, agent_id, session_id, llm_config))
    if extra_tools:
        tools.extend(extra_tools)
    tools = _sort_tools(tools)
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=_compose_system_prompt(ua),
    )
    return agent, model


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
    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        knowledge_base_document_filter=retrieval_filter if use_knowledge_retrieval else None,
        extra_tools=mcp_tools,
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

    to_invoke = _prepare_to_invoke_messages(
        messages,
        ua,
        user_id,
        agent_id,
        session_id,
        (user_text or "").strip(),
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        document_filter=retrieval_filter if use_knowledge_retrieval else None,
        session_attachment_hint=session_attachment_hint,
        mcp_approval_note=_mcp_approval_note(
            mcp_approved_pending_id, user_id=user_id, agent_id=agent_id, session_id=session_id
        ),
    )
    caught_exc: Exception | None = None
    response_content = ""
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
                "thinking_items": [{"type": "step", **s} for s in rag_collector.steps] or None,
            }
        ],
    )

    # 归档会话记忆，按配置同步或后台线程归档。
    schedule_archive_session_memory(user_id, agent_id, session_id) 

    if caught_exc:
        raise caught_exc

    return {
        "response": response_content,
        "rag_trace": rag_trace,
        "sources": merged_sources,
        "kb_preselect": kb_preselect_meta or None,
        "pending_mcp_confirmations": pending_mcp or None,
    }


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
    cancel_check: Callable[[], bool] | None = None,
    mcp_approved_pending_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    异步对话：产出与 SSE 中 `data: {...}` 相同结构的 dict 事件（供直连 SSE 与后台 Job 复用）。
    最后依次产出 trace（若有）、写入存储后产出 done。
    :param ua: 智能体
    :param user_text: 用户文本（regenerate 时仅作校验用，模型输入以存储中最后一条用户消息为准）
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param use_knowledge_retrieval: 是否启用知识库检索
    :param use_web_search: 是否启用联网搜索工具（与知识库检索互斥）
    :param regenerate: 为 True 时不追加用户消息，移除末尾助手消息后基于当前历史重答
    :param cancel_check: 若返回 True 则协作停止生成（如同步读 Redis 取消标记）
    :return: 异步迭代器
    """
    attachment_ids = attachment_ids or []
    messages = storage.load(user_id, agent_id, session_id)

    # 清空 RAG 上下文, 重置工具调用守卫
    get_last_rag_context(clear=True)
    reset_tool_call_guards()
    set_turn_tool_policy(use_knowledge_retrieval=use_knowledge_retrieval, use_web_search=use_web_search)
    set_approved_mcp_pending_id(mcp_approved_pending_id)

    if regenerate:
        if not messages:
            yield {"type": "error", "content": "没有可重新生成的对话"}
            yield {"type": "done", "cancelled": False}
            return
        while messages and isinstance(messages[-1], AIMessage):
            messages.pop()
        if not messages or not isinstance(messages[-1], HumanMessage):
            yield {"type": "error", "content": "无法重新生成：没有可配对的用户消息"}
            yield {"type": "done", "cancelled": False}
            return
        last_human_text = msg_content_to_str(messages[-1].content).strip()
        req_text = (user_text or "").strip()
        if req_text and req_text != last_human_text:
            yield {
                "type": "error",
                "content": "重新生成失败：请求文案与当前最后一条用户消息不一致，请刷新后重试",
            }
            yield {"type": "done", "cancelled": False}
            return

    if regenerate:
        preselect_query = msg_content_to_str(messages[-1].content).strip()
    else:
        preselect_query = (user_text or "").strip()

    session_attachment_hint = format_attachment_hint(user_id, agent_id, session_id)

    kb_preselect_meta: dict[str, Any] = {}
    retrieval_filter: list[str] | None = None
    if use_knowledge_retrieval:
        retrieval_filter, kb_preselect_meta = await asyncio.to_thread(
            partial(
                _run_kb_document_preselect_with_context,
                messages,
                preselect_query,
                regenerate=regenerate,
                ua=ua,
                agent_id=agent_id,
            )
        )

    # 加载该智能体已启用的 MCP 工具（单服务失败仅记录，不中断对话）；
    # 共享（非属主）会话跳过加载，避免属主凭据被共享用户对话驱动。
    if _mcp_tools_allowed_for(ua, user_id):
        mcp_tools, mcp_errors = await load_agent_mcp_tools(agent_id, user_id=user_id, session_id=session_id)
    else:
        mcp_tools, mcp_errors = [], []

    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        knowledge_base_document_filter=retrieval_filter if use_knowledge_retrieval else None,
        extra_tools=mcp_tools,
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

    memory_query = (user_text or "").strip()
    if regenerate:
        memory_query = msg_content_to_str(messages[-1].content).strip()

    to_invoke = await asyncio.to_thread(
        _prepare_to_invoke_messages,
        messages,
        ua,
        user_id,
        agent_id,
        session_id,
        memory_query,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        document_filter=retrieval_filter if use_knowledge_retrieval else None,
        session_attachment_hint=session_attachment_hint,
        mcp_approval_note=_mcp_approval_note(
            mcp_approved_pending_id, user_id=user_id, agent_id=agent_id, session_id=session_id
        ),
    )

    # 初始化响应内容
    full_response = ""
    thinking_text_parts: list[str] = []
    stream_error: str | None = None
    cancelled_externally = False

    # 预流式阶段（KB 预选 / MCP 加载 / 记忆准备）完成后、启动生成前检查一次取消标记，
    # 使用户在阻塞阶段点击停止也能即时生效（此时用户消息已落库，与流式中取消行为一致）
    if cancel_check and await asyncio.to_thread(cancel_check):
        get_last_rag_context(clear=True)
        get_pending_mcp_confirmations(clear=True)
        set_rag_step_queue(None)
        yield {"type": "cancelled"}
        yield {"type": "done", "cancelled": True}
        return

    # 创建异步任务, 调用智能体
    async def _agent_worker() -> None:
        nonlocal full_response, stream_error, cancelled_externally
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
                if cancel_check and await asyncio.to_thread(cancel_check):
                    cancelled_externally = True
                    break
                if not isinstance(msg, AIMessageChunk):
                    continue

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
        if not cancel_check:
            return
        while not agent_task.done():
            if await asyncio.to_thread(cancel_check):
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
        storage.replace_trailing_assistant(user_id, agent_id, session_id, ai_msg, extra=ai_extra)
    else:
        storage.append_messages(
            user_id, agent_id, session_id, [ai_msg], extra_message_data=[ai_extra]
        )

    # 归档会话记忆，按配置同步或后台线程归档。
    schedule_archive_session_memory(user_id, agent_id, session_id) 

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
    :param regenerate: 是否重新生成最后一轮助手回复
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
        mcp_approved_pending_id=mcp_approved_pending_id,
    ):
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
