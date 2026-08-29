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
    reset_tool_call_guards,
    set_rag_step_queue,
)
from app.chat.web_search_tool import make_web_search_tool
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
        _llm_config_from_ua(ua),
        conversation_context=ctx or None,
    )
    return filt, {**ctx_meta, **pre_meta}


def _merge_proactive_memory_system(windowed: list[BaseMessage], inject_body: str) -> list[BaseMessage]:
    """
    合并预检索的会话记忆到系统提示词
    :param windowed: 消息列表
    :param inject_body: 预检索的会话记忆
    :return: 消息列表
    """
    i = 0
    while i < len(windowed) and isinstance(windowed[i], SystemMessage):
        i += 1
    block = SystemMessage(
        content=(
            "【本回合根据用户最新输入自动检索的较早会话摘录（仅供参考；"
            "若仍不足可再调用 search_session_memory 工具）】\n\n"
            + inject_body
        )
    )
    return [*windowed[:i], block, *windowed[i:]]


def _prepare_to_invoke_messages(
    messages: list[BaseMessage],
    ua: UserAgent,
    user_id: int,
    agent_id: int,
    session_id: str,
    user_query_for_memory: str,
) -> list:
    """
    滑动窗口 → 可选会话记忆预注入 → 展开多模态。
    :param messages: 消息列表
    :param ua: 智能体
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param user_query_for_memory: 用户查询
    :return: 展开后的消息列表
    """
    from app.chat.memory_search import proactive_session_memory_inject_text
    from app.chat.tools import emit_rag_step

    windowed = apply_sliding_window_turns(messages)
    llm_cfg = _llm_config_from_ua(ua)
    inj = proactive_session_memory_inject_text( # 预检索会话记忆
        (user_query_for_memory or "").strip(),
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        llm_config=llm_cfg,
    )
    if inj:
        emit_rag_step("📌", "会话记忆预注入", "已附加较早轮次摘录")
        windowed = _merge_proactive_memory_system(windowed, inj)
    return expand_messages_for_model(
        windowed,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )


def _format_kb_system_extension(document_filter: list[str] | None) -> str:
    """
    将前置选档结果写入系统提示，使智能体知悉本回合 search_knowledge_base 的文档范围由系统固定。
    document_filter 为 None 表示全知识库（或未加 filename 子句）；非空为限定 file_key 列表。
    """
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


def _compose_system_prompt(
    ua: UserAgent,
    use_knowledge_retrieval: bool = True,
    *,
    use_web_search: bool = False,
    session_attachment_hint: str = "",
    kb_retrieval_system_extension: str | None = None,
) -> str:
    """
    组合智能体的系统提示词（工具如何调用由各工具的 description 说明，此处不重复指令）。
    :param ua: 智能体
    :param use_knowledge_retrieval: 保留参数以兼容调用方；不在此写入知识库工具说明
    :param use_web_search: 为 True 时写入联网搜索作答纪律
    :param session_attachment_hint: 本会话已上传附件的纯事实列表（无工具调用指引）
    :return: 系统提示词
    """
    parts: list[str] = []
    # 基础提示词, 用户在创建智能体时配置的前置提示词
    base = (ua.system_prompt or "").strip()
    if base:
        parts.append(base)
    else:
        parts.append("You are a helpful assistant.")
    # 联网搜索作答纪律（工具为 web_search，DuckDuckGo 实时结果）
    if use_web_search:
        parts.append(
            "联网搜索作答纪律：回答必须仅依据 web_search 工具的返回内容与多轮对话上下文；"
            "凡引用搜索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致），并保证引用的 URL 与工具返回逐字一致。"
            "当工具返回明确提示搜索失败或无结果（TOOL_CALL_LIMIT_REACHED / WEB_SEARCH_NO_RESULTS / 联网搜索出错）时，"
            "必须如实告知用户「联网搜索未找到相关内容」并可建议换个问法重试，"
            "不得编造搜索结果、实时数据或来源链接；"
            "注意区分搜索结论与你的一般常识推断，后者不得冒充联网检索结果。"
        )
    if (session_attachment_hint or "").strip():
        parts.append(session_attachment_hint.strip())
    if use_knowledge_retrieval and (kb_retrieval_system_extension or "").strip():
        parts.append(kb_retrieval_system_extension.strip())
    if use_knowledge_retrieval:
        parts.append(
            "知识库检索结果中若出现「图片公网访问 URL」或「PostgreSQL 存储相对路径 stored_relpath」，"
            "展示图片时必须在回答中使用工具给出的完整 http(s) 图片 URL（Markdown：![](完整URL)），"
            "须与工具返回的「图片公网访问 URL」逐字一致。"
            "禁止使用 image://、file://、kb_image:// 等自定义协议，禁止用 [1][2] 或序号代替 URL。"
        )
        parts.append(
            "知识库作答纪律：回答必须仅依据知识库检索工具的返回内容与多轮对话上下文；"
            "凡引用检索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致）。"
            "当工具返回明确提示知识库无相关资料（或检索未命中）时，必须如实告知用户「知识库中未找到相关资料」"
            "并说明可补充资料后重试，不得编造知识库结论或凭想象作答；"
            "若无确凿资料支撑，宁可说明「知识库中未找到相关资料」，也不要虚构。"
            "注意区分知识库中的结论与你的一般常识推断，后者不得冒充知识库内容。"
        )
    return "\n\n".join(parts)


