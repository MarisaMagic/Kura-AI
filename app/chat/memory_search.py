"""
会话记忆检索：查询重写 + 混合向量检索（仅当前 memory_scope）。
包括用于构建 Agent 工具的记忆检索和用于补充系统提示词的预检索。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage

from app.chat.milvus_memory import get_chat_memory_milvus, memory_filter_expr
from app.chat.memory_scope import memory_scope_for
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.settings import settings
from app.utils.egress import pinned_llm_client_kwargs

logger = logging.getLogger(__name__)

REWRITE_PROMPT = """你是查询重写助手。将用户输入改写成适合向量检索「历史对话」的简短查询（1~3 句中文或关键词），要求：
- 消除指代（如「那个」「上次」补全为具体主题）；
- 不要回答问题、不要解释；
- 只输出重写后的查询文本，不要其他内容。

用户输入：
{query}

重写后的检索查询："""


def rewrite_memory_query(raw_query: str, llm_config: dict[str, Any]) -> str:
    """
    重写会话记忆查询, 用于重写用户输入, 使其更适合向量检索历史对话记忆。
    :param raw_query: 原始查询
    :param llm_config: LLM配置
    :return: 重写后的查询
    """
    q = (raw_query or "").strip()
    if not q:
        return ""
    key = (llm_config.get("api_key") or "").strip()
    if not key:
        return q
    try:
        model = init_chat_model(
            model=(llm_config.get("model_name") or "gpt-4"),
            model_provider="openai",
            api_key=key,
            base_url=(llm_config.get("base_url") or "").strip() or None,
            temperature=0,
            stream_usage=False,
            **pinned_llm_client_kwargs((llm_config.get("base_url") or "").strip() or None),
        )
        out = model.invoke([HumanMessage(content=REWRITE_PROMPT.format(query=q))])
        text = (getattr(out, "content", None) or str(out)).strip()
        return text if text else q
    except Exception:
        logger.exception("rewrite_memory_query failed, using raw query")
        return q


def retrieve_session_memory_hits(
    query: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
    top_k: int,
    allowed_turn_keys: list[int] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    混合检索会话记忆；返回 (结果列表, 重写后的检索串)。
    无命中或不可用时结果为空列表，重写串仍可能非空。
    allowed_turn_keys：只检索这些轮次的记忆（当前路径上的已归档轮；空列表表示无可检索轮次）。
    """
    empty: list[dict[str, Any]] = []
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return empty, ""
    q = (query or "").strip()
    if not q:
        return empty, ""
    if allowed_turn_keys is not None and not allowed_turn_keys:
        return empty, ""

    mem_scope = memory_scope_for(user_id, agent_id, session_id)  # 获取会话记忆的隔离键, 只能检索当前会话的记忆
    rewritten = rewrite_memory_query(q, llm_config)  # 重写用户输入, 使其更适合向量检索历史对话记忆。
    search_q = (rewritten or q).strip()
    if not search_q:
        return empty, rewritten

    try:
        embedder = get_multimodal_embedding_service()
        dense = embedder.get_text_embeddings([search_q[:8000]])
        milvus = get_chat_memory_milvus()
        milvus.init_collection()
        flt = memory_filter_expr(mem_scope, turn_keys=allowed_turn_keys)
        hits = milvus.hybrid_retrieve(
            dense[0],
            search_q[:8000],
            top_k=max(1, top_k),
            filter_expr=flt,
        )
        if allowed_turn_keys is not None:
            allowed = {int(k) for k in allowed_turn_keys}
            hits = [h for h in hits if int(h.get("turn_key", -1) or -1) in allowed]
        return hits, rewritten
    except Exception:
        logger.exception("retrieve_session_memory_hits failed")
        return empty, rewritten


def search_session_memory(
    query: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    返回 (格式化片段文本, trace 字典)。将检索到的记忆转换为文本格式, 并返回 trace 字典。
    :param query: 查询
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param llm_config: LLM配置
    :return: 格式化片段文本和 trace 字典
    """
    trace: dict[str, Any] = {"tool_used": True, "tool_name": "search_session_memory", "query": query}
    if not (settings.EMBEDDING_API_KEY or "").strip():
        trace["error"] = "未配置 EMBEDDING_API_KEY"
        return ("会话记忆检索不可用：未配置嵌入服务。", trace)

    top_k = max(1, int(getattr(settings, "CHAT_MEMORY_SEARCH_TOP_K", 5) or 5))
    from app.chat.memory_archive import archived_turn_keys_on_path

    allowed_keys = archived_turn_keys_on_path(user_id, agent_id, session_id)
    hits, rewritten = retrieve_session_memory_hits(
        query.strip(),
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        llm_config=llm_config,
        top_k=top_k,
        allowed_turn_keys=allowed_keys,
    )
    trace["rewritten_query"] = rewritten

    trace["hit_count"] = len(hits)
    if not hits:
        return ("当前会话较早的对话中未检索到与问题相关的片段。", trace)

    lines: list[str] = ["[会话历史记忆] 以下为检索到的较早对话片段（按相关度排序）："]
    for i, h in enumerate(hits, 1):
        ti = h.get("turn_index", "")
        tx = (h.get("text") or "").strip()
        lines.append(f"[{i}] 轮次 {ti}：\n{tx}")
    return "\n\n".join(lines), trace


def proactive_session_memory_inject_text(
    user_query: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
    path_turn_keys: list[int] | None = None,
) -> str | None:
    """
    用本轮用户输入预检索，返回可拼入 System 的摘录正文；无命中或关闭功能时返回 None。
    这部分记忆检索是预先检索的, 用于在用户输入后立即检索, 用于补充系统提示词。和 Agent 工具检索互补。
    :param user_query: 用户输入
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param llm_config: LLM配置
    :return: 可拼入 System 的摘录正文
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return None
    if not getattr(settings, "CHAT_MEMORY_PROACTIVE_INJECT", True):
        return None
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return None
    q = (user_query or "").strip()
    if not q:
        return None

    top_k = max(1, int(getattr(settings, "CHAT_MEMORY_PROACTIVE_TOP_K", 3) or 3))
    from app.chat.memory_archive import archived_turn_keys_on_path

    allowed_keys = archived_turn_keys_on_path(user_id, agent_id, session_id, path_turn_keys)
    hits, _rew = retrieve_session_memory_hits(
        q,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        llm_config=llm_config,
        top_k=top_k,
        allowed_turn_keys=allowed_keys,
    )
    if not hits:
        return None

    lines: list[str] = []
    for i, h in enumerate(hits, 1):
        ti = h.get("turn_index", "")
        tx = (h.get("text") or "").strip()
        lines.append(f"[{i}] 轮次 {ti}：\n{tx}")
    return "\n\n".join(lines)
