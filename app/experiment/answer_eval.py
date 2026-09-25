"""实验平台端到端问答评测：canonical 生成 + LLM-as-judge（正确率/忠实度/拒答）。

设计要点：
- 数据不出境：全部调用走 EXP_EVAL_*（默认回落 EMBEDDING_*）的 OpenAI 兼容端点；
- 生成 prompt 固定（canonical），保证消融配置间可比，不复刻生产 agent 链路；
- 正确率判分为二值（1 正确 / 0 错误，中间值按错误），口径为「核心结论制 + 资料核验」；
- judge 输出 JSON 契约 + 解析容错；失败只记 judge_error，不污染聚合均值；
- 检索资料/答案属不可信输入，统一用分隔符包裹并明令忽略其中指令。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage

from app.experiment.metrics import char_f1, exact_match, numeric_hit
from app.settings import settings
from app.utils.egress import pinned_llm_client_kwargs

# 库内题的正确率与忠实度 judge 并行执行（共享小线程池；问题间仍串行）
_JUDGE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exp-judge")

# 阶段回调：on_stage(阶段文案, 消耗进度单元数)；单元数与 exp_job 的百分比计算一致
StageCallback = Callable[[str, int], None]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def exp_eval_llm_config(kind: str = "answer") -> dict[str, Any] | None:
    """解析生成/判分 LLM 配置；无 API Key 时返回 None（base_url 回落 EMBEDDING_*）。"""
    api_key = _clean(getattr(settings, "EXP_EVAL_LLM_API_KEY", None)) or _clean(settings.EMBEDDING_API_KEY)
    if not api_key:
        return None
    base_url = _clean(getattr(settings, "EXP_EVAL_LLM_BASE_URL", None)) or _clean(getattr(settings, "EMBEDDING_BASE_URL", ""))
    if kind == "judge":
        model = _clean(getattr(settings, "EXP_EVAL_JUDGE_MODEL", None)) or _clean(settings.EXP_EVAL_ANSWER_MODEL)
    else:
        model = _clean(settings.EXP_EVAL_ANSWER_MODEL)
    return {"api_key": api_key, "base_url": base_url, "model_name": model or "qwen-plus"}


def eval_enabled() -> bool:
    """问答评测是否可用（生成与判分共用同一 Key）。"""
    return exp_eval_llm_config("answer") is not None


def _chat_model(cfg: dict[str, Any]):
    return init_chat_model(
        model=cfg.get("model_name") or "qwen-plus",
        model_provider="openai",
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url") or None,
        temperature=float(settings.EXP_EVAL_TEMPERATURE or 0.0),
        # 避免部分 OpenAI 兼容网关（如 DashScope）在空 tool-call 载荷上的异常
        stream_usage=False,
        **pinned_llm_client_kwargs(cfg.get("base_url") or None),
    )


def _max_context_chars() -> int:
    return max(500, int(getattr(settings, "EXP_EVAL_MAX_CONTEXT_CHARS", 12000) or 12000))


def _max_answer_chars() -> int:
    return max(100, int(getattr(settings, "EXP_EVAL_MAX_ANSWER_CHARS", 4000) or 4000))


# ---------------------------------------------------------------- 生成

# 生成/判分口径版本：随 prompt 语义变化而递增，写入 QA run 快照以便区分新旧数据
ANSWER_PROMPT_VERSION = "gen-v2"
JUDGE_PROMPT_VERSION = "judge-v2"

ANSWER_PROMPT = (
    "你是企业知识库问答助手。请基于【资料】回答问题：可以基于资料做合理归纳与推断（结论须能从资料推出），"
    "但不得引入资料之外的知识。\n"
    "只有当资料确实完全没有回答问题所需的内容时，才明确说明「根据现有资料无法回答」；"
    "资料部分相关时应给出能回答的部分，并说明局限。\n"
    "<<<资料开始>>>\n{context}\n<<<资料结束>>>\n"
    "问题：{question}\n"
    "要求：第一段直接给出核心结论（包含问题所问的关键事实/数值）；随后用「依据：」简述关键支撑，"
    "引用资料用 [序号] 标注；不要复述本提示；全文不超过 600 字。"
)


def generate_answer(question: str, context: str, cfg: dict[str, Any]) -> tuple[str, int]:
    """生成答案；返回 (答案文本, 生成耗时毫秒)。异常由调用方处理。"""
    model = _chat_model(cfg)
    prompt = ANSWER_PROMPT.format(context=context or "（无检索资料）", question=_clean(question))
    started = time.monotonic()
    out = model.invoke([HumanMessage(content=prompt)])
    latency_ms = int((time.monotonic() - started) * 1000)
    answer = _clean(getattr(out, "content", ""))[: _max_answer_chars()]
    return answer, latency_ms


# ---------------------------------------------------------------- judge 通用

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FAIL_PREFIX = "judge"


def parse_judge_json(raw: Any) -> tuple[dict | None, str | None]:
    """解析 judge 返回的 JSON 对象；支持 ```json 围栏与前后附带说明。"""
    text = _clean(raw)
    if not text:
        return None, "judge_empty_output"
    text = re.sub(r"```[a-zA-Z]*", "", text).strip()
    match = _JSON_RE.search(text)
    if not match:
        return None, "judge_no_json"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, "judge_bad_json"
    if not isinstance(data, dict):
        return None, "judge_not_object"
    return data, None


