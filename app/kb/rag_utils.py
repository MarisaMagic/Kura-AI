"""检索：混合向量 + 可选 rerank（未配置则跳过）+ Auto-merge。"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import requests
from langchain.chat_models import init_chat_model

from app.kb.embedding import EmbeddingService
from app.kb.milvus_client import MilvusManager, kb_filter_expr, milvus_escape
from app.kb.parent_chunk_store import ParentChunkStore
from app.settings import settings

_embedding_service = EmbeddingService()
_milvus_manager = MilvusManager()
_parent_chunk_store = ParentChunkStore()

AUTO_MERGE_ENABLED = bool(settings.AUTO_MERGE_ENABLED)
AUTO_MERGE_THRESHOLD = int(settings.AUTO_MERGE_THRESHOLD or 2)
LEAF_RETRIEVE_LEVEL = int(settings.LEAF_RETRIEVE_LEVEL or 3)


def _rerank_endpoint() -> str:
    """
    获取 rerank 端点
    :return: rerank 端点
    """
    host = (getattr(settings, "RERANK_BINDING_HOST", None) or "").strip().rstrip("/") # 获取 rerank 绑定主机
    if not host:
        return ""
    return host if host.endswith("/v1/rerank") else f"{host}/v1/rerank"


def _merge_to_parent_level(docs: List[dict], threshold: int = 2) -> Tuple[List[dict], int]:
    """
    将文档合并到父级层级
    用于 Auto-merge 功能，将叶子块合并到父级块。
    :param docs: 文档列表
    :param threshold: 阈值
    :return: 合并后的文档列表和合并次数
    """
    # 创建一个默认值为列表的字典，用于存储按父级块ID分组的文档
    groups: Dict[str, List[dict]] = defaultdict(list)
    # 将文档按父级块ID分组
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if parent_id:
            groups[parent_id].append(doc)

    # 获取需要合并的父级块ID列表（文档数量大于等于阈值的父级块ID）
    merge_parent_ids = [pid for pid, children in groups.items() if len(children) >= threshold] 
    # 如果不需要合并，则返回原始文档列表和合并次数
    if not merge_parent_ids:
        return docs, 0

    # 从 PostgreSQL 中获取需要合并的父级块的元数据
    parent_docs = _parent_chunk_store.get_documents_by_ids(merge_parent_ids)
    # 创建一个字典，用于存储按父级块ID分组的父级块的元数据
    parent_map = {item.get("chunk_id", ""): item for item in parent_docs if item.get("chunk_id")}

    # 创建一个列表，用于存储合并后的文档
    merged_docs: List[dict] = []
    merged_count = 0
    # 遍历文档列表
    for doc in docs:
        # 获取文档的父级块ID
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        # 如果文档没有父级块ID，或者父级块ID不在需要合并的父级块ID列表中，则将文档直接添加到合并后的文档列表中
        if not parent_id or parent_id not in parent_map:
            merged_docs.append(doc)
            continue
        # 获取父级块的元数据 和 文档的分数
        parent_doc = dict(parent_map[parent_id])
        score = doc.get("score")
        # 如果文档的分数不为空，则将文档的分数与父级块的分数进行比较，取最大值
        if score is not None:
            parent_doc["score"] = max(float(parent_doc.get("score", score)), float(score))
        # 设置父级块的合并来源为子块
        parent_doc["merged_from_children"] = True
        # 设置父级块的合并子块数量
        parent_doc["merged_child_count"] = len(groups[parent_id])
        # 将父级块添加到合并后的文档列表中
        merged_docs.append(parent_doc)
        merged_count += 1

    # deduped 用于存储去重后的文档
    deduped: List[dict] = []
    seen = set()
    for item in merged_docs:
        key = item.get("chunk_id") or (item.get("filename"), item.get("page_number"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped, merged_count


def _auto_merge_documents(docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    """
    自动合并文档
    :param docs: 文档列表
    :param top_k: 返回的文档数量
    :return: 合并后的文档列表和合并次数
    """
    if not AUTO_MERGE_ENABLED or not docs:
        return docs[:top_k], {
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "auto_merge_applied": False,
            "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
            "auto_merge_replaced_chunks": 0,
            "auto_merge_steps": 0,
        }

    merged_docs, c1 = _merge_to_parent_level(docs, threshold=AUTO_MERGE_THRESHOLD) # 第一次合并（L3叶子块合并到L2父级块）
    merged_docs, c2 = _merge_to_parent_level(merged_docs, threshold=AUTO_MERGE_THRESHOLD) # 第二次合并（L2父级块合并到L1父级块）
    merged_docs.sort(key=lambda item: item.get("score", 0.0), reverse=True) # 按照分数排序
    merged_docs = merged_docs[:top_k]
    replaced = c1 + c2
    return merged_docs, {
        "auto_merge_enabled": AUTO_MERGE_ENABLED,
        "auto_merge_applied": replaced > 0,
        "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
        "auto_merge_replaced_chunks": replaced,
        "auto_merge_steps": int(c1 > 0) + int(c2 > 0),
    }


def _rerank_documents(query: str, docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    """
    重新排序文档, 使用 Rerank 模型对文档进行重新排序。
    :param query: 查询
    :param docs: 文档列表
    :param top_k: 返回的文档数量
    :return: 重新排序后的文档列表和重新排序的元数据
    """
    docs_with_rank = [{**doc, "rrf_rank": i} for i, doc in enumerate(docs, 1)] # 添加 rrf_rank 字段
    rm = getattr(settings, "RERANK_MODEL", None)
    rk = getattr(settings, "RERANK_API_KEY", None)
    rh = getattr(settings, "RERANK_BINDING_HOST", None)
    meta: Dict[str, Any] = {
        "rerank_enabled": bool(rm and rk and rh),
        "rerank_applied": False,
        "rerank_model": rm,
        "rerank_endpoint": _rerank_endpoint(),
        "rerank_error": None,
        "candidate_count": len(docs_with_rank),
    }
    if not docs_with_rank or not meta["rerank_enabled"]:
        return docs_with_rank[:top_k], meta

    payload = {
        "model": rm,
        "query": query,
        "documents": [doc.get("text", "") for doc in docs_with_rank],
        "top_n": min(top_k, len(docs_with_rank)),
        "return_documents": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {rk}"}
    try:
        meta["rerank_applied"] = True
        response = requests.post(meta["rerank_endpoint"], headers=headers, json=payload, timeout=15)
        if response.status_code >= 400:
            meta["rerank_error"] = f"HTTP {response.status_code}: {response.text}"
            return docs_with_rank[:top_k], meta
        items = response.json().get("results", [])
        reranked = []
        for item in items:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs_with_rank):
                doc = dict(docs_with_rank[idx])
                sc = item.get("relevance_score")
                if sc is not None:
                    doc["rerank_score"] = sc
                reranked.append(doc)
        if reranked:
            return reranked[:top_k], meta
        meta["rerank_error"] = "empty_rerank_results"
        return docs_with_rank[:top_k], meta
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        meta["rerank_error"] = str(e)
        return docs_with_rank[:top_k], meta


def _chat_from_config(llm_config: dict | None, temperature: float = 0.2):
    """
    从配置中初始化聊天模型, 用于生成假设性文档和回答退步问题。
    :param llm_config: 聊天模型配置
    :param temperature: 温度
    :return: 聊天模型
    """
    if not llm_config or not (llm_config.get("api_key") or "").strip():
        return None
    return init_chat_model(
        model=llm_config.get("model_name") or "gpt-4",
        model_provider="openai",
        api_key=(llm_config.get("api_key") or "").strip(),
        base_url=(llm_config.get("base_url") or "").strip() or None,
        temperature=temperature,
        stream_usage=True,
    )


def _generate_step_back_question(query: str, llm_config: dict | None) -> str:
    """
    通过大模型生成退步问题
    :param query: 查询
    :param llm_config: 聊天模型配置
    :return: 退步问题
    """
    model = _chat_from_config(llm_config, 0.2)
    if not model:
        return ""
    prompt = (
        "请将用户的具体问题抽象成更高层次、更概括的‘退步问题’，"
        "用于探寻背后的通用原理或核心概念。只输出退步问题一句话，不要解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def _answer_step_back_question(step_back_question: str, llm_config: dict | None) -> str:
    """
    通过大模型回答退步问题
    :param step_back_question: 退步问题
    :param llm_config: 聊天模型配置
    :return: 退步问题答案
    """
    model = _chat_from_config(llm_config, 0.2)
    if not model or not step_back_question:
        return ""
    prompt = (
        "请简要回答以下退步问题，提供通用原理/背景知识，"
        "控制在120字以内。只输出答案，不要列出推理过程。\n"
        f"退步问题：{step_back_question}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def generate_hypothetical_document(query: str, llm_config: dict | None) -> str:
    """
    通过大模型生成假设性文档
    :param query: 查询
    :param llm_config: 聊天模型配置
    :return: 假设性文档
    """
    model = _chat_from_config(llm_config, 0.2)
    if not model:
        return ""
    prompt = (
        "请基于用户问题生成一段‘假设性文档’，内容应像真实资料片段，"
        "用于帮助检索相关信息。文档可以包含合理推测，但需与问题语义相关。"
        "只输出文档正文，不要标题或解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def step_back_expand(query: str, llm_config: dict | None) -> dict:
    """
    扩展查询, 使用大模型生成退步问题和回答退步问题。
    :param query: 查询
    :param llm_config: 聊天模型配置
    :return: 扩展查询
    """
    step_back_question = _generate_step_back_question(query, llm_config)
    step_back_answer = _answer_step_back_question(step_back_question, llm_config)
    if step_back_question or step_back_answer:
        expanded_query = f"{query}\n\n退步问题：{step_back_question}\n退步问题答案：{step_back_answer}"
    else:
        expanded_query = query
    return {
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "expanded_query": expanded_query,
    }


def retrieve_documents(query: str, kb_scope: str, top_k: int = 5) -> Dict[str, Any]:
    """
    检索文档
    :param query: 查询
    :param kb_scope: 知识库范围
    :param top_k: 返回的文档数量
    :return: 检索后的文档列表和检索的元数据
    """
    candidate_k = max(top_k * 3, top_k) # 候选文档数量
    esc = milvus_escape(kb_scope) # 转义知识库范围
    filter_expr = f'kb_scope == "{esc}" && chunk_level == {LEAF_RETRIEVE_LEVEL}' # 过滤表达式
    try:
        # 将查询转换为密集向量和稀疏向量
        dense_embeddings = _embedding_service.get_embeddings([query])
        dense_embedding = dense_embeddings[0]
        sparse_embedding = _embedding_service.get_sparse_embedding(query)
        # 混合检索，检索 candidate_k 个相似文档
        retrieved = _milvus_manager.hybrid_retrieve(
            dense_embedding=dense_embedding,
            sparse_embedding=sparse_embedding,
            top_k=candidate_k,
            filter_expr=filter_expr,
        )
        # 对检索到的文档进行重排序和合并
        reranked, rerank_meta = _rerank_documents(query=query, docs=retrieved, top_k=top_k)
        merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=top_k)
        rerank_meta["retrieval_mode"] = "hybrid"
        rerank_meta["candidate_k"] = candidate_k
        rerank_meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
        rerank_meta.update(merge_meta)
        return {"docs": merged_docs, "meta": rerank_meta}
    except Exception:
        try:
            # 如果混合检索失败，则使用密集检索
            dense_embeddings = _embedding_service.get_embeddings([query])
            dense_embedding = dense_embeddings[0]
            retrieved = _milvus_manager.dense_retrieve(
                dense_embedding=dense_embedding,
                top_k=candidate_k,
                filter_expr=filter_expr,
            )
            reranked, rerank_meta = _rerank_documents(query=query, docs=retrieved, top_k=top_k)
            merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=top_k)
            rerank_meta["retrieval_mode"] = "dense_fallback"
            rerank_meta["candidate_k"] = candidate_k
            rerank_meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
            rerank_meta.update(merge_meta)
            return {"docs": merged_docs, "meta": rerank_meta}
        except Exception:
            return {
                "docs": [],
                "meta": {
                    "rerank_enabled": False,
                    "rerank_applied": False,
                    "rerank_error": "retrieve_failed",
                    "retrieval_mode": "failed",
                    "candidate_k": candidate_k,
                    "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
                    "auto_merge_enabled": AUTO_MERGE_ENABLED,
                    "auto_merge_applied": False,
                    "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
                    "auto_merge_replaced_chunks": 0,
                    "auto_merge_steps": 0,
                    "candidate_count": 0,
                },
            }
