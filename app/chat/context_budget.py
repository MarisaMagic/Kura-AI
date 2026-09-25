"""上下文 token 预算：按模型窗口计算压缩触发点，并用真实 usage 反推校准系数。

设计对齐 Claude Code 的「距上限固定缓冲」思路（而非百分比 / 轮数 / 时间间隔）：

    effective = window - SUMMARY_RESERVE_TOKENS   # 给摘要自身输出留的位置
    trigger   = effective - BUFFER_TOKENS         # 再留一道独立的安全垫

B/S 差异：本项目模型由用户自填 OpenAI 兼容端点，无法假定 tokenizer 可用，
故采用「CJK 感知启发式估算 + 上一轮真实 usage_metadata 反推的乘性修正系数」，
通常 1~2 轮内即收敛到接近真实 token 数。
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage

from app.chat.message_codec import msg_content_to_str
from app.settings import settings

logger = logging.getLogger(__name__)

# 校准系数的合法区间：中文≈1.0~1.8 字符/token，英文≈3.5~4.5 字符/token
_CALIB_FACTOR_MIN = 0.5
_CALIB_FACTOR_MAX = 2.5
_CALIB_EWMA_ALPHA = 0.4
_CALIB_MAX_SAMPLES = 8

# 工具 schema 估算结果的进程内缓存（key = schema JSON 的 hash）
_tools_cache: dict[str, int] = {}
_tools_cache_lock = threading.Lock()
_TOOLS_CACHE_MAX = 64


def _int_setting(name: str, default: int) -> int:
    """读取整型配置，非法值回退默认。"""
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _float_setting(name: str, default: float) -> float:
    """读取浮点配置，非法值回退默认。"""
    try:
        v = float(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def count_cjk(text: str) -> int:
    """统计 CJK 及全角字符数（这些字符在主流 tokenizer 中接近 1 字符 = 1 token）。"""
    n = 0
    for ch in text:
        o = ord(ch)
        if (
            0x3040 <= o <= 0x30FF  # 日文假名
            or 0x3400 <= o <= 0x4DBF  # CJK 扩展 A
            or 0x4E00 <= o <= 0x9FFF  # CJK 基本
            or 0xAC00 <= o <= 0xD7AF  # 韩文音节
            or 0xF900 <= o <= 0xFAFF  # CJK 兼容
            or 0xFF00 <= o <= 0xFFEF  # 全角
            or 0x20000 <= o <= 0x2FA1F  # CJK 扩展 B~F
        ):
            n += 1
    return n


def estimate_tokens(text: str) -> int:
    """启发式 token 估算（未校准的「形状函数」）。

    CJK 字符按 CHAT_TOKENS_PER_CJK_CHAR（默认 1.0）计，其余字符按
    CHAT_CHARS_PER_LATIN_TOKEN（默认 3.6）折算。校准系数另行乘算。
    """
    s = text or ""
    if not s:
        return 0
    cjk = count_cjk(s)
    other = len(s) - cjk
    per_cjk = _float_setting("CHAT_TOKENS_PER_CJK_CHAR", 1.0)
    per_latin = _float_setting("CHAT_CHARS_PER_LATIN_TOKEN", 3.6)
    return int(math.ceil(cjk * per_cjk + other / per_latin))


def messages_chars(messages: list[BaseMessage]) -> int:
    """消息列表的纯文本字符数（多模态块按占位文本计）。"""
    n = 0
    for m in messages or []:
        n += len(msg_content_to_str(getattr(m, "content", "")))
        for tc in getattr(m, "tool_calls", None) or []:
            try:
                n += len(json.dumps(tc, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                n += 64
    return n


def estimate_messages_tokens(messages: list[BaseMessage], *, factor: float = 1.0) -> int:
    """估算消息列表 token 数（含 tool_calls 参数）；factor 为校准后的乘性修正系数。"""
    total = 0
    for m in messages or []:
        total += estimate_tokens(msg_content_to_str(getattr(m, "content", "")))
        for tc in getattr(m, "tool_calls", None) or []:
            try:
                total += estimate_tokens(json.dumps(tc, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                total += 16
    return apply_factor(total, factor)


def estimate_tokens_blind(chars: int) -> int:
    """只知道字符数、不知道 CJK 占比时的保守估算（假定约一半为 CJK）。"""
    n = max(0, int(chars or 0))
    if n == 0:
        return 0
    per_cjk = _float_setting("CHAT_TOKENS_PER_CJK_CHAR", 1.0)
    per_latin = _float_setting("CHAT_CHARS_PER_LATIN_TOKEN", 3.6)
    return int(math.ceil(n * (0.5 * per_cjk + 0.5 / per_latin)))


def tools_schema_tokens(tools: list[Any] | None) -> int:
    """实测工具 schema 的 token 占用（取代旧的固定 8000 字符拍脑袋值）。

    序列化失败或 LangChain 版本不兼容时回退到 CHAT_COMPACT_TOOLS_ESTIMATE_CHARS。
    """
    if not tools:
        return 0
    payloads: list[str] = []
    for t in tools:
        schema: Any = None
        for fn in ("tool_call_schema", "args_schema"):
            obj = getattr(t, fn, None)
            if obj is None:
                continue
            try:
                if hasattr(obj, "model_json_schema"):
                    schema = obj.model_json_schema()
                elif hasattr(obj, "schema"):
                    schema = obj.schema()
                else:
                    schema = obj
                break
            except Exception:  # noqa: BLE001
                schema = None
        try:
            payloads.append(
                json.dumps(
                    {
                        "name": str(getattr(t, "name", "") or ""),
                        "description": str(getattr(t, "description", "") or ""),
                        "parameters": schema,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        except (TypeError, ValueError):
            payloads.append(str(getattr(t, "name", "")))
    blob = "\n".join(payloads)
    key = f"{len(blob)}:{hash(blob)}"
    with _tools_cache_lock:
        hit = _tools_cache.get(key)
        if hit is not None:
            return hit
        if len(_tools_cache) >= _TOOLS_CACHE_MAX:
            _tools_cache.clear()
        val = estimate_tokens(blob) + 8 * len(tools)  # 每工具的结构开销
        _tools_cache[key] = val
        return val


def clear_tools_cache() -> None:
    """清空工具 schema 估算缓存（测试用）。"""
    with _tools_cache_lock:
        _tools_cache.clear()


@dataclass(frozen=True)
class ContextBudget:
    """一次请求可用的 token 预算与各阈值。"""

    window: int
    summary_reserve: int
    buffer: int
    keep_tokens: int
    soft_ratio: float

    @property
    def effective(self) -> int:
        """扣掉摘要输出预留后的可用窗口。"""
        return max(1024, self.window - self.summary_reserve)

    @property
    def trigger(self) -> int:
        """硬触发点：超过就必须压缩（对齐 Claude Code 的 window-20k-13k 双层缓冲）。"""
        return max(self.keep_tokens + 1, self.effective - self.buffer)

    @property
    def soft_trigger(self) -> int:
        """软触发点：后台预压缩阈值，命中即提前算好摘要，下一轮零延迟。"""
        return max(self.keep_tokens + 1, int(self.effective * self.soft_ratio))


def budget_for(context_window: int | None = None) -> ContextBudget:
    """按智能体窗口（缺省用全局默认）构造预算对象。"""
    window = int(context_window or 0)
    if window <= 0:
        window = _int_setting("CHAT_MODEL_CONTEXT_WINDOW_DEFAULT", 128000)
    window = max(4096, window)
    reserve = min(max(1024, _int_setting("CHAT_COMPACT_SUMMARY_RESERVE_TOKENS", 8000)), window // 4)
    buffer = min(max(512, _int_setting("CHAT_COMPACT_BUFFER_TOKENS", 6000)), window // 4)
    keep = max(512, _int_setting("CHAT_COMPACT_KEEP_TOKENS", 8000))
    ratio = min(max(0.1, _float_setting("CHAT_COMPACT_PRECOMPACT_RATIO", 0.6)), 0.95)
    return ContextBudget(
        window=window,
        summary_reserve=reserve,
        buffer=buffer,
        keep_tokens=min(keep, max(512, window // 4)),
        soft_ratio=ratio,
    )


def resolve_window(context_window: Any) -> int:
    """从智能体字段解析窗口大小；空/非法回退全局默认。"""
    try:
        v = int(context_window or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else _int_setting("CHAT_MODEL_CONTEXT_WINDOW_DEFAULT", 128000)


# ---------------------------------------------------------------------------
# 校准：用上一轮真实 usage_metadata.input_tokens 反推启发式的乘性修正系数
# ---------------------------------------------------------------------------


def calibrate_factor(prev: dict | None, *, estimated: int, input_tokens: int, model: str) -> dict | None:
    """用一次真实请求的 (估算值, 真实 input_tokens) 更新校准状态。

    :param prev: 上一份校准状态（None 表示首次）
    :param estimated: 本次请求发送前我们对全部输入（system+tools+messages）的估算 token 数
    :param input_tokens: 服务端回报的真实 input_tokens
    :param model: 模型名，换模型即作废重学
    :return: 新的校准状态字典；样本不可用时返回 prev 原样
    """
    if estimated <= 0 or input_tokens <= 0:
        return prev
    sample = input_tokens / float(estimated)
    if not (_CALIB_FACTOR_MIN <= sample <= _CALIB_FACTOR_MAX):
        logger.debug("token 校准样本越界，忽略: sample=%.3f model=%s", sample, model)
        return prev
    cur = dict(prev) if isinstance(prev, dict) else {}
    if str(cur.get("model") or "") != str(model or ""):
        cur = {"model": str(model or ""), "factor": 1.0, "samples": 0}
    old = float(cur.get("factor") or 1.0)
    n = int(cur.get("samples") or 0)
    alpha = _CALIB_EWMA_ALPHA if n < 3 else _CALIB_EWMA_ALPHA * 0.5
    factor = old * (1 - alpha) + sample * alpha
    factor = min(max(factor, _CALIB_FACTOR_MIN), _CALIB_FACTOR_MAX)
    cur.update(
        {
            "model": str(model or ""),
            "factor": round(factor, 4),
            "samples": min(n + 1, _CALIB_MAX_SAMPLES),
            "last_estimated": int(estimated),
            "last_input_tokens": int(input_tokens),
        }
    )
    return cur


def factor_of(calib: Any, model: str) -> float:
    """取当前模型的校准系数；模型不匹配或无样本时返回 1.0（即纯启发式）。"""
    if not isinstance(calib, dict):
        return 1.0
    if str(calib.get("model") or "") != str(model or ""):
        return 1.0
    try:
        f = float(calib.get("factor") or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return min(max(f, _CALIB_FACTOR_MIN), _CALIB_FACTOR_MAX)


def apply_factor(estimated: int, factor: float) -> int:
    """把校准系数乘到估算值上。"""
    if estimated <= 0:
        return 0
    return int(math.ceil(estimated * (factor if factor > 0 else 1.0)))


def extract_input_tokens(usage: Any) -> int:
    """从 LangChain usage_metadata / OpenAI usage 结构中取 input_tokens。"""
    if not usage:
        return 0
    if isinstance(usage, dict):
        for k in ("input_tokens", "prompt_tokens", "total_tokens"):
            v = usage.get(k)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return 0
    for k in ("input_tokens", "prompt_tokens", "total_tokens"):
        v = getattr(usage, k, None)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0
