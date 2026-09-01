"""检索：使用多模态嵌入向量 + 可选 rerank（未配置则跳过）+ Auto-merge。"""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Tuple

import requests
from langchain.chat_models import init_chat_model
from loguru import logger

from app.core import object_storage as obs
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.kb.milvus_client import (
    MilvusManager,
    _normalize_content_type,
    filename_in_filter_expr,
    kb_filter_expr,
    milvus_escape,
)
from app.kb.parent_chunk_store import ParentChunkStore
from app.settings import settings
from app.utils.egress import pinned_llm_client_kwargs

_multimodal_embedding_service = get_multimodal_embedding_service()
_milvus_manager = MilvusManager()
_parent_chunk_store = ParentChunkStore()

# 以文检索时 document_filenames 白名单条数上限（与智能体选档方案一致）
KB_MAX_DOCUMENT_FILTER = 10


def apply_document_name_filter(
    kb_scope: str,
    document_filenames: list[str] | None,
    *,
    max_docs: int = KB_MAX_DOCUMENT_FILTER,
) -> tuple[str | None, list[str] | None, list[str]]:
    """
    将调用方（智能体）提供的 filename 与向量库中存在的 filename 做交集校验，供 Milvus 子句使用。
    返回: (错误信息或 None, 用于 filter 的 filename 或 None=全库不限制文档, 被剔除的不存在项)。
    约定：document_filenames 为 None 表示不限制；非 None 的列表必须至少能解析出 1 个合法 file_key，否则返回错误信息。
    """
    if document_filenames is None:
        return (None, None, [])
    if not document_filenames:
        return (
            "错误：若需全库检索请传入 None；若需限定文档则须至少 1 个与知识库一致的 file_key。",
            None,
            [],
        )
    allow = set(_milvus_manager.list_distinct_filenames(kb_scope))
    valid: list[str] = []
    invalid: list[str] = []
    for r in (x.strip() for x in document_filenames if (x or "").strip()):
        if r in allow:
            if r not in valid:
                valid.append(r)
        else:
            if r not in invalid:
                invalid.append(r)
    if not valid:
        tail = f" 无效项示例：{invalid[:15]}" if invalid else ""
        return (f"错误：提供的 file_key 均不在本知识库中。{tail}", None, invalid)
    if len(valid) > max_docs:
        valid = valid[:max_docs]
    return (None, valid, invalid)

AUTO_MERGE_ENABLED = bool(settings.AUTO_MERGE_ENABLED)
AUTO_MERGE_THRESHOLD = int(settings.AUTO_MERGE_THRESHOLD or 2)
LEAF_RETRIEVE_LEVEL = int(settings.LEAF_RETRIEVE_LEVEL or 3)


def _rerank_endpoint() -> str:
    """
    获取 rerank 端点（DashScope 原生协议）。
    RERANK_BINDING_HOST 填完整路径时原样使用；填基础地址（.../api/v1）时补全服务路径。
    :return: rerank 端点
    """
    host = (getattr(settings, "RERANK_BINDING_HOST", None) or "").strip().rstrip("/") # 获取 rerank 绑定主机
    if not host:
        return ""
    if "services/rerank" in host:
        return host
    if host.endswith("/api/v1"):
        return f"{host}/services/rerank/text-rerank/text-rerank"
    return host


def _kb_image_data_uri(doc: dict) -> str:
    """
    将图片块从对象存储读为 base64 Data URI（DashScope 多模态 rerank 的 image 输入）。
    仅 jpeg/png 参与；对象缺失、超限或非支持格式时返回空串（该图不参与重排）。
    :param doc: 图片块文档
    :return: data:image/{fmt};base64,... 或空串
    """
    meta = doc.get("image_metadata") or {}
    rel = str(meta.get("stored_relpath") or "").strip().replace("\\", "/")
    if not rel:
        return ""
    fmt = str(meta.get("format") or Path(rel).suffix.lstrip(".") or "").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("jpeg", "png"):
        return ""
    try:
        raw = obs.read_bytes(obs.join_key(settings.USER_AGENT_KB_IMAGES_ROOT, rel))
    except Exception:
        return ""
    max_bytes = max(1, int(getattr(settings, "RERANK_MAX_IMAGE_BYTES", 4 * 1024 * 1024) or 0))
    if len(raw) > max_bytes:
        return ""
    return f"data:image/{fmt};base64,{base64.b64encode(raw).decode('ascii')}"


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


