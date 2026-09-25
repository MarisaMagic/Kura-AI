"""长期记忆写入前的价值闸门：只把值得记住的轮次/事实送进向量库。

为什么需要：把每个离开窗口的轮次原文都嵌入入库，向量库会迅速堆满
「好的」「谢谢」「未找到相关资料」这类零信息量内容，既占空间又污染召回。
闸门的原则是**宁可少存**——被拦下的轮次信息并不会丢：
它们仍会进入压缩摘要的输入（episodic 段），且原文永远 append-only 留在 PG，
可用 read_session_history 精确翻牌。
"""

from __future__ import annotations

import re

from app.settings import settings

# 纯寒暄/确认：整条消息只由这些词构成时判为无价值
_GREETING_RE = re.compile(
    r"^(?:"
    r"好的?|好嘞|好滴|嗯+|哦+|噢+|啊+|行|可以|中|收到|明白|了解了?|懂了?|知道了?"
    r"|谢谢|多谢|感谢|辛苦了?|麻烦了?"
    r"|继续|接着说|go\s*on|continue"
    r"|ok+|okay|k|thx|thanks|thank\s*you|nice|cool|great|good"
    r"|你好|您好|hi|hello|hey|在吗|在么|有人吗|测试|test"
    r"|哈+|嘿+|呵+|嘻+"
    r")[\s!！。.~～,，、?？]*$",
    re.IGNORECASE,
)

# 明确的长期偏好/约束触发词：这类信息价值最高，优先入库
_PREFERENCE_TRIGGERS = (
    "记住",
    "记一下",
    "以后都",
    "以后请",
    "今后",
    "下次",
    "不要再",
    "别再用",
    "不要再犯",
    "我偏好",
    "我更喜欢",
    "我喜欢",
    "我习惯",
    "我要求",
    "必须用",
    "默认用",
    "统一用",
    "always",
    "never",
    "remember",
    "preference",
    "from now on",
)

# 关键实体线索：文件名、URL、file_key、长数字/编号、带扩展名的标识符
_ENTITY_RE = re.compile(
    r"(?:file_key|attachment_id|https?://\S+|\b\d{4,}\b|[A-Za-z_][A-Za-z0-9_]{2,}\.[A-Za-z0-9]{1,8})",
    re.IGNORECASE,
)

_CODE_MARKERS = ("```", "<code", "    def ", "    return ")

# 指纹归一化：只保留中日韩文字与字母数字，其余（空白、标点、符号）全部剥掉
_NORMALIZE_RE = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+")


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _float_setting(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def is_greeting(text: str) -> bool:
    """整条消息是否只是寒暄/确认。"""
    s = (text or "").strip()
    if not s:
        return True
    return bool(_GREETING_RE.match(s))


def has_preference_trigger(text: str) -> bool:
    """是否出现长期偏好/约束类触发词。"""
    low = (text or "").lower()
    return any(t in low for t in _PREFERENCE_TRIGGERS)


def has_code(text: str) -> bool:
    s = text or ""
    return any(m in s for m in _CODE_MARKERS)


def has_entity(text: str) -> bool:
    return bool(_ENTITY_RE.search(text or ""))


def score_turn(
    user_text: str,
    assistant_text: str,
    *,
    has_error: bool = False,
) -> tuple[float, list[str]]:
    """给一轮对话打价值分（0~1）并给出判据。

    :param has_error: 该轮助手消息是否生成失败（error_text 非空）
    """
    reasons: list[str] = []
    u = (user_text or "").strip()
    a = (assistant_text or "").strip()

    if has_error:
        return 0.0, ["error_turn"]
    if not u and not a:
        return 0.0, ["empty"]

    min_user = max(1, _int_setting("CHAT_MEMORY_MIN_USER_CHARS", 8))
    if len(u) < min_user and len(a) < 40:
        return 0.0, ["too_short"]
    if is_greeting(u) and len(a) < 120:
        return 0.05, ["greeting"]

    score = 0.3
    reasons.append("base")
    if len(u) >= 30 or len(a) >= 200:
        score += 0.25
        reasons.append("substantial_length")
    if has_code(u) or has_code(a):
        score += 0.2
        reasons.append("code_block")
    if has_entity(u) or has_entity(a):
        score += 0.15
        reasons.append("entity_or_number")
    if has_preference_trigger(u):
        score += 0.25
        reasons.append("preference_trigger")
    if len(a) >= 600:
        score += 0.1
        reasons.append("detailed_answer")
    return min(1.0, round(score, 3)), reasons


def passes_value_gate(user_text: str, assistant_text: str, *, has_error: bool = False) -> tuple[bool, float, list[str]]:
    """是否值得单独入库。未过闸门的轮次仍会进入压缩摘要输入，信息不丢。"""
    score, reasons = score_turn(user_text, assistant_text, has_error=has_error)
    threshold = min(max(0.0, _float_setting("CHAT_MEMORY_MIN_VALUE_SCORE", 0.25)), 1.0)
    return score >= threshold, score, reasons


# ---------------------------------------------------------------------------
# 近重复检测：用户反复问同一件事时不要把向量库灌满同一份内容
# ---------------------------------------------------------------------------


def shingles(text: str, n: int = 8) -> frozenset[str]:
    """字符 n-gram 指纹集合（中文友好，无需分词）。

    先剥掉标点与符号再切 gram：字符 n-gram 对插入极其敏感——
    句首多一个「。」就会让其后的每个 gram 全部错位，
    导致「同一句话多打个句号」的相似度掉到 0.79 而漏判为不重复。
    标点不承载话题信息，剥掉后指纹才具备位移不变性。
    """
    s = _NORMALIZE_RE.sub("", (text or "").lower())
    if len(s) < n:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i : i + n] for i in range(len(s) - n + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def is_near_duplicate(text: str, fingerprints: list[frozenset[str]], *, threshold: float | None = None) -> bool:
    """与近期已归档轮次指纹比对，判断是否近重复。

    :param fingerprints: 近期已归档轮次的 shingles 列表（有界窗口，避免 O(n²) 全量比对）
    """
    th = _float_setting("CHAT_MEMORY_DEDUP_JACCARD", 0.85) if threshold is None else float(threshold)
    if not fingerprints:
        return False
    cur = shingles(text)
    if not cur:
        return False
    return any(jaccard(cur, fp) >= th for fp in fingerprints)


def dedup_window_size() -> int:
    """参与近重复比对的近期指纹条数上限。"""
    return max(1, _int_setting("CHAT_MEMORY_DEDUP_WINDOW", 20))
