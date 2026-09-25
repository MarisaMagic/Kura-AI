"""实验执行核心：单题 × 单配置的检索 + 可选 rerank + 文档级命中评测 + 可选生成评测。

绕开 retrieve_documents 的高层封装（选档/Auto-merge/HyDE 均不参与），
直接组合 MilvusManager 三种检索腿与 _rerank_documents，保证消融变量唯一可控。
"""

from __future__ import annotations

import time
from typing import Any

from app.experiment import answer_eval
from app.experiment.metrics import doc_level_results, evaluate_docs
from app.kb.milvus_client import MilvusManager, milvus_escape
from app.kb.rag_utils import LEAF_RETRIEVE_LEVEL, _rerank_documents
from app.settings import settings

DEFAULT_CONFIG: dict[str, Any] = {
    "retrieval_mode": "hybrid",
    "fusion": "rrf",
    "weighted_params": [0.7, 0.3],
    "rerank": True,
    "top_k": 5,
    "rrf_k": 60,
    "candidate_multiplier": 3,
}

VALID_MODES = ("dense", "sparse", "hybrid")
VALID_FUSIONS = ("rrf", "weighted")


def config_label(cfg: dict) -> str:
    """生成配置的简短展示名，如 hybrid+rrf+rerank。"""
    mode = cfg.get("retrieval_mode") or "hybrid"
    parts = [mode]
    if mode == "hybrid":
        parts.append(cfg.get("fusion") or "rrf")
    if cfg.get("rerank"):
        parts.append("rerank")
    return "+".join(parts)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """整数化并夹取到 [lo, hi]；None/非法值取默认。显式 0 视为合法输入参与夹取。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def validate_config(raw: dict) -> dict:
    """
    校验并规范化单个消融配置；非法值抛 ValueError。
    :param raw: 前端提交的配置字典
    :return: 规范化后的配置
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in (raw or {}).items() if v is not None})
    mode = str(cfg.get("retrieval_mode") or "").lower()
    if mode not in VALID_MODES:
        raise ValueError(f"retrieval_mode 必须是 {VALID_MODES} 之一")
    fusion = str(cfg.get("fusion") or "").lower()
    if fusion not in VALID_FUSIONS:
        raise ValueError(f"fusion 必须是 {VALID_FUSIONS} 之一")
    cfg["retrieval_mode"] = mode
    cfg["fusion"] = fusion
    cfg["rerank"] = bool(cfg.get("rerank"))
    cfg["top_k"] = _clamp_int(cfg.get("top_k"), 5, 1, 50)
    cfg["rrf_k"] = _clamp_int(cfg.get("rrf_k"), 60, 1, 500)
    cfg["candidate_multiplier"] = _clamp_int(cfg.get("candidate_multiplier"), 3, 1, 10)
    wp = cfg.get("weighted_params") or [0.7, 0.3]
    if not (isinstance(wp, (list, tuple)) and len(wp) == 2):
        raise ValueError("weighted_params 必须是两个数字")
    cfg["weighted_params"] = [float(wp[0]), float(wp[1])]
    if not cfg.get("name"):
        cfg["name"] = config_label(cfg)
    return cfg


def needs_dense_embedding(configs: list[dict]) -> bool:
    """任一配置需要密集向量时为 True（sparse-only 实验可完全不调用 embedding）。"""
    return any((c.get("retrieval_mode") or "hybrid") in ("dense", "hybrid") for c in configs)


def doc_context_map(chunks: list[dict], top_k: int, max_chars: int) -> str:
    """
    将检索块按文档级排名拼成生成用上下文（保留全文，非 120 字摘要）。
    按 filename 首现顺序取前 top_k 个「含文本」文档（图片等无文本块不占编号），合并文本块并标注页码，总长截断到 max_chars。
    """
    order: list[str] = []
    by_file: dict[str, list[dict]] = {}
    for c in chunks:
        fn = str(c.get("filename") or "").strip()
        text = str(c.get("text") or "").strip()
        if not fn or not text or str(c.get("content_type") or "text") != "text":
            continue
        if fn not in by_file:
            if len(order) >= max(1, int(top_k)):
                continue
            order.append(fn)
            by_file[fn] = []
        by_file[fn].append(c)

    parts: list[str] = []
    used = 0
    for i, fn in enumerate(order, 1):
        blocks = [str(c.get("text") or "").strip() for c in by_file[fn]]
        page = by_file[fn][0].get("page_number") or 0
        segment = f"[{i}] {fn} (Page {page}):\n" + "\n".join(blocks)
        remain = max_chars - used
        if remain <= 0:
            break
        if len(segment) > remain:
            segment = segment[:remain]
        parts.append(segment)
        used += len(segment)
    return "\n\n---\n\n".join(parts)