def _image_row_to_retrieval_doc(img: Any, base_score: float, source_text_chunk_id: str) -> dict:
    """由 KbImage 行构造与 hybrid_retrieve 一致结构的图片块，供检索合并。"""
    return {
        "id": None,
        "text": "",
        "filename": img.source_document or img.filename,
        "file_type": "image",
        "page_number": img.page_number,
        "chunk_id": (img.chunk_id or "").strip(),
        "parent_chunk_id": (img.parent_chunk_id or "") or "",
        "root_chunk_id": (img.root_chunk_id or "") or "",
        "chunk_level": 4,
        "chunk_idx": 0,
        "kb_scope": img.kb_scope,
        "content_type": "image",
        "image_path": "",
        "position_start": 0,
        "position_end": 0,
        "image_position_x": int(img.position_x or 0),
        "image_position_y": int(img.position_y or 0),
        "image_width": int(img.position_width or 0),
        "image_height": int(img.position_height or 0),
        "score": float(base_score),
        "related_text_expansion": True,
        "source_text_chunk_id": source_text_chunk_id,
        "image_metadata": {
            "id": img.id,
            "stored_relpath": img.stored_relpath,
            "display_filename": img.display_filename,
            "file_size": img.file_size,
            "width": img.width,
            "height": img.height,
            "format": img.format,
            "position_x": img.position_x,
            "position_y": img.position_y,
            "position_width": img.position_width,
            "position_height": img.position_height,
            "related_text_ids": list(img.related_text_ids or []),
        },
    }


def _expand_related_image_docs(
    retrieved: List[dict],
    kb_scope: str,
    image_store: Any,
    include_images: bool,
) -> tuple[List[dict], int]:
    """
    对候选中的每个文本块，查询 related_text_ids 命中的图片并追加为候选（去重 chunk_id）。
    """
    if not include_images or not bool(getattr(settings, "KB_RELATED_IMAGE_EXPANSION", True)):
        return retrieved, 0
    max_total = int(getattr(settings, "KB_RELATED_IMAGE_MAX_TOTAL", 24) or 24)
    max_per_text = int(getattr(settings, "KB_RELATED_IMAGE_MAX_PER_TEXT", 5) or 5)
    seen: set[str] = set()
    for d in retrieved:
        cid = (d.get("chunk_id") or "").strip()
        if cid:
            seen.add(cid)
    out = list(retrieved)
    added = 0
    for doc in retrieved:
        if added >= max_total:
            break
        if doc.get("content_type") != "text":
            continue
        tid = (doc.get("chunk_id") or "").strip()
        if not tid:
            continue
        base = float(doc.get("score") or 0.0)
        rel_score = base * 0.97 if base > 0 else 0.0
        try:
            related = image_store.get_related_images_for_text(tid)
        except Exception as e:
            logger.warning("get_related_images_for_text 失败，跳过该文本块关联图: tid={!r} err={}", tid, e)
            continue
        n = 0
        for img in related:
            if added >= max_total or n >= max_per_text:
                break
            if (img.kb_scope or "") != kb_scope:
                continue
            ic = (img.chunk_id or "").strip()
            if not ic or ic in seen:
                continue
            seen.add(ic)
            out.append(_image_row_to_retrieval_doc(img, rel_score, tid))
            added += 1
            n += 1
    return out, added


def _final_slice_with_image_floor(docs: List[dict], top_k: int, min_image_slots: int) -> List[dict]:
    """
    在总数不超过 top_k 的前提下，尽量保留若干图片块，再与文本按 score 填满。
    """
    if not docs or top_k <= 0:
        return []
    for d in docs:
        d["content_type"] = _normalize_content_type(d.get("content_type"), d.get("chunk_level", 0))
    if min_image_slots <= 0:
        return sorted(docs, key=lambda x: float(x.get("score") or 0.0), reverse=True)[:top_k]
    by_score = sorted(docs, key=lambda x: float(x.get("score") or 0.0), reverse=True)
    texts = [d for d in by_score if d.get("content_type") == "text"]
    images = [d for d in by_score if d.get("content_type") == "image"]
    want_img = min(len(images), min_image_slots, top_k)
    picked: List[dict] = images[:want_img]
    seen_c: set[str] = set()
    for d in picked:
        c = (d.get("chunk_id") or "").strip()
        if c:
            seen_c.add(c)
    for d in texts:
        if len(picked) >= top_k:
            break
        c = (d.get("chunk_id") or "").strip()
        if c and c in seen_c:
            continue
        if c:
            seen_c.add(c)
        picked.append(d)
    if len(picked) < top_k:
        for d in by_score:
            if len(picked) >= top_k:
                break
            c = (d.get("chunk_id") or "").strip()
            if c and c in seen_c:
                continue
            if c:
                seen_c.add(c)
            picked.append(d)
    return picked[:top_k]


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


