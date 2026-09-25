"""
会话记忆检索：查询重写 + 混合向量检索。

检索分两路，各自独立配额，避免小而稳的长期事实被大段摘要挤掉：

- **会话级**（``s:`` scope）：episodic 分段摘要为主，raw 原文块为可选兜底；
- **用户级**（``u:`` scope）：跨会话的稳定偏好与硬约束（factual）。

过滤表达式只带「段链右端点 turn_key」这类天然很短的 IN 列表
（段数受 CHAT_COMPACT_MAX_SEGMENTS 约束，默认 8），
彻底避免了 v1「archived_turn_keys 无上限增长 → 表达式无限膨胀」的问题。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage

from app.chat.memory_scope import session_memory_scope, user_memory_scope
from app.chat.milvus_memory import (
    KIND_EPISODIC,
    KIND_FACTUAL,
    KIND_RAW,
    get_chat_memory_milvus,
    memory_filter_expr,
)
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.settings import settings
from app.utils.egress import pinned_llm_client_kwargs

logger = logging.getLogger(__name__)

REWRITE_PROMPT = """你是查询重写助手。将用户输入改写成适合向量检索「历史对话与长期记忆」的简短查询（1~3 句中文或关键词），要求：
- 消除指代（如「那个」「上次」补全为具体主题）；
- 不要回答问题、不要解释；
- 只输出重写后的检索查询文本，不要其他内容。

用户输入：
{query}