def run_config_for_question(
    *,
    query: str,
    gold_file_keys: list[str],
    config: dict,
    dense_embedding: list[float] | None,
    milvus: MilvusManager,
    kb_scope: str,
    generate: bool = False,
    reference_answer: str = "",
    is_ood: bool = False,
    answer_cfg: dict[str, Any] | None = None,
    judge_cfg: dict[str, Any] | None = None,
    on_stage: answer_eval.StageCallback | None = None,
) -> dict[str, Any]:
    """
    对单题执行单配置检索评测（同步，供后台线程调用）。
    :param query: 问题文本
    :param gold_file_keys: 目标文档文件名（OOD 题为空）
    :param config: validate_config 输出的规范配置
    :param dense_embedding: 预计算的密集向量（sparse-only 时可为 None）
    :param milvus: 线程专用 MilvusManager 实例
    :param kb_scope: 实验知识库范围
    :param generate: 是否追加端到端生成评测（仅终选配置）
    :param reference_answer: 参考答案（生成评测用）
    :param is_ood: 是否 OOD 题（仅做拒答判分）
    :param answer_cfg: 生成 LLM 配置
    :param judge_cfg: 判分 LLM 配置
    :param on_stage: 进度阶段回调（阶段文案, 进度单元数）
    :return: 逐题结果字典（retrieved/hit/... + 可选的 answer/answer_latency_ms/answer_metrics）
    """
    if on_stage:
        on_stage("检索", 1)
    top_k = int(config["top_k"])
    candidate_k = top_k * int(config["candidate_multiplier"])
    esc = milvus_escape(kb_scope)
    # 文字模态：仅检索 L3 叶子文本块
    filter_expr = f'kb_scope == "{esc}" && chunk_level == {LEAF_RETRIEVE_LEVEL}'

    mode = config["retrieval_mode"]
    started = time.monotonic()
    if mode == "dense":
        if dense_embedding is None:
            raise ValueError("dense 检索缺少查询向量")
        chunks = milvus.dense_retrieve(dense_embedding, candidate_k, filter_expr)
    elif mode == "sparse":
        chunks = milvus.sparse_retrieve(query, candidate_k, filter_expr)
    else:
        if dense_embedding is None:
            raise ValueError("hybrid 检索缺少查询向量")
        chunks = milvus.hybrid_retrieve(
            dense_embedding=dense_embedding,
            query_text=query,
            top_k=candidate_k,
            filter_expr=filter_expr,
            rrf_k=int(config["rrf_k"]),
            fusion=config["fusion"],
            weighted_params=config["weighted_params"],
        )

    max_rerank_score: float | None = None
    below_min = False
    if config["rerank"]:
        chunks, rmeta = _rerank_documents(
            query, chunks, return_cap=top_k, include_images=False, skip_rerank=False
        )
        if rmeta.get("rerank_applied"):
            try:
                max_rerank_score = float(rmeta.get("max_rerank_score") or 0.0) or None
            except (TypeError, ValueError):
                max_rerank_score = None
            below_min = bool(rmeta.get("rerank_below_min"))
    else:
        chunks = sorted(chunks, key=lambda d: float(d.get("score") or 0.0), reverse=True)[:top_k]
    latency_ms = int((time.monotonic() - started) * 1000)

    doc_results = doc_level_results(chunks, top_k)
    metrics = evaluate_docs(doc_results, gold_file_keys)
    top1_score = float(doc_results[0]["score"]) if doc_results else 0.0
    result: dict[str, Any] = {
        "retrieved": doc_results,
        "hit": metrics["hit"],
        "hit_rank": metrics["hit_rank"],
        "reciprocal_rank": metrics["reciprocal_rank"],
        "recall": metrics["recall"],
        "top1_score": top1_score,
        "max_rerank_score": max_rerank_score,
        "rerank_below_min": below_min,
        "latency_ms": latency_ms,
        "error": None,
    }
    if generate and answer_cfg and judge_cfg:
        context = doc_context_map(
            chunks, top_k, int(getattr(settings, "EXP_EVAL_MAX_CONTEXT_CHARS", 12000) or 12000)
        )
        result.update(
            answer_eval.evaluate_answer(
                question=query,
                reference=reference_answer,
                context=context,
                is_ood=is_ood,
                answer_cfg=answer_cfg,
                judge_cfg=judge_cfg,
                on_stage=on_stage,
            )
        )
    return result
