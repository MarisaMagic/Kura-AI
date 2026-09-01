"""联网搜索通用权威度：结构信号 + 查询对齐，与 rerank 软融合。

不维护品牌/媒体/品类域名表，只使用跨领域成立的主机与路径特征。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlparse

from app.settings import settings

_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")
_DIGIT_RUN_RE = re.compile(r"\d{4,}")
_DIGIT_GROUP_RE = re.compile(r"\d+")

# 常见双段公共后缀；gov.cn / edu.cn / ac.cn 同时用于权威后缀判断
_MULTI_PART_SUFFIX = frozenset(
    {
        "co.uk",
        "ac.uk",
        "gov.uk",
        "org.uk",
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "ac.cn",
        "com.hk",
        "com.tw",
        "co.jp",
        "ne.jp",
        "or.jp",
        "ac.jp",
        "com.au",
        "net.au",
        "org.au",
    }
)
_AUTHORITY_SUFFIXES = (
    ".gov.cn",
    ".edu.cn",
    ".ac.cn",
    ".gov",
    ".edu",
    ".mil",
)
_TEMPORAL_INTENT_RE = re.compile(
    r"今天|今日|tonight|today|本周|近一周|这一周|上周|this\s*week|"
    r"本月|近一月|近一个月|这个月|this\s*month|今年|近一年|this\s*year|"
    r"最新|近期|最近|recent|latest",
    re.IGNORECASE,
)


def _extract_query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    for run in _CJK_TOKEN_RE.findall(query or ""):
        tokens.append(run)
        if len(run) >= 2:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) >= 3:
            tokens.extend(run[i : i + 3] for i in range(len(run) - 2))
    tokens.extend(_LATIN_TOKEN_RE.findall(query or ""))
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        key = t.casefold()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _host_from_url(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def registrable_domain(host: str) -> str:
    """简单 eTLD：默认后两段；co.uk / gov.cn 等取后三段。"""
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    parts = [p for p in h.split(".") if p]
    if len(parts) < 2:
        return h
    last2 = ".".join(parts[-2:])
    if last2 in _MULTI_PART_SUFFIX and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def _path_depth(url: str) -> int:
    try:
        path = urlparse(url).path or "/"
    except ValueError:
        return 0
    segs = [s for s in path.split("/") if s]
    return len(segs)


def _query_alignment(query: str, item: dict, host: str, domain: str) -> float:
    tokens = _extract_query_tokens(query)
    if not tokens:
        return 0.0
    site = (item.get("siteName") or "").strip()
    title = (item.get("title") or "").strip()
    host_blob = host.replace(".", "").replace("-", "")
    domain_blob = domain.replace(".", "").replace("-", "")
    blob = f"{host}\n{domain}\n{host_blob}\n{domain_blob}\n{site}\n{title}"
    blob_cf = blob.casefold()
    hits = 0
    for t in tokens:
        if t.isascii():
            if t.casefold() in blob_cf:
                hits += 1
        elif t in blob:
            hits += 1
    return min(1.0, hits / min(len(tokens), 6))


def authority_score(query: str, item: dict) -> float:
    """单条 0–1 权威度，仅用结构与查询对齐信号。"""
    url = (item.get("url") or "").strip()
    host = _host_from_url(url)
    domain = registrable_domain(host)
    score = 0.45

    host_or_domain = f".{host}" if host else ""
    if any(host_or_domain.endswith(sfx) or (domain and f".{domain}".endswith(sfx)) for sfx in _AUTHORITY_SUFFIXES):
        score += 0.25
    try:
        if urlparse(url).scheme == "https":
            score += 0.08
    except ValueError:
        pass
    if _path_depth(url) <= 1:
        score += 0.07

    score += 0.25 * _query_alignment(query, item, host, domain)

    labels = [p for p in host.split(".") if p]
    host_core = ".".join(labels[:-2]) if len(labels) >= 2 else host
    if len(host) > 30:
        score -= 0.08
    if host_core.count("-") >= 3:
        score -= 0.12
    if _DIGIT_RUN_RE.search(host) or len(_DIGIT_GROUP_RE.findall(host)) >= 3:
        score -= 0.10

    return max(0.0, min(1.0, score))


def _query_has_temporal_intent(query: str) -> bool:
    return bool(_TEMPORAL_INTENT_RE.search(query or ""))


def _should_apply_recency(query: str, freshness: str | None) -> bool:
    f = (freshness or "").strip()
    if f and f != "noLimit":
        return True
    return _query_has_temporal_intent(query)


def _parse_published_date(raw: object) -> date | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    s = s[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def recency_score(item: dict) -> float:
    """有日期：越近越高；超过一年明显降低。无日期：0.5。"""
    d = _parse_published_date(item.get("datePublished"))
    if d is None:
        return 0.5
    days = (datetime.now().date() - d).days
    if days < 0:
        days = 0
    if days <= 7:
        return 1.0
    if days <= 30:
        return 0.85
    if days <= 90:
        return 0.65
    if days <= 365:
        return 0.35
    return 0.15


def apply_authority_ranking(
    query: str,
    results: list[dict],
    *,
    freshness: str | None = None,
) -> list[dict]:
    """
    与 rerank（或召回位次）及可选 recency 软融合后按 final 排序，截断到 MAX_RESULTS。
    权威度与 recency 均可独立关闭；两者都关时仅截断。
    """
    top_k = max(1, int(getattr(settings, "WEB_SEARCH_MAX_RESULTS", 5) or 5))
    if not results:
        return []
    auth_on = bool(getattr(settings, "WEB_SEARCH_AUTHORITY_ENABLED", True))
    recency_on = _should_apply_recency(query, freshness)
    auth_blend = float(getattr(settings, "WEB_SEARCH_AUTHORITY_BLEND", 0.25) or 0.0) if auth_on else 0.0
    recency_blend = (
        float(getattr(settings, "WEB_SEARCH_RECENCY_BLEND", 0.15) or 0.0) if recency_on else 0.0
    )
    auth_blend = max(0.0, min(1.0, auth_blend))
    recency_blend = max(0.0, min(1.0, recency_blend))
    extra = auth_blend + recency_blend
    if extra > 0.9:
        scale = 0.9 / extra
        auth_blend *= scale
        recency_blend *= scale
    rel_w = 1.0 - auth_blend - recency_blend
    if not auth_on and not recency_on:
        return results[:top_k]

    n = len(results)
    scored: list[dict] = []
    for i, raw in enumerate(results):
        item = dict(raw)
        auth = authority_score(query, item) if auth_on else 0.0
        rec = recency_score(item) if recency_on else 0.5
        item["authority_score"] = auth
        item["recency_score"] = rec
        rel = item.get("rerank_score")
        try:
            rel_f = float(rel) if rel is not None else (n - i) / n
        except (TypeError, ValueError):
            rel_f = (n - i) / n
        rel_f = max(0.0, min(1.0, rel_f))
        item["final_score"] = rel_w * rel_f + auth_blend * auth + recency_blend * rec
        scored.append(item)
    scored.sort(key=lambda x: float(x.get("final_score") or 0.0), reverse=True)
    return scored[:top_k]