重写后的检索查询："""

_FACT_TYPE_LABELS = {
    "preference": "偏好",
    "constraint": "约束",
    "decision": "决策",
    "entity": "实体",
}


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _bool_setting(name: str, default: bool) -> bool:
    v = getattr(settings, name, default)
    return default if v is None else bool(v)


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


def _retrieve(
    milvus,
    dense: list[float],
    query_text: str,
    *,
    filter_expr: str,
    top_k: int,
) -> list[dict[str, Any]]:
    if not filter_expr or top_k <= 0:
        return []
    try:
        return milvus.hybrid_retrieve(dense, query_text, top_k=top_k, filter_expr=filter_expr)
    except Exception:
        logger.exception("memory hybrid_retrieve failed")
        return []


def retrieve_session_memory_hits(
    query: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
    top_k: int,
    allowed_to_keys: list[int] | None = None,
    allowed_turn_keys: list[int] | None = None,
    include_facts: bool | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """混合检索会话记忆；返回 (结果列表, 重写后的检索串)。

    :param allowed_to_keys: 当前路径可达的 episodic 段右端点 turn_key（天然很短）
    :param allowed_turn_keys: raw 模式下允许命中的轮次；空列表表示无可检索轮次
    :param include_facts: 是否检索用户级跨会话事实（缺省读 CHAT_MEMORY_FACTUAL_ENABLED）
    """
    empty: list[dict[str, Any]] = []
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return empty, ""
    q = (query or "").strip()
    if not q:
        return empty, ""

    want_facts = _bool_setting("CHAT_MEMORY_FACTUAL_ENABLED", True) if include_facts is None else bool(include_facts)
    want_episodic = allowed_to_keys is None or len(allowed_to_keys) > 0
    want_raw = _bool_setting("CHAT_MEMORY_ARCHIVE_RAW_ENABLED", False) and bool(allowed_turn_keys)
    if not (want_facts or want_episodic or want_raw):
        return empty, ""

    rewritten = rewrite_memory_query(q, llm_config)
    search_q = (rewritten or q).strip()
    if not search_q:
        return empty, rewritten

    sess_scope = session_memory_scope(user_id, agent_id, session_id)
    usr_scope = user_memory_scope(user_id, agent_id)
    try:
        embedder = get_multimodal_embedding_service()
        dense = embedder.get_text_embeddings([search_q[:8000]])[0]
        milvus = get_chat_memory_milvus()
        milvus.init_collection()

        fact_k = max(1, _int_setting("CHAT_MEMORY_FACT_TOP_K", 3)) if want_facts else 0
        sess_k = max(1, int(top_k))

        sess_hits: list[dict[str, Any]] = []
        if want_episodic or want_raw:
            kinds = ([KIND_EPISODIC] if want_episodic else []) + ([KIND_RAW] if want_raw else [])
            # raw 的轮次白名单可能很长，超上限时不下推表达式，改为过采样 + Python 侧精确过滤
            cap = max(1, _int_setting("CHAT_MEMORY_FILTER_MAX_KEYS", 256))
            raw_keys = [int(k) for k in (allowed_turn_keys or [])] if want_raw else []
            push_raw_keys = raw_keys if 0 < len(raw_keys) <= cap else None
            expr = memory_filter_expr(sess_scope, kinds=kinds, turn_keys=push_raw_keys or None)
            if want_episodic and allowed_to_keys is not None:
                # 段可达性由右端点 turn_key 精确表达；列表长度受段数上限约束，不会膨胀
                keys = ",".join(str(int(k)) for k in allowed_to_keys) or "-1"
                expr += f" && (kind != \"{KIND_EPISODIC}\" || to_turn_key in [{keys}])"
            over_fetch = sess_k * 4 if (raw_keys and push_raw_keys is None) else sess_k * 2
            sess_hits = _retrieve(milvus, dense, search_q, filter_expr=expr, top_k=over_fetch)
            if raw_keys and push_raw_keys is None:
                allowed = set(raw_keys)
                sess_hits = [
                    h
                    for h in sess_hits
                    if str(h.get("kind")) != KIND_RAW or int(h.get("turn_key", -1) or -1) in allowed
                ]

        fact_hits = (
            _retrieve(
                milvus,
                dense,
                search_q,
                filter_expr=memory_filter_expr(usr_scope, kinds=[KIND_FACTUAL]),
                top_k=fact_k,
            )
            if fact_k
            else []
        )
        return _merge_hits(fact_hits, sess_hits, top_k=max(sess_k, fact_k)), rewritten
    except Exception:
        logger.exception("retrieve_session_memory_hits failed")
        return empty, rewritten


def _fused_score(hit: dict[str, Any]) -> float:
    """相关度与价值分融合：长期偏好/约束优先级最高，其次是分段摘要，最后是原文块。"""
    try:
        score = float(hit.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        value = float(hit.get("value_score") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    kind = str(hit.get("kind") or KIND_RAW)
    weight = {KIND_FACTUAL: 0.35, KIND_EPISODIC: 0.2, KIND_RAW: 0.0}.get(kind, 0.0)
    # RRF 分数通常在 0~0.03 量级，先归一再与价值分加权
    return score * 10.0 + weight + value * 0.1


def _merge_hits(
    fact_hits: list[dict[str, Any]], sess_hits: list[dict[str, Any]], *, top_k: int
) -> list[dict[str, Any]]:
    """合并两路结果：事实优先占名额，其余给会话记忆，**总量不超过 top_k**。"""
    total = max(1, int(top_k))
    facts = sorted(fact_hits, key=_fused_score, reverse=True)
    sess = sorted(sess_hits, key=_fused_score, reverse=True)
    if not facts:
        return sess[:total]
    fact_budget = min(len(facts), _int_setting("CHAT_MEMORY_FACT_TOP_K", 3), total)
    picked = facts[:fact_budget]
    return picked + sess[: max(0, total - len(picked))]


def _format_fact(hit: dict[str, Any]) -> str:
    label = _FACT_TYPE_LABELS.get(str(hit.get("fact_type") or ""), "记忆")
    return f"[长期{label}] {str(hit.get('text') or '').strip()}"


def _format_episodic(hit: dict[str, Any]) -> str:
    ti = hit.get("turn_index", "")
    tx = str(hit.get("text") or "").strip()
    return f"[较早对话摘要｜约轮次 {ti} 起] \n{tx}"


def _format_raw(hit: dict[str, Any]) -> str:
    ti = hit.get("turn_index", "")
    tx = str(hit.get("text") or "").strip()
    return f"[较早对话原文｜轮次 {ti}]\n{tx}"


def format_memory_hits(hits: list[dict[str, Any]], *, max_tokens: int | None = None) -> str:
    """把命中结果渲染成注入文本，按类型分块并受 token 预算约束。"""
    if not hits:
        return ""
    budget = max(200, int(max_tokens if max_tokens is not None else _int_setting("CHAT_MEMORY_INJECT_MAX_TOKENS", 1200)))
    from app.chat.context_budget import estimate_tokens

    facts = [h for h in hits if str(h.get("kind")) == KIND_FACTUAL]
    episodic = [h for h in hits if str(h.get("kind")) == KIND_EPISODIC]
    raws = [h for h in hits if str(h.get("kind")) not in (KIND_FACTUAL, KIND_EPISODIC)]

    lines: list[str] = []
    used = 0

    def _push(text: str) -> bool:
        nonlocal used
        cost = estimate_tokens(text)
        if used + cost > budget:
            return False
        used += cost
        lines.append(text)
        return True

    if facts:
        _push("【跨会话长期记忆（用户偏好与硬约束，优先级高于你的默认习惯）】")
        for h in facts:
            if not _push("- " + _format_fact(h)):
                break
    if episodic:
        _push("【本会话较早对话的分段摘要（蒸馏，非逐字）】")
        for i, h in enumerate(episodic, 1):
            if not _push(f"{i}. " + _format_episodic(h)):
                break
    if raws:
        _push("【本会话较早对话的原文片段】")
        for i, h in enumerate(raws, 1):
            if not _push(f"{i}. " + _format_raw(h)):
                break
    if episodic or raws:
        _push("（如需逐字原文，可用 read_session_history 按轮次精确翻阅）")
    return "\n\n".join(lines)


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
    """
    trace: dict[str, Any] = {"tool_used": True, "tool_name": "search_session_memory", "query": query}
    if not (settings.EMBEDDING_API_KEY or "").strip():
        trace["error"] = "未配置 EMBEDDING_API_KEY"
        return ("会话记忆检索不可用：未配置嵌入服务。", trace)

    top_k = max(1, _int_setting("CHAT_MEMORY_SEARCH_TOP_K", 5))
    allowed_to_keys, allowed_turn_keys = _allowed_keys(user_id, agent_id, session_id)
    hits, rewritten = retrieve_session_memory_hits(
        query.strip(),
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        llm_config=llm_config,
        top_k=top_k,
        allowed_to_keys=allowed_to_keys,
        allowed_turn_keys=allowed_turn_keys,
    )
    trace["rewritten_query"] = rewritten
    trace["hit_count"] = len(hits)
    trace["kinds"] = sorted({str(h.get("kind") or "") for h in hits})
    if not hits:
        return ("当前会话较早的对话与长期记忆中未检索到与问题相关的片段。", trace)

    # 工具调用走更宽的预算：模型显式索取时应尽量给全
    text = format_memory_hits(hits, max_tokens=_int_setting("CHAT_MEMORY_TOOL_MAX_TOKENS", 3000))
    return "[会话历史记忆]\n" + text, trace


