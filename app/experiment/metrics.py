"""实验指标纯函数：文档级命中判定、Hit@k、MRR、Recall@k 与按配置聚合。"""

from __future__ import annotations

from typing import Any

SNIPPET_CHARS = 120


def doc_level_results(retrieved_chunks: list[dict], top_k: int) -> list[dict]:
    """
    将 chunk 级检索结果按 filename 去重为文档级排名（保留首现顺序）。
    :param retrieved_chunks: 已按分数/融合序排列的 chunk 列表
    :param top_k: 文档级截断数量
    :return: [{filename, rank, score, snippet}]，rank 从 1 起
    """
    seen: dict[str, dict] = {}
    for c in retrieved_chunks:
        fn = str(c.get("filename") or "").strip()
        if not fn or fn in seen:
            continue
        text = str(c.get("text") or "").strip().replace("\n", " ")
        seen[fn] = {
            "filename": fn,
            "rank": len(seen) + 1,
            "score": float(c.get("score") or 0.0),
            "snippet": text[:SNIPPET_CHARS],
        }
        if len(seen) >= max(1, int(top_k)):
            break
    return list(seen.values())


def evaluate_docs(doc_results: list[dict], gold_file_keys: list[str]) -> dict[str, Any]:
    """
    文档级命中评测。
    :param doc_results: doc_level_results 输出（已按 top_k 截断）
    :param gold_file_keys: 目标文档文件名列表（OOD 题为空）
    :return: {hit, hit_rank, reciprocal_rank, recall}
    """
    gold = {str(g).strip() for g in gold_file_keys if str(g).strip()}
    if not gold:
        return {"hit": False, "hit_rank": 0, "reciprocal_rank": 0.0, "recall": 0.0}
    filenames = [d["filename"] for d in doc_results]
    hit_rank = 0
    for i, fn in enumerate(filenames, 1):
        if fn in gold:
            hit_rank = i
            break
    matched = len(gold.intersection(filenames))
    return {
        "hit": hit_rank > 0,
        "hit_rank": hit_rank,
        "reciprocal_rank": (1.0 / hit_rank) if hit_rank else 0.0,
        "recall": matched / len(gold),
    }


def aggregate_results(rows: list[dict]) -> dict[str, Any]:
    """
    聚合单配置的全部逐题结果行。
    :param rows: [{hit, hit_rank, reciprocal_rank, recall, is_ood, top1_score,
                  max_rerank_score, rerank_below_min, latency_ms, error}]
    :return: 指标字典（in_kb 为库内题聚合，ood 为库外题聚合）
    """
    in_kb = [r for r in rows if not r.get("is_ood") and not r.get("error")]
    ood = [r for r in rows if r.get("is_ood") and not r.get("error")]
    errors = [r for r in rows if r.get("error")]

    n = len(in_kb)
    hit_rate = (sum(1 for r in in_kb if r.get("hit")) / n) if n else 0.0
    mrr = (sum(float(r.get("reciprocal_rank") or 0.0) for r in in_kb) / n) if n else 0.0
    recall = (sum(float(r.get("recall") or 0.0) for r in in_kb) / n) if n else 0.0
    avg_rank = (
        sum(int(r.get("hit_rank") or 0) for r in in_kb if r.get("hit"))
        / max(1, sum(1 for r in in_kb if r.get("hit")))
        if n
        else 0.0
    )
    avg_latency = (sum(int(r.get("latency_ms") or 0) for r in rows if not r.get("error")) / max(1, len(rows) - len(errors))) if rows else 0.0

    ood_n = len(ood)
    ood_gated = sum(1 for r in ood if r.get("rerank_below_min"))
    ood_avg_top1 = (sum(float(r.get("top1_score") or 0.0) for r in ood) / ood_n) if ood_n else 0.0
    return {
        "question_count": n,
        "hit_rate": round(hit_rate, 4),
        "mrr": round(mrr, 4),
        "recall": round(recall, 4),
        "avg_hit_rank": round(float(avg_rank), 2),
        "avg_latency_ms": int(avg_latency),
        "error_count": len(errors),
        "ood": {
            "count": ood_n,
            "gated_count": ood_gated,
            "gated_rate": round(ood_gated / ood_n, 4) if ood_n else None,
            "avg_top1_score": round(ood_avg_top1, 4) if ood_n else None,
        },
    }
