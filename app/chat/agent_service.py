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
    parts: list[str] = []
    base = (ua.system_prompt or "").strip()
    if base:
        parts.append(base)
    else:
        parts.append("You are a helpful assistant.")
    if ua.enable_web:
        parts.append("用户已开启「联网」能力说明：当前未接入真实联网工具，请勿编造实时网页内容。")
    if ua.enable_code:
        parts.append("用户希望你在适当时给出可运行的代码示例；注意标注语言与前提假设。")
    parts.append(
        "你可以使用工具辅助回答。知识库工具若返回占位说明，请诚实告知用户知识库尚未接入。"
        "同一轮对话中对 search_knowledge_base 最多调用一次；得到工具结果后应直接给出最终回答。"
    )
    return "\n\n".join(parts)


def build_model_and_agent(ua: UserAgent) -> tuple[Any, Any]:
    plain = decrypt_api_key_safe(ua.api_key_ciphertext)
    if not plain or not plain.strip():
        raise ValueError("智能体未配置有效的 API Key")

    base_url = (ua.base_url or "").strip() or None
    model = init_chat_model(
        model=ua.model_name,
        model_provider="openai",
        api_key=plain.strip(),
        base_url=base_url,
        temperature=float(ua.temperature),
        stream_usage=True,
    )
    agent = create_agent(
        model=model,
        tools=[get_current_weather, search_knowledge_base],
        system_prompt=_compose_system_prompt(ua),
    )
    return agent, model


def summarize_old_messages(model: Any, messages: list) -> str:
    old_conversation = "\n".join(
        [f"{'用户' if msg.type == 'human' else 'AI'}: {msg.content}" for msg in messages]
    )
    summary_prompt = f"""请总结以下对话的关键信息：

{old_conversation}
总结（包含用户信息、重要事实、待办事项）："""
    summary = model.invoke(summary_prompt).content
    return summary


def _extract_response_content(result: Any) -> str:
    if isinstance(result, dict):
        if "output" in result:
            return str(result["output"])
        if "messages" in result and result["messages"]:
            msg = result["messages"][-1]
            return str(getattr(msg, "content", msg))
        return str(result)
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
    agent, model = build_model_and_agent(ua)
    messages = storage.load(user_id, agent_id, session_id)

    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    if len(messages) > 50:
        summary = summarize_old_messages(model, messages[:40])
        messages = [SystemMessage(content=f"之前的对话摘要：\n{summary}")] + messages[40:]

    messages.append(HumanMessage(content=user_text))
    result = agent.invoke({"messages": messages}, config={"recursion_limit": 8})
    response_content = _extract_response_content(result)
    messages.append(AIMessage(content=response_content))

    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)

    return {"response": response_content, "rag_trace": rag_trace}


async def chat_with_agent_stream(
    ua: UserAgent,
    user_text: str,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> AsyncIterator[str]:
    agent, model = build_model_and_agent(ua)
    messages = storage.load(user_id, agent_id, session_id)

    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    output_queue: asyncio.Queue = asyncio.Queue()

    class _RagStepProxy:
        def put_nowait(self, step: dict) -> None:
            output_queue.put_nowait({"type": "rag_step", "step": step})

    set_rag_step_queue(_RagStepProxy())

    if len(messages) > 50:
        summary = await asyncio.to_thread(summarize_old_messages, model, messages[:40])
        messages = [SystemMessage(content=f"之前的对话摘要：\n{summary}")] + messages[40:]

    messages.append(HumanMessage(content=user_text))
    full_response = ""

    async def _agent_worker() -> None:
        nonlocal full_response
        try:
            async for msg, _metadata in agent.astream(
                {"messages": messages},
                stream_mode="messages",
                config={"recursion_limit": 8},
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

    agent_task = asyncio.create_task(_agent_worker())

    try:
        while True:
            event = await output_queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
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

    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    if rag_trace:
        yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace}, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"

    messages.append(AIMessage(content=full_response))
    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, agent_id, session_id, messages, extra_message_data=extra_message_data)