def _allowed_keys(user_id: int, agent_id: int, session_id: str) -> tuple[list[int], list[int]]:
    """(episodic 段右端点集合, raw 允许轮次集合)。"""
    from app.chat.memory_archive import archived_turn_keys_on_path, episodic_allowed_to_keys

    try:
        to_keys = episodic_allowed_to_keys(user_id, agent_id, session_id)
    except Exception:
        logger.exception("episodic_allowed_to_keys failed")
        to_keys = []
    try:
        turn_keys = archived_turn_keys_on_path(user_id, agent_id, session_id) if _bool_setting(
            "CHAT_MEMORY_ARCHIVE_RAW_ENABLED", False
        ) else []
    except Exception:
        logger.exception("archived_turn_keys_on_path failed")
        turn_keys = []
    return to_keys, turn_keys


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
    """
    if not _bool_setting("CHAT_USE_SESSION_MEMORY", True):
        return None
    if not _bool_setting("CHAT_MEMORY_PROACTIVE_INJECT", True):
        return None
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return None
    q = (user_query or "").strip()
    if not q:
        return None

    from app.chat.memory_archive import archived_turn_keys_on_path, episodic_allowed_to_keys

    top_k = max(1, _int_setting("CHAT_MEMORY_PROACTIVE_TOP_K", 3))
    try:
        allowed_to_keys = episodic_allowed_to_keys(
            user_id, agent_id, session_id, path_turn_keys=path_turn_keys
        )
    except Exception:
        logger.exception("episodic_allowed_to_keys failed")
        allowed_to_keys = []
    allowed_turn_keys: list[int] = []
    if _bool_setting("CHAT_MEMORY_ARCHIVE_RAW_ENABLED", False):
        try:
            allowed_turn_keys = archived_turn_keys_on_path(
                user_id, agent_id, session_id, path_turn_keys
            )
        except Exception:
            logger.exception("archived_turn_keys_on_path failed")

    hits, _rew = retrieve_session_memory_hits(
        q,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        llm_config=llm_config,
        top_k=top_k,
        allowed_to_keys=allowed_to_keys,
        allowed_turn_keys=allowed_turn_keys,
    )
    if not hits:
        return None
    text = format_memory_hits(hits)
    return text or None
