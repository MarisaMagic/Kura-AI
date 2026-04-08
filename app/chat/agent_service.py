"""
LangChain Agent 对话（同步 invoke + 异步 SSE 流式）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from app.chat.storage import storage
from app.chat.tools import (
    get_current_weather,
    get_last_rag_context,
    reset_tool_call_guards,
    search_knowledge_base,
    set_rag_step_queue,
)
from app.models.user_agent import UserAgent
from app.utils.api_key_crypto import decrypt_api_key_safe


def _compose_system_prompt(ua: UserAgent) -> str:
    """
    组合智能体的系统提示词
    :param ua: 智能体
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
    # 工具使用说明
    parts.append(
        "你可以使用工具辅助回答。知识库工具若返回占位说明，请诚实告知用户知识库尚未接入。"
        "同一轮对话中对 search_knowledge_base 最多调用一次；得到工具结果后应直接给出最终回答。"
    )
    # 返回组合后的系统提示词
    return "\n\n".join(parts)


def build_model_and_agent(ua: UserAgent) -> tuple[Any, Any]:
    """
    构建模型和智能体
    :param ua: 智能体
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
    # create_agent 创建智能体, 使用模型和系统提示词
    agent = create_agent(
        model=model,
        tools=[get_current_weather, search_knowledge_base],
        system_prompt=_compose_system_prompt(ua),
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
    agent, model = build_model_and_agent(ua)
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
    # invoke 调用智能体
    result = agent.invoke({"messages": messages}, config={"recursion_limit": 8})
    # 提取响应内容
    response_content = _extract_response_content(result)
    messages.append(AIMessage(content=response_content))

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    # 构建额外消息数据
    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    # 保存会话消息，保存最新一轮对话消息到数据库对应会话并更新 Redis 缓存
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)

    return {"response": response_content, "rag_trace": rag_trace}


async def chat_with_agent_stream(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> AsyncIterator[str]:
    """
    异步对话
    :param ua: 智能体
    :param user_text: 用户文本
    :param user_id: 用户 ID
    :param agent_id: 智能体 ID
    :param session_id: 会话 ID
    :return: 响应结果
    """
    # 构建智能体和大模型
    agent, model = build_model_and_agent(ua)
    # 加载会话消息
    messages = storage.load(user_id, agent_id, session_id)

    # 清空 RAG 上下文, 重置工具调用守卫
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    # 创建异步队列
    output_queue: asyncio.Queue = asyncio.Queue()

    # 创建 RAG 步骤代理
    class _RagStepProxy:
        # 将 RAG 步骤添加到异步队列
        def put_nowait(self, step: dict) -> None:
            output_queue.put_nowait({"type": "rag_step", "step": step})

    # 设置 RAG 步骤队列
    set_rag_step_queue(_RagStepProxy())

    # 如果会话消息超过 50 条, 则总结前面的历史对话
    if len(messages) > 50:
        # 总结前面的历史对话
        summary = await asyncio.to_thread(summarize_old_messages, model, messages[:40])
        messages = [SystemMessage(content=f"之前的对话摘要：\n{summary}")] + messages[40:]

    # 将用户文本添加到会话消息中
    messages.append(HumanMessage(content=user_text))
    full_response = ""

    # 创建异步代理工作线程
    async def _agent_worker() -> None:
        nonlocal full_response
        try:
            # astream 异步流式调用智能体
            async for msg, _metadata in agent.astream(
                {"messages": messages},
                stream_mode="messages",
                config={"recursion_limit": 8},
            ):
                # 如果消息不是 AI 消息块, 则跳过
                if not isinstance(msg, AIMessageChunk):
                    continue
                # 如果消息包含工具调用块, 则跳过
                if getattr(msg, "tool_call_chunks", None):
                    continue

                # 获取消息内容
                content = ""
                # 如果消息内容是字符串, 则添加到内容中
                if isinstance(msg.content, str):
                    content = msg.content
                # 如果消息内容是列表, 则遍历列表
                elif isinstance(msg.content, list):
                    # 遍历列表
                    for block in msg.content:
                        if isinstance(block, str):
                            content += block
                        # 如果消息内容是字典且类型为文本, 则添加到内容中
                        elif isinstance(block, dict) and block.get("type") == "text":
                            content += block.get("text", "")

                # 如果内容不为空, 则添加到响应内容中
                if content:
                    full_response += content
                    await output_queue.put({"type": "content", "content": content})
        except Exception as e:
            # 如果出现异常, 则将异常添加到异步队列
            await output_queue.put({"type": "error", "content": str(e)})
        finally:
            # 最后将 None 添加到异步队列
            await output_queue.put(None)

    # 创建异步代理任务
    agent_task = asyncio.create_task(_agent_worker())

    try:
        while True:
            # 获取异步队列事件
            event = await output_queue.get()
            if event is None:
                break
            # 生成事件
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except GeneratorExit:
        # 如果生成器退出, 则取消异步代理任务
        agent_task.cancel()
        # 尝试取消异步代理任务
        try:
            await agent_task
        except asyncio.CancelledError:
            pass
        raise
    finally:
        # 设置 RAG 步骤队列为 None
        set_rag_step_queue(None)
        # 如果异步代理任务未完成, 则取消异步代理任务
        if not agent_task.done():
            agent_task.cancel()

    # 获取 RAG 上下文
    rag_context = get_last_rag_context(clear=True)
    # 获取 RAG 追踪
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    # 如果 RAG 追踪不为空, 则生成 RAG 追踪事件
    if rag_trace:
        # 生成 RAG 追踪事件
        yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace}, ensure_ascii=False)}\n\n"

    # 生成完成事件
    yield "data: [DONE]\n\n"

    # 将响应内容添加到会话消息中
    messages.append(AIMessage(content=full_response))
    # 构建额外消息数据
    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    # 保存会话消息，保存最新一轮对话消息到数据库对应会话并更新 Redis 缓存
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)