def build_model_and_agent(
    ua: UserAgent,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
    use_web_search: bool = False,
    session_attachment_hint: str = "",
    knowledge_base_document_filter: list[str] | None = None,
    kb_retrieval_system_extension: str | None = None,
    extra_tools: list[Any] | None = None,
) -> tuple[Any, Any]:
    """
    构建模型和智能体（知识库检索按属主隔离：使用他人已发布智能体时检索发布者的知识库）。
    :param ua: 智能体
    :param user_id: 聊天者用户 ID（会话附件/会话记忆按聊天者隔离；知识库按属主隔离）
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID（会话附件工具作用域）
    :param use_knowledge_retrieval: 是否注册知识库检索工具
    :param use_web_search: 是否注册联网搜索工具（与知识库检索互斥，由请求层校验）
    :param session_attachment_hint: 本会话附件列表说明
    :param knowledge_base_document_filter: 由入口前置选档得到；None=全库，非空=仅这些 filename
    :param kb_retrieval_system_extension: 与选档结果对应的系统补充说明（file_key 列表等）
    :param extra_tools: 外部预加载的附加工具（如 MCP 服务工具）
    :return: 模型和智能体
    """
    # 解密 API Key
    plain = decrypt_api_key_safe(ua.api_key_ciphertext)
    if not plain or not plain.strip():
        raise ValueError("智能体未配置有效的 API Key")

    # 获取基础 URL, 用户在创建智能体时配置的 OpenAI 兼容 API Base URL
    base_url = (ua.base_url or "").strip() or None
    if base_url:
        from app.utils.ssrf import assert_public_http_url

        assert_public_http_url(base_url)
    # 构建大模型, 使用 OpenAI 兼容 API Base URL
    model = init_chat_model(
        model=ua.model_name,
        model_provider="openai",
        api_key=plain.strip(),
        base_url=base_url,
        temperature=float(ua.temperature),
        stream_usage=True,
    )
    kb_scope = kb_scope_for(ua.user_id, ua.id)
    llm_config = {
        "api_key": plain.strip(),
        "base_url": base_url,
        "model_name": ua.model_name,
    }
    tools: list[Any] = []
    tools.extend(make_session_attachment_tools(user_id, agent_id, session_id))
    if use_web_search and getattr(settings, "WEB_SEARCH_ENABLED", True):
        tools.append(make_web_search_tool())  # 联网搜索（DuckDuckGo）
    if use_knowledge_retrieval:
        tools.append(
            make_search_knowledge_tool(
                kb_scope,
                llm_config,
                knowledge_base_document_filter=knowledge_base_document_filter,
            )
        )  # 以文检索（范围由闭包固定）
        tools.append(
            make_search_knowledge_by_image_tool(kb_scope, user_id, agent_id, session_id)
        )  # 以图检索
    if getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        tools.append(make_search_session_memory_tool(user_id, agent_id, session_id, llm_config)) # 添加会话记忆检索工具
    if extra_tools:
        tools.extend(extra_tools)  # MCP 服务等外部预加载工具
    # create_agent 创建智能体, 使用模型和系统提示词
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=_compose_system_prompt(
            ua,
            use_knowledge_retrieval=use_knowledge_retrieval,
            use_web_search=use_web_search,
            session_attachment_hint=session_attachment_hint,
            kb_retrieval_system_extension=kb_retrieval_system_extension,
        ),
    )
    # 返回智能体和大模型
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
    kb_ext = _format_kb_system_extension(retrieval_filter) if use_knowledge_retrieval else None

    # 加载该智能体已启用的 MCP 工具（本函数在无线事件循环的线程中运行，asyncio.run 安全）；
    # MCP 工具为 async-only，包装为同步调用；单服务失败仅记录到 rag_steps，不中断对话。
    mcp_tools: list[Any] = []
    mcp_errors: list[dict] = []
    try:
        raw_mcp_tools, mcp_errors = asyncio.run(load_agent_mcp_tools(agent_id))
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
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        session_attachment_hint=session_attachment_hint,
        knowledge_base_document_filter=retrieval_filter if use_knowledge_retrieval else None,
        kb_retrieval_system_extension=kb_ext if use_knowledge_retrieval else None,
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
    kb_ext = _format_kb_system_extension(retrieval_filter) if use_knowledge_retrieval else None

    # 加载该智能体已启用的 MCP 工具（单服务失败仅记录，不中断对话）
    mcp_tools, mcp_errors = await load_agent_mcp_tools(agent_id)

    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        use_knowledge_retrieval=use_knowledge_retrieval,
        use_web_search=use_web_search,
        session_attachment_hint=session_attachment_hint,
        knowledge_base_document_filter=retrieval_filter if use_knowledge_retrieval else None,
        kb_retrieval_system_extension=kb_ext if use_knowledge_retrieval else None,
        extra_tools=mcp_tools,
    )

    # 创建输出队列, 收集 RAG 步骤
    output_queue: asyncio.Queue = asyncio.Queue()
    rag_steps_collected: list[dict] = []

    # 创建 RAG 步骤代理, 将 RAG 步骤收集到输出队列
    class _RagStepProxy:
        def put_nowait(self, step: dict) -> None:
            rag_steps_collected.append(step)
            output_queue.put_nowait({"type": "rag_step", "step": step})

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
    )

    # 初始化响应内容
    full_response = ""
    thinking_text_parts: list[str] = []
    stream_error: str | None = None
    cancelled_externally = False

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
                        if full_response.endswith(msg_text_emitted):
                            full_response = full_response[: -len(msg_text_emitted)]
                        else:
                            pos = full_response.rfind(msg_text_emitted)
                            full_response = full_response[:pos] if pos >= 0 else full_response
                        await output_queue.put({"type": "thinking_move", "text": msg_text_emitted})
                        msg_text_emitted = ""
                    if msg.id is not None:
                        msg_moved = True

                if content:
                    if msg_moved:
                        thinking_text_parts.append(content)
                        await output_queue.put({"type": "thinking_text", "content": content})
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
        if not agent_task.done():
            agent_task.cancel()

    if cancelled_externally:
        get_last_rag_context(clear=True)
        yield {"type": "cancelled"}
        yield {"type": "done", "cancelled": True}
        return

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None
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

    # 助手落库仅用纯文本；见 chat_with_agent_sync
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
    ):
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
