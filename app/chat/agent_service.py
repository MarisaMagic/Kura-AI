"""
LangChain Agent 对话（同步 invoke + 异步 SSE 流式）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
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
    get_current_weather,
    get_last_rag_context,
    reset_tool_call_guards,
    set_rag_step_queue,
)
from app.kb.kb_scope import kb_scope_for
from app.kb.search_tool import make_search_knowledge_tool
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

        if not getattr(settings, "DEBUG_AGENT_KB_PROMPT", True):
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


def _llm_config_from_ua(ua: UserAgent) -> dict[str, Any]:
    """
    从智能体配置中获取 LLM 配置
    :param ua: 智能体
    :return: LLM 配置
    """
    plain = decrypt_api_key_safe(ua.api_key_ciphertext)
    base_url = (ua.base_url or "").strip() or None
    return {
        "api_key": (plain or "").strip(),
        "base_url": base_url,
        "model_name": ua.model_name,
    }


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


def _compose_system_prompt(
    ua: UserAgent,
    use_knowledge_retrieval: bool = True,
    *,
    session_attachment_hint: str = "",
) -> str:
    """
    组合智能体的系统提示词（工具如何调用由各工具的 description 说明，此处不重复指令）。
    :param ua: 智能体
    :param use_knowledge_retrieval: 保留参数以兼容调用方；不在此写入知识库工具说明
    :param session_attachment_hint: 本会话已上传附件的纯事实列表（无工具调用指引）
    :return: 系统提示词
    """
    _ = use_knowledge_retrieval
    parts: list[str] = []
    # 基础提示词, 用户在创建智能体时配置的前置提示词
    base = (ua.system_prompt or "").strip()
    if base:
        parts.append(base)
    else:
        parts.append("You are a helpful assistant.")
    # 联网能力说明
    if ua.enable_web:
        parts.append("用户已开启「联网」能力说明：当前未接入真实联网工具，请勿编造实时网页内容。")
    # 写代码能力说明
    if ua.enable_code:
        parts.append("用户希望你在适当时给出可运行的代码示例；注意标注语言与前提假设。")
    if (session_attachment_hint or "").strip():
        parts.append(session_attachment_hint.strip())
    return "\n\n".join(parts)


def build_model_and_agent(
    ua: UserAgent,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
    session_attachment_hint: str = "",
) -> tuple[Any, Any]:
    """
    构建模型和智能体（知识库检索按 user_id + agent_id 隔离）。
    :param ua: 智能体
    :param user_id: 所属用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID（会话附件工具作用域）
    :param use_knowledge_retrieval: 是否注册知识库检索工具
    :param session_attachment_hint: 本会话附件列表说明
    :return: 模型和智能体
    """
    # 解密 API Key
    plain = decrypt_api_key_safe(ua.api_key_ciphertext)
    if not plain or not plain.strip():
        raise ValueError("智能体未配置有效的 API Key")

    # 获取基础 URL, 用户在创建智能体时配置的 OpenAI 兼容 API Base URL
    base_url = (ua.base_url or "").strip() or None
    # 构建大模型, 使用 OpenAI 兼容 API Base URL
    model = init_chat_model(
        model=ua.model_name,
        model_provider="openai",
        api_key=plain.strip(),
        base_url=base_url,
        temperature=float(ua.temperature),
        stream_usage=True,
    )
    kb_scope = kb_scope_for(user_id, ua.id)
    llm_config = {
        "api_key": plain.strip(),
        "base_url": base_url,
        "model_name": ua.model_name,
    }
    tools: list[Any] = [get_current_weather]
    tools.extend(make_session_attachment_tools(user_id, agent_id, session_id))
    if use_knowledge_retrieval:
        tools.append(make_search_knowledge_tool(kb_scope, llm_config)) # 添加知识库检索工具
    if getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        tools.append(make_search_session_memory_tool(user_id, agent_id, session_id, llm_config)) # 添加会话记忆检索工具
    # create_agent 创建智能体, 使用模型和系统提示词
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=_compose_system_prompt(
            ua,
            use_knowledge_retrieval=use_knowledge_retrieval,
            session_attachment_hint=session_attachment_hint,
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
    attachment_ids: list[str] | None = None,
) -> dict:
    """
    同步对话
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
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
    # 构建智能体和大模型（含会话附件工具）
    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        use_knowledge_retrieval=use_knowledge_retrieval,
        session_attachment_hint=session_attachment_hint,
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
    messages.append(HumanMessage(content=human_content))
    storage.save(user_id, agent_id, session_id, messages, None)

    class _SyncRagStepCollector:
        def __init__(self) -> None:
            self.steps: list[dict] = []

        def put_nowait(self, step: dict) -> None:
            self.steps.append(step)

    rag_collector = _SyncRagStepCollector()
    set_rag_step_queue(rag_collector, sync=True)

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

    error_text = str(caught_exc) if caught_exc else None
    messages.append(AIMessage(content=response_content))
    # 构建额外消息数据（含失败时的 error_text 供历史展示）
    extra_message_data = [None] * (len(messages) - 1) + [
        {"rag_trace": rag_trace, "rag_steps": rag_collector.steps or None, "error_text": error_text}
    ]
    # 保存会话消息，保存最新一轮对话消息到数据库对应会话并更新 Redis 缓存
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)

    # 归档会话记忆，按配置同步或后台线程归档。
    schedule_archive_session_memory(user_id, agent_id, session_id) 

    if caught_exc:
        raise caught_exc

    return {"response": response_content, "rag_trace": rag_trace}


async def iter_chat_stream_events(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
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

    session_attachment_hint = format_attachment_hint(user_id, agent_id, session_id)
    agent, model = build_model_and_agent(
        ua,
        user_id,
        agent_id,
        session_id,
        use_knowledge_retrieval=use_knowledge_retrieval,
        session_attachment_hint=session_attachment_hint,
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

    if not regenerate:
        human_content = build_storable_human_content(
            user_text,
            attachment_ids,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            supports_vision=bool(getattr(ua, "supports_vision", False)),
        )
        messages.append(HumanMessage(content=human_content))

        # 生成完成前即落库用户消息，刷新后仍可从历史会话看到提问（助手在结束时再写入）
        await asyncio.to_thread(storage.save, user_id, agent_id, session_id, messages, None)

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
    stream_error: str | None = None
    cancelled_externally = False

    # 创建异步任务, 调用智能体
    async def _agent_worker() -> None:
        nonlocal full_response, stream_error, cancelled_externally
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
                if getattr(msg, "tool_call_chunks", None):
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

                if content:
                    full_response += content
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

    # 如果 RAG 追踪存在, 则输出 RAG 追踪
    if rag_trace:
        yield {"type": "trace", "rag_trace": rag_trace}

    # 将响应内容添加到会话消息中
    messages.append(AIMessage(content=full_response))
    # 构建额外消息数据（含流式失败时的 error_text 供历史展示）
    extra_message_data = [None] * (len(messages) - 1) + [
        {
            "rag_trace": rag_trace,
            "rag_steps": rag_steps_collected or None,
            "error_text": stream_error,
        }
    ]
    # 保存会话消息，保存最新一轮对话消息到数据库对应会话并更新 Redis 缓存
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)

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
        attachment_ids=attachment_ids,
        regenerate=regenerate,
    ):
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