def _rerank_documents(
    query: str,
    docs: List[dict],
    return_cap: int,
    include_images: bool = True,
    *,
    skip_rerank: bool = False,
) -> Tuple[List[dict], Dict[str, Any]]:
    """
    重新排序文档, 使用 DashScope 多模态 Rerank 模型（如 qwen3-vl-rerank）对文本+图片统一排序。
    图片块以本地文件 base64 Data URI 参与重排；读取失败/超限的图片块不参与，按向量分排在已重排结果之后。
    rerank 成功后用 relevance_score 覆盖 doc.score（原向量分移入 vector_score），保证下游 Auto-merge / 图片槽位按重排分排序。
    :param return_cap: 本阶段返回条数上限（可大于最终 top_k，便于后续 merge 与图片槽位截断）
    :param include_images: 无图片语义时可 False（当前仅写入 meta）
    :param skip_rerank: 以图检索等场景无自然语言 query 时跳过 rerank，仅按向量分排序
    :return: 重新排序后的文档列表和重新排序的元数据
    """
    for d in docs:
        d["content_type"] = _normalize_content_type(d.get("content_type"), d.get("chunk_level", 0))
    # 分离文本块和图片块
    text_docs = [doc for doc in docs if doc.get("content_type") == "text"]
    image_docs = [doc for doc in docs if doc.get("content_type") == "image"]

    # 文本块加 rrf_rank（供调用方追溯召回顺序）
    if text_docs:
        docs_with_rank = [{**doc, "rrf_rank": i} for i, doc in enumerate(text_docs, 1)] # 添加 rrf_rank 字段
    else:
        docs_with_rank = []

    rm = (getattr(settings, "RERANK_MODEL", None) or "").strip()
    rk = (getattr(settings, "RERANK_API_KEY", None) or "").strip()
    rh = (getattr(settings, "RERANK_BINDING_HOST", None) or "").strip()
    include_img = include_images and bool(getattr(settings, "RERANK_INCLUDE_IMAGES", True))
    meta: Dict[str, Any] = {
        "rerank_enabled": bool(rm and rk and rh),
        "rerank_applied": False,
        "rerank_model": rm,
        "rerank_endpoint": _rerank_endpoint(),
        "rerank_error": None,
        "candidate_count": len(docs),
        "text_count": len(text_docs),
        "image_count": len(image_docs),
        "include_images": include_images,
        "rerank_include_images": include_img,
        "return_cap": return_cap,
        "rerank_below_min": False,
    }

    def _fallback_by_vector_score() -> Tuple[List[dict], Dict[str, Any]]:
        """rerank 不可用时的兜底：保持原行为，按向量分排序。"""
        final_docs = docs_with_rank + image_docs
        final_docs.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return final_docs[:return_cap], meta

    if skip_rerank:
        meta["rerank_skipped"] = "image_query"
        return _fallback_by_vector_score()

    # rerank 未配置或无文本块可排时，按向量分兜底
    if not meta["rerank_enabled"] or not docs_with_rank:
        return _fallback_by_vector_score()

    # 组装多模态候选：文本块 +（可选）可读取的图片块；按向量分截断到单次送排上限
    max_candidates = max(1, int(getattr(settings, "RERANK_MAX_CANDIDATES", 30) or 30))
    candidates: List[dict] = list(docs_with_rank)
    excluded_images: List[dict] = []
    if include_img:
        for doc in image_docs:
            data_uri = _kb_image_data_uri(doc)
            if data_uri:
                candidates.append({**doc, "_rerank_image_uri": data_uri})
            else:
                excluded_images.append(doc)
    else:
        excluded_images = list(image_docs)
    candidates.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    candidates = candidates[:max_candidates]

    documents: List[dict] = []
    for doc in candidates:
        if doc.get("content_type") == "image":
            documents.append({"image": doc["_rerank_image_uri"]})
        else:
            documents.append({"text": doc.get("text", "") or ""})

    payload = {
        "model": rm,
        "input": {
            "query": {"text": query},
            "documents": documents,
        },
        "parameters": {
            "top_n": min(return_cap, len(candidates)),
            "return_documents": False,
        },
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {rk}"}
    timeout = max(5, int(getattr(settings, "RERANK_TIMEOUT_SECONDS", 15) or 15))
    try:
        meta["rerank_applied"] = True
        response = requests.post(meta["rerank_endpoint"], headers=headers, json=payload, timeout=timeout)
        if response.status_code >= 400:
            meta["rerank_error"] = f"HTTP {response.status_code}: {response.text[:500]}"
            return _fallback_by_vector_score()
        items = (response.json().get("output") or {}).get("results") or []
        reranked: List[dict] = []
        for item in items:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                doc = dict(candidates[idx])
                doc.pop("_rerank_image_uri", None)
                sc = item.get("relevance_score")
                if sc is not None:
                    # relevance_score 覆盖 score，使下游排序/展示统一使用重排分
                    doc["vector_score"] = doc.get("score")
                    doc["rerank_score"] = sc
                    try:
                        doc["score"] = float(sc)
                    except (TypeError, ValueError):
                        pass
                reranked.append(doc)
        if reranked:
            # rerank 分数阈值门控（可选）：最高分低于 RERANK_MIN_SCORE 时标记不达标，供上层走「无相关资料」路径
            below_min = False
            min_score = getattr(settings, "RERANK_MIN_SCORE", None)
            if min_score is not None:
                try:
                    top = max((float(d.get("rerank_score") or 0.0) for d in reranked), default=0.0)
                    meta["max_rerank_score"] = top
                    below_min = top < float(min_score)
                except (TypeError, ValueError):
                    meta["max_rerank_score"] = None
            meta["rerank_below_min"] = below_min
            # 保持 rerank 返回顺序（API 按 relevance_score 降序）；未参与重排的图片块按向量分排在末尾
            excluded_images.sort(key=lambda item: item.get("score", 0.0), reverse=True)
            final_docs = reranked + excluded_images
            return final_docs[:return_cap], meta
        meta["rerank_error"] = "empty_rerank_results"
        return _fallback_by_vector_score()
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        meta["rerank_error"] = str(e)[:500]
        return _fallback_by_vector_score()


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
        **pinned_llm_client_kwargs((llm_config.get("base_url") or "").strip() or None),
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
        "请将用户的具体问题抽象成更高层次、更概括的'退步问题'，"
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
        "请基于用户问题生成一段'假设性文档'，内容应像真实资料片段，"
        "用于帮助检索相关信息。文档可以包含合理推测，但需与问题语义相关。"
        "只输出文档正文，不要标题或解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def _finalize_retrieved_docs(
    retrieved: List[dict],
    *,
    query: str,
    kb_scope: str,
    top_k: int,
    candidate_k: int,
    include_images: bool,
    image_store: Any,
    retrieval_mode: str,
    skip_rerank: bool = False,
    expand_related: bool = True,
) -> tuple[List[dict], Dict[str, Any]]:
    """
    为 Milvus 命中的图片补全 image_metadata、按文本块展开关联图、重排、父级 merge、再按图片槽位截断为最终 top_k。
    :param expand_related: 为 False 时不做「文本块→关联图」扩展
    """
    for d in retrieved:
        d["content_type"] = _normalize_content_type(d.get("content_type"), d.get("chunk_level", 0))
    image_chunks = [doc for doc in retrieved if doc.get("content_type") == "image"]
    if image_chunks:
        chunk_ids = [c.get("chunk_id") for c in image_chunks if c.get("chunk_id")]
        images = image_store.get_images_by_chunk_ids(chunk_ids)
        image_map = {img.chunk_id: img for img in images}
        for chunk in retrieved:
            if chunk.get("content_type") != "image":
                continue
            chunk_id = chunk.get("chunk_id")
            if not chunk_id or chunk_id not in image_map:
                continue
            img = image_map[chunk_id]
            chunk["image_metadata"] = {
                "id": img.id,
                "stored_relpath": img.stored_relpath,
                "display_filename": img.display_filename,
                "file_size": img.file_size,
                "width": img.width,
                "height": img.height,
                "format": img.format,
                "position_x": img.position_x,
                "position_y": img.position_y,
                "position_width": img.position_width,
                "position_height": img.position_height,
                "related_text_ids": img.related_text_ids,
            }

    do_expand = include_images and expand_related
    retrieved, rel_added = _expand_related_image_docs(retrieved, kb_scope, image_store, do_expand)
    min_slots = int(getattr(settings, "KB_MIN_IMAGE_SLOTS", 0) or 0) if include_images else 0
    work_cap = min(
        len(retrieved),
        max(
            candidate_k,
            top_k + (min_slots * 2 + 4 if min_slots else 0),
        ),
    )

    reranked, rerank_meta = _rerank_documents(
        query=query,
        docs=retrieved,
        return_cap=work_cap,
        include_images=include_images,
        skip_rerank=skip_rerank,
    )
    rerank_meta["related_image_expansion_count"] = rel_added
    rerank_meta["retrieval_mode"] = retrieval_mode
    rerank_meta["candidate_k"] = candidate_k
    rerank_meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
    rerank_meta["include_images"] = include_images
    rerank_meta["work_cap"] = work_cap
    rerank_meta["final_top_k"] = top_k

    merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=work_cap)
    rerank_meta.update(merge_meta)

    if include_images and min_slots > 0:
        merged_docs = _final_slice_with_image_floor(merged_docs, top_k, min_slots)
    else:
        merged_docs = sorted(merged_docs, key=lambda x: float(x.get("score") or 0.0), reverse=True)[:top_k]
    rerank_meta["image_slot_floor"] = min_slots if include_images else 0
    return merged_docs, rerank_meta


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


