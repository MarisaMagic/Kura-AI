"""
LangChain Agent 对话（同步 invoke + 异步 SSE 流式）。
"""

from __future__ import annotations

import asyncio
import json
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
from app.utils.api_key_crypto import decrypt_api_key_safe


def _msg_content_to_str(content: Any) -> str:
    """
    将消息内容转换为字符串
    :param content: 消息内容
    :return: 字符串
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
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _format_one_message_for_debug(msg: BaseMessage) -> str:
    """
    格式化一条消息，用于调试
    :param msg: 消息
    :return: 字符串
    """
    if isinstance(msg, SystemMessage):
        return f"[System]\n{_msg_content_to_str(msg.content)}"
    if isinstance(msg, HumanMessage):
        return f"[Human]\n{_msg_content_to_str(msg.content)}"
    if isinstance(msg, AIMessage):
        blocks = ["[AI]"]
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            blocks.append(f"tool_calls: {json.dumps(tool_calls, ensure_ascii=False)}")
        body = _msg_content_to_str(getattr(msg, "content", ""))
        if body:
            blocks.append(body)
        return "\n".join(blocks)
    if isinstance(msg, ToolMessage):
        name = getattr(msg, "name", "") or ""
        return f"[Tool:{name}]\n{_msg_content_to_str(msg.content)}"
    return f"[{type(msg).__name__}]\n{_msg_content_to_str(getattr(msg, 'content', ''))}"


def _kb_tool_message_in_batch(msg: BaseMessage) -> bool:
    """
    判断一条消息是否是知识库工具消息
    :param msg: 消息
    :return: 是否是知识库工具消息
    """
    if not isinstance(msg, ToolMessage):
        return False
    if getattr(msg, "name", None) == "search_knowledge_base":
        return True
    c = _msg_content_to_str(getattr(msg, "content", ""))
    return (
        "Retrieved Chunks:" in c
        or "No relevant documents found in the knowledge base" in c
        or "TOOL_CALL_LIMIT_REACHED" in c
        or "知识库检索出错" in c
    )


class _KbPromptDebugCallback(BaseCallbackHandler):
    """在「含知识库工具结果」的那次 LLM 调用前打印完整输入消息列表。"""

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
            if not any(_kb_tool_message_in_batch(m) for m in batch):
                continue
            sep = "=" * 72
            lines: list[str] = []
            for i, m in enumerate(batch):
                lines.append(f"--- message[{i}] ---")
                lines.append(_format_one_message_for_debug(m))
            out = "\n".join(lines)
            print(
                f"\n{sep}\n[智能体 LLM] 包含知识库工具结果后的完整输入消息（本轮发给模型的消息列表）:\n{sep}\n{out}\n{sep}\n",
                flush=True,
            )


def _agent_invoke_config() -> dict[str, Any]:
    return {"recursion_limit": 8, "callbacks": [_KbPromptDebugCallback()]}


def _compose_system_prompt(ua: UserAgent, use_knowledge_retrieval: bool = True) -> str:
    """
    组合智能体的系统提示词
    :param ua: 智能体
    :param use_knowledge_retrieval: 是否启用知识库检索相关说明
    :return: 系统提示词
    """
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
    # 工具使用说明（与前端「知识库检索」开关一致）
    if use_knowledge_retrieval:
        parts.append(
            "你可以使用工具辅助回答。当用户问题涉及已上传文档或领域知识时，应使用 search_knowledge_base 检索本智能体知识库。"
            "同一轮对话中对 search_knowledge_base 最多调用一次；得到工具结果后应直接基于检索内容给出最终回答。"
            "若检索无结果，请如实说明。"
        )
    else:
        parts.append("当前对话未启用知识库检索：请勿调用 search_knowledge_base，仅依据通用知识回答。")
    # 返回组合后的系统提示词
    return "\n\n".join(parts)


def build_model_and_agent(
    ua: UserAgent,
    user_id: int,
    *,
    use_knowledge_retrieval: bool = True,
) -> tuple[Any, Any]:
    """
    构建模型和智能体（知识库检索按 user_id + agent_id 隔离）。
    :param ua: 智能体
    :param user_id: 所属用户 ID
    :param use_knowledge_retrieval: 是否注册知识库检索工具
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
    if use_knowledge_retrieval:
        tools.append(make_search_knowledge_tool(kb_scope, llm_config))
    # create_agent 创建智能体, 使用模型和系统提示词
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=_compose_system_prompt(ua, use_knowledge_retrieval=use_knowledge_retrieval),
    )
    # 返回智能体和大模型
    return agent, model


