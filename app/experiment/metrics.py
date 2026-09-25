"""实验指标纯函数：文档级命中判定、Hit@k、MRR、Recall@k、答案确定性指标与按配置聚合。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

SNIPPET_CHARS = 120

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


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


# ------------------------------------------------ 答案确定性指标（生成评测）


def normalize_answer(text: Any) -> str:
    """答案归一化：NFKC、小写、去引用角标（[1]/【1】），仅保留字母与数字（含 CJK）。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = re.sub(r"[\[【]\s*\d+\s*[\]】]", "", s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N"))


def exact_match(pred: Any, ref: Any) -> float | None:
    """归一化后完全一致 → 1.0，否则 0.0；任一方为空 → None（不适用）。"""
    p, r = normalize_answer(pred), normalize_answer(ref)
    if not p or not r:
        return None
    return 1.0 if p == r else 0.0


def char_f1(pred: Any, ref: Any) -> float | None:
    """字符多重集 F1（SQuAD 风格，中文按字符）；任一方为空 → None。"""
    p, r = normalize_answer(pred), normalize_answer(ref)
    if not p or not r:
        return None
    overlap = sum((Counter(p) & Counter(r)).values())
    if not overlap:
        return 0.0
    precision = overlap / len(p)
    recall = overlap / len(r)
    return 2 * precision * recall / (precision + recall)


def extract_numbers(text: Any) -> list[float]:
    """抽取阿拉伯数字（含小数/负数）；忽略千分位逗号，百分号按去掉符号处理。"""
    out: list[float] = []
    for m in _NUM_RE.finditer(str(text or "").replace(",", "")):
        try:
            out.append(float(m.group()))
        except ValueError:
            continue
    return out


def numeric_hit(pred: Any, ref: Any, rel_tol: float = 0.01, abs_tol: float = 0.01) -> float | None:
    """数值题容差命中：参考数字均能在预测答案中找到容差内数字 → 1.0，否则 0.0；
    参考答案无数字 → None（不适用）。"""
    ref_nums = extract_numbers(ref)
    if not ref_nums:
        return None
    pred_nums = extract_numbers(pred)
    for rn in ref_nums:
        tol = max(abs_tol, abs(rn) * rel_tol)
        if not any(abs(pn - rn) <= tol for pn in pred_nums):
            return 0.0
    return 1.0


def _numeric_values(rows: list[dict], key: str) -> list[float]:
    """收集各行 answer_metrics[key] 的数值（None/布尔/非数值一律跳过）。"""
    out: list[float] = []
    for r in rows:
        m = r.get("answer_metrics")
        if not isinstance(m, dict):
            continue
        v = m.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _rate(values: list[float], threshold: float) -> float | None:
    """达到阈值（含）的比例；无样本 → None。"""
    if not values:
        return None
    hit = sum(1 for v in values if v >= threshold - 1e-9)
    return round(hit / len(values), 4)


def _aggregate_generation(rows: list[dict]) -> dict[str, Any] | None:
    """聚合端到端问答评测指标；无任何实际执行生成的题目时返回 None。

    分母口径：仅统计 answer_metrics 非空的题；judge 失败（指标为 null）不进对应均值，
    单独计入 judge_error_count；OOD 题只统计拒答正确率。
    """
    gen_rows = [r for r in rows if isinstance(r.get("answer_metrics"), dict) and r["answer_metrics"]]
    if not gen_rows:
        return None
    in_kb = [r for r in gen_rows if not r.get("is_ood")]
    ood = [r for r in gen_rows if r.get("is_ood")]

    correctness = _numeric_values(in_kb, "correctness")
    faithfulness = _numeric_values(in_kb, "faithfulness")
    em = _numeric_values(in_kb, "em")
    f1 = _numeric_values(in_kb, "f1")
    numeric = _numeric_values([r for r in in_kb if (r.get("stratum") or "").strip() == "numeric"], "numeric_hit")
    refused = [
        1.0 if (r.get("answer_metrics") or {}).get("refused") is True else 0.0
        for r in ood
        if isinstance((r.get("answer_metrics") or {}).get("refused"), bool)
    ]
    latencies = [
        int(r.get("answer_latency_ms") or 0)
        for r in gen_rows
        if not (r.get("answer_metrics") or {}).get("gen_error")
    ]

    by_stratum: dict[str, list[dict]] = {}
    for r in in_kb:
        key = str(r.get("stratum") or "").strip() or "unknown"
        by_stratum.setdefault(key, []).append(r)

    return {
        "count": len(gen_rows),
        "correctness_mean": _mean(correctness),
        # 二值口径正确率（新数据 correctness ∈ {0,1}）；pass_* 保留用于历史 0/0.5/1 数据兼容
        "correct_rate": _rate(correctness, 1.0),
        "incorrect_rate": (round(1 - sum(1 for v in correctness if v >= 1.0 - 1e-9) / len(correctness), 4) if correctness else None),
        "legacy_partial_count": sum(1 for v in correctness if abs(v - 0.5) < 1e-9),
        "pass_exact_rate": _rate(correctness, 1.0),
        "pass_partial_rate": _rate(correctness, 0.5),
        "faithfulness_mean": _mean(faithfulness),
        "hallucination_rate": (round(1 - sum(faithfulness) / len(faithfulness), 4) if faithfulness else None),
        "em": _mean(em),
        "f1": _mean(f1),
        "numeric": {"count": len(numeric), "accuracy": _mean(numeric)},
        "ood": {
            "count": len(refused),
            "correct_refusal_rate": _mean(refused),
            "false_answer_rate": (round(1 - sum(refused) / len(refused), 4) if refused else None),
        },
        "avg_answer_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
        "gen_error_count": sum(1 for r in gen_rows if (r.get("answer_metrics") or {}).get("gen_error")),
        "judge_error_count": sum(1 for r in gen_rows if (r.get("answer_metrics") or {}).get("judge_error")),
        "by_stratum": {
            key: {
                "count": len(group),
                "correct_rate": _rate(_numeric_values(group, "correctness"), 1.0),
                "correctness_mean": _mean(_numeric_values(group, "correctness")),
                "em": _mean(_numeric_values(group, "em")),
                "f1": _mean(_numeric_values(group, "f1")),
                "numeric_accuracy": _mean(_numeric_values(group, "numeric_hit")),
            }
            for key, group in sorted(by_stratum.items())
        },
    }


def aggregate_results(rows: list[dict]) -> dict[str, Any]:
    """
    聚合单配置的全部逐题结果行。
    :param rows: [{hit, hit_rank, reciprocal_rank, recall, is_ood, top1_score,
                  max_rerank_score, rerank_below_min, latency_ms, error,
                  stratum, answer_metrics, answer_latency_ms}]
    :return: 指标字典（in_kb 为库内题聚合，ood 为库外题聚合，generation 为生成评测聚合）
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
    result = {
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
    generation = _aggregate_generation(rows)
    if generation:
        result["generation"] = generation
    return result