def retrieve_documents(
    query: str,
    kb_scope: str,
    top_k: int = 5,
    include_images: bool = True,
    *,
    document_filenames: list[str] | None = None,
) -> Dict[str, Any]:
    """
    检索文档（使用多模态嵌入服务）
    根据查询同时检索文本块和图片块，然后根据文本块和图片块的相似度进行排序，最后返回排序后的文档列表。
    :param query: 查询
    :param kb_scope: 知识库范围
    :param top_k: 返回的文档数量
    :param include_images: 是否包含图片块
    :param document_filenames: 若为非空，仅在这些 filename 对应的块上检索（须已由上层校验）
    :return: 检索后的文档列表和检索的元数据
    """
    from app.kb.image_store import get_image_store
    image_store = get_image_store()
    
    candidate_k = max(top_k * 3, top_k) # 候选文档数量
    esc = milvus_escape(kb_scope) # 转义知识库范围
    
    # 支持检索文本块（L3）和图片块（L4）
    if include_images:
        # 检索所有叶子块：L3文本 + L4图片
        base_filter = f'kb_scope == "{esc}" && (chunk_level == {LEAF_RETRIEVE_LEVEL} || chunk_level == 4)'
    else:
        # 只检索文本块
        base_filter = f'kb_scope == "{esc}" && chunk_level == {LEAF_RETRIEVE_LEVEL}'
    if document_filenames:
        filter_expr = f"({base_filter}) && ({filename_in_filter_expr(document_filenames)})"
    else:
        filter_expr = base_filter
    
    try:
        # 使用多模态嵌入服务生成查询的密集向量（BM25 稀疏向量由 Milvus 服务端 Function 基于文本计算）
        dense_embeddings = _multimodal_embedding_service.get_text_embeddings([query])
        dense_embedding = dense_embeddings[0]

        # 混合检索（dense + 服务端 BM25），检索 candidate_k 个相似文档
        retrieved = _milvus_manager.hybrid_retrieve(
            dense_embedding=dense_embedding,
            query_text=query,
            top_k=candidate_k,
            filter_expr=filter_expr,
        )
        
        merged_docs, rerank_meta = _finalize_retrieved_docs(
            retrieved,
            query=query,
            kb_scope=kb_scope,
            top_k=top_k,
            candidate_k=candidate_k,
            include_images=include_images,
            image_store=image_store,
            retrieval_mode="hybrid",
            skip_rerank=False,
        )
        rerank_meta["document_filenames_filter"] = document_filenames
        return {"docs": merged_docs, "meta": rerank_meta}
    except Exception:
        try:
            # 如果混合检索失败，则使用密集检索
            dense_embeddings = _multimodal_embedding_service.get_text_embeddings([query])
            dense_embedding = dense_embeddings[0]
            retrieved = _milvus_manager.dense_retrieve(
                dense_embedding=dense_embedding,
                top_k=candidate_k,
                filter_expr=filter_expr,
            )
            
            merged_docs, rerank_meta = _finalize_retrieved_docs(
                retrieved,
                query=query,
                kb_scope=kb_scope,
                top_k=top_k,
                candidate_k=candidate_k,
                include_images=include_images,
                image_store=image_store,
                retrieval_mode="dense_fallback",
                skip_rerank=False,
            )
            rerank_meta["document_filenames_filter"] = document_filenames
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
                    "include_images": include_images,
                    "auto_merge_enabled": AUTO_MERGE_ENABLED,
                    "auto_merge_applied": False,
                    "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
                    "auto_merge_replaced_chunks": 0,
                    "auto_merge_steps": 0,
                    "candidate_count": 0,
                },
            }