def summarize_old_messages(model: Any, messages: list) -> str:
    """
    总结旧的对话
    :param model: 模型
    :param messages: 对话消息
    :return: 总结
    """
    # 将对话消息转换为字符串, 用户和 AI 分别用不同的标识
    old_conversation = "\n".join(
        [f"{'用户' if msg.type == 'human' else 'AI'}: {msg.content}" for msg in messages]
    )
    # 构建总结提示词
    summary_prompt = f"""请总结以下对话的关键信息：

{old_conversation}
总结（包含用户信息、重要事实、待办事项）："""
    # 调用大模型总结前面的历史对话
    summary = model.invoke(summary_prompt).content
    return summary


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
) -> dict:
    """
    同步对话
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :return: 响应结果
    """
    # 构建智能体和大模型
    agent, model = build_model_and_agent(ua, user_id, use_knowledge_retrieval=use_knowledge_retrieval)
    # 加载会话消息
    messages = storage.load(user_id, agent_id, session_id)

    # 清空 RAG 上下文, 重置工具调用守卫
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    # 如果会话消息超过 50 条, 则总结前面的历史对话
    if len(messages) > 50:
        # 总结前面的历史对话
        summary = summarize_old_messages(model, messages[:40])
        # 将总结添加到会话消息中
        messages = [SystemMessage(content=f"之前的对话摘要：\n{summary}")] + messages[40:]

    # 将用户文本添加到会话消息中
    messages.append(HumanMessage(content=user_text))
    storage.save(user_id, agent_id, session_id, messages, None)

    class _SyncRagStepCollector:
        def __init__(self) -> None:
            self.steps: list[dict] = []

        def put_nowait(self, step: dict) -> None:
            self.steps.append(step)

    rag_collector = _SyncRagStepCollector()
    set_rag_step_queue(rag_collector, sync=True)
    try:
        # invoke 调用智能体
        result = agent.invoke({"messages": messages}, config=_agent_invoke_config())
    finally:
        set_rag_step_queue(None)
    # 提取响应内容
    response_content = _extract_response_content(result)
    messages.append(AIMessage(content=response_content))

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    # 构建额外消息数据
    extra_message_data = [None] * (len(messages) - 1) + [
        {"rag_trace": rag_trace, "rag_steps": rag_collector.steps or None}
    ]
    # 保存会话消息，保存最新一轮对话消息到数据库对应会话并更新 Redis 缓存
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)

    return {"response": response_content, "rag_trace": rag_trace}


async def iter_chat_stream_events(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """
    异步对话：产出与 SSE 中 `data: {...}` 相同结构的 dict 事件（供直连 SSE 与后台 Job 复用）。
    最后依次产出 trace（若有）、写入存储后产出 done。
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param use_knowledge_retrieval: 是否启用知识库检索
    :return: 异步迭代器
    """
    # 构建智能体和大模型, 并加载会话消息
    agent, model = build_model_and_agent(ua, user_id, use_knowledge_retrieval=use_knowledge_retrieval)
    messages = storage.load(user_id, agent_id, session_id)

    # 清空 RAG 上下文, 重置工具调用守卫
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    # 创建输出队列, 收集 RAG 步骤
    output_queue: asyncio.Queue = asyncio.Queue()
    rag_steps_collected: list[dict] = []

    # 创建 RAG 步骤代理, 将 RAG 步骤收集到输出队列
    class _RagStepProxy:
        def put_nowait(self, step: dict) -> None:
            rag_steps_collected.append(step)
            output_queue.put_nowait({"type": "rag_step", "step": step})

    # 设置 RAG 步骤队列, 将 RAG 步骤收集到输出队列
    set_rag_step_queue(_RagStepProxy())

    # 如果会话消息超过 50 条, 则总结前面的历史对话
    if len(messages) > 50:
        summary = await asyncio.to_thread(summarize_old_messages, model, messages[:40])
        messages = [SystemMessage(content=f"之前的对话摘要：\n{summary}")] + messages[40:]

    # 将用户文本添加到会话消息中
    messages.append(HumanMessage(content=user_text))

    # 生成完成前即落库用户消息，刷新后仍可从历史会话看到提问（助手在结束时再写入）
    await asyncio.to_thread(storage.save, user_id, agent_id, session_id, messages, None)

    # 初始化响应内容
    full_response = ""
    # 创建异步任务, 调用智能体
    async def _agent_worker() -> None:
        nonlocal full_response
        try:
            # 异步流式调用智能体
            async for msg, _metadata in agent.astream(
                {"messages": messages},
                stream_mode="messages",
                config=_agent_invoke_config(),
            ):
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
        except Exception as e:
            await output_queue.put({"type": "error", "content": str(e)})
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

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    # 如果 RAG 追踪存在, 则输出 RAG 追踪
    if rag_trace:
        yield {"type": "trace", "rag_trace": rag_trace}

    # 将响应内容添加到会话消息中
    messages.append(AIMessage(content=full_response))
    # 构建额外消息数据
    extra_message_data = [None] * (len(messages) - 1) + [
        {"rag_trace": rag_trace, "rag_steps": rag_steps_collected or None}
    ]
    # 保存会话消息，保存最新一轮对话消息到数据库对应会话并更新 Redis 缓存
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)

    yield {"type": "done"}


async def chat_with_agent_stream(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    use_knowledge_retrieval: bool = True,
) -> AsyncIterator[str]:
    """
    异步对话（SSE 字符串片段）
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :param use_knowledge_retrieval: 是否启用知识库检索
    :return: 异步迭代器
    """
    async for ev in iter_chat_stream_events(
        ua,
        user_text,
        user_id,
        agent_id,
        session_id,
        use_knowledge_retrieval=use_knowledge_retrieval,
    ):
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