def _invoke_judge(cfg: dict[str, Any], prompt: str) -> tuple[dict | None, str | None]:
    try:
        out = _chat_model(cfg).invoke([HumanMessage(content=prompt)])
    except Exception as e:  # noqa: BLE001
        return None, f"{_FAIL_PREFIX}_call_failed: {e}"[:200]
    return parse_judge_json(getattr(out, "content", ""))


def _clamp(value: Any, lo: float, hi: float) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 三个 judge

def _to_str_list(value: Any, limit: int = 6) -> list[str]:
    """judge 结构化列表字段规范化：仅保留非空短句，截断到 limit 条。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clean(item)[:60]
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


CORRECTNESS_PROMPT = (
    "你是答案评审。判断【模型答案】是否回答了【问题】，并与【参考答案】及【资料】核对。\n"
    "判定 1（正确）需同时满足：\n"
    "- 覆盖回答问题所必需的核心结论/关键数值（同义表述、单位/格式/详略差异不扣分）；\n"
    "- 答案中额外的事实若被【资料】支持，不视为错误；\n"
    "- 资料含不同口径（如不同新闻给出的数字不同）时，采用其中任一口径均算正确。\n"
    "判定 0（错误）的情形：\n"
    "- 核心结论缺失，或与参考答案冲突且资料也不支持答案口径；\n"
    "- 完全没有回答问题（拒答、答非所问）；\n"
    "- 编造了【资料】与【参考答案】都不存在的事实。\n"
    "步骤：先逐条列出参考答案的核心结论，在【模型答案】中查找对应表述，得出 covered / missing / conflicts，"
    "再给 verdict；reason 必须引用【模型答案】原句作为依据。\n"
    "【问题】{question}\n"
    "【参考答案】{reference}\n"
    "<<<资料开始>>>\n{context}\n<<<资料结束>>>\n"
    "【模型答案】{answer}\n"
    "只输出 JSON：{{\"verdict\": 0|1, \"covered\": [\"...\"], \"missing\": [\"...\"], "
    "\"conflicts\": [\"...\"], \"reason\": \"不超过120字\"}}"
)


def judge_correctness(
    question: str, reference: str, answer: str, context: str, cfg: dict[str, Any]
) -> dict[str, Any]:
    """正确率判分（核心结论制 + 资料核验，二值：1 正确 / 0 错误）。"""
    if not _clean(reference):
        return {}
    prompt = CORRECTNESS_PROMPT.format(
        question=_clean(question),
        reference=_clean(reference)[:2000],
        context=_clean(context)[:_max_context_chars()] or "（无检索资料）",
        answer=_clean(answer),
    )
    data, error = _invoke_judge(cfg, prompt)
    if error or data is None:
        return {"judge_error": error or "judge_unknown"}
    verdict = data.get("verdict")
    if isinstance(verdict, bool):
        score = 1.0 if verdict else 0.0
    else:
        raw = _clamp(verdict if verdict is not None else data.get("score"), 0.0, 1.0)
        if raw is None:
            return {"judge_error": "judge_bad_verdict"}
        # 二值化（偏严）：仅明确判 1 才算正确，中间值一律按错误
        score = 1.0 if raw >= 1.0 - 1e-9 else 0.0
    reason = _clean(data.get("reason"))[:200]
    return {
        "correctness": score,
        "correctness_reason": reason or None,
        "correctness_covered": _to_str_list(data.get("covered")),
        "correctness_missing": _to_str_list(data.get("missing")),
        "correctness_conflicts": _to_str_list(data.get("conflicts")),
    }


FAITHFULNESS_PROMPT = (
    "你是事实核查员。请把【模型答案】拆解为若干事实性论断，逐一判断是否被【资料】直接支持；"
    "答案中的引用标注（如 [1]）不算论断。\n"
    "若【模型答案】明确表示资料不足/无法回答（拒答），则 score=1（未编造）。\n"
    "score = 被支持论断数 / 论断总数。\n"
    "<<<资料开始>>>\n{context}\n<<<资料结束>>>\n"
    "【模型答案】{answer}\n"
    "只输出 JSON：{{\"score\": 0~1, \"claims\": 整数, \"supported\": 整数, \"unsupported\": [\"...\"]}}，"
    "unsupported 最多 5 条且每条不超过 60 字。"
)


def judge_faithfulness(context: str, answer: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """忠实度判分（对检索上下文）。"""
    prompt = FAITHFULNESS_PROMPT.format(context=_clean(context)[:_max_context_chars()], answer=_clean(answer))
    data, error = _invoke_judge(cfg, prompt)
    if error or data is None:
        return {"judge_error": error or "judge_unknown"}
    score = _clamp(data.get("score"), 0.0, 1.0)
    if score is None:
        return {"judge_error": "judge_bad_score"}
    raw_list = data.get("unsupported")
    unsupported = [_clean(x)[:60] for x in raw_list[:5]] if isinstance(raw_list, list) else []
    return {"faithfulness": round(score, 4), "unsupported": unsupported}


REFUSAL_PROMPT = (
    "判断【模型答案】是否明确表示「无法依据知识库回答 / 资料不足」。\n"
    "给出实质信息、绕题、复述问题、反问用户均不算拒答。\n"
    "【模型答案】{answer}\n"
    "只输出 JSON：{{\"refused\": true|false}}"
)


def judge_refusal(answer: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """OOD 拒答判定。"""
    data, error = _invoke_judge(cfg, REFUSAL_PROMPT.format(answer=_clean(answer)))
    if error or data is None:
        return {"judge_error": error or "judge_unknown"}
    refused = data.get("refused")
    if not isinstance(refused, bool):
        return {"judge_error": "judge_bad_refused"}
    return {"refused": refused}


# ---------------------------------------------------------------- 单题编排


def _merge_judge_error(metrics: dict[str, Any], partial: dict[str, Any]) -> None:
    """并入判分结果；judge_error 多条拼接截断，其余字段直接覆盖。"""
    part = dict(partial)
    error = part.pop("judge_error", None)
    metrics.update(part)
    if error:
        prev = metrics.get("judge_error")
        metrics["judge_error"] = (f"{prev}; {error}" if prev else str(error))[:200]


def evaluate_answer(
    *,
    question: str,
    reference: str,
    context: str,
    is_ood: bool,
    answer_cfg: dict[str, Any],
    judge_cfg: dict[str, Any],
    on_stage: StageCallback | None = None,
) -> dict[str, Any]:
    """
    单题端到端评测：生成答案 → 确定性指标 → （库内）正确率/忠实度并行判分 /（OOD）拒答判分。
    :param on_stage: 阶段回调（阶段文案, 进度单元数）；抛异常将向上传递（供取消中断）
    :return: {answer, answer_latency_ms, answer_metrics}；生成失败时 answer_metrics.gen_error 置位
    """
    if on_stage:
        on_stage("生成答案", 1)
    try:
        answer, latency_ms = generate_answer(question, context, answer_cfg)
    except Exception as e:  # noqa: BLE001
        return {
            "answer": "",
            "answer_latency_ms": 0,
            "answer_metrics": {"gen_error": f"generate_failed: {e}"[:200]},
        }

    metrics: dict[str, Any] = {
        "em": exact_match(answer, reference),
        "f1": char_f1(answer, reference),
        "numeric_hit": numeric_hit(answer, reference),
    }
    if is_ood:
        if on_stage:
            on_stage("判分（拒答）", 1)
        _merge_judge_error(metrics, judge_refusal(answer, judge_cfg))
    else:
        if on_stage:
            on_stage("判分（正确率+忠实度）", 2)
        futures = [
            _JUDGE_POOL.submit(judge_correctness, question, reference, answer, context, judge_cfg),
            _JUDGE_POOL.submit(judge_faithfulness, context, answer, judge_cfg),
        ]
        for future in futures:
            try:
                _merge_judge_error(metrics, future.result())
            except Exception as e:  # noqa: BLE001
                _merge_judge_error(metrics, {"judge_error": f"judge_pool_failed: {e}"[:200]})
    return {"answer": answer, "answer_latency_ms": latency_ms, "answer_metrics": metrics}