def retrieve_documents_by_image(
    image_abs_path: str,
    kb_scope: str,
    top_k: int = 5,
    *,
    focus: Literal["text", "image", "mixed"] = "mixed",
    include_related_image_expansion: bool | None = None,
) -> Dict[str, Any]:
    """
    以图片为查询的密集检索（无 BM25）；适用于以图搜文 / 以图搜图。
    """
    from app.kb.image_store import get_image_store

    image_store = get_image_store()
    candidate_k = max(top_k * 3, top_k)
    esc = milvus_escape(kb_scope)

    if focus == "text":
        filter_expr = f'kb_scope == "{esc}" && chunk_level == {LEAF_RETRIEVE_LEVEL}'
    elif focus == "image":
        filter_expr = f'kb_scope == "{esc}" && chunk_level == 4'
    else:
        filter_expr = f'kb_scope == "{esc}" && (chunk_level == {LEAF_RETRIEVE_LEVEL} || chunk_level == 4)'

    if include_related_image_expansion is None:
        include_related_image_expansion = focus != "text"
    include_img_finalize = focus in ("image", "mixed")
    expand_rel = bool(include_related_image_expansion) if include_img_finalize else False

    try:
        dense_embeddings = _multimodal_embedding_service.get_image_embeddings([image_abs_path])
        if not dense_embeddings or not dense_embeddings[0]:
            raise ValueError("empty_image_embedding")
        dense_embedding = dense_embeddings[0]
        retrieved = _milvus_manager.dense_retrieve(
            dense_embedding=dense_embedding,
            top_k=candidate_k,
            filter_expr=filter_expr,
        )
        query_placeholder = ""  # 以图检索不传入 rerank 文本
        merged_docs, rerank_meta = _finalize_retrieved_docs(
            retrieved,
            query=query_placeholder,
            kb_scope=kb_scope,
            top_k=top_k,
            candidate_k=candidate_k,
            include_images=include_img_finalize,
            image_store=image_store,
            retrieval_mode="dense_image_query",
            skip_rerank=True,
            expand_related=expand_rel,
        )
        rerank_meta["focus"] = focus
        rerank_meta["include_related_image_expansion"] = expand_rel
        return {"docs": merged_docs, "meta": rerank_meta}
    except Exception as e:
        logger.warning("retrieve_documents_by_image 失败: {}", e)
        return {
            "docs": [],
            "meta": {
                "rerank_enabled": False,
                "rerank_applied": False,
                "rerank_error": str(e)[:200],
                "retrieval_mode": "dense_image_query_failed",
                "candidate_k": candidate_k,
                "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
                "include_images": include_img_finalize,
                "auto_merge_enabled": AUTO_MERGE_ENABLED,
                "auto_merge_applied": False,
                "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
                "auto_merge_replaced_chunks": 0,
                "auto_merge_steps": 0,
                "candidate_count": 0,
                "focus": focus,
            },
        }
