"""联网搜索工具。

Provider：
- bocha：博查 Web Search JSON API（国内无代理主路径，需 WEB_SEARCH_BOCHA_API_KEY）
- ddgs：ddgs 库（有 WEB_SEARCH_PROXY 时优先；无代理默认 bing backend）
- bing_html：Bing 国内版 HTML 解析（最后兜底）
- auto（默认）：无代理且已配 Key 时 bocha → bing_html；有代理时 ddgs → bocha → bing_html

召回后可选走博查 Semantic Reranker、通用权威度软加权，以及对 top 结果读页抽主文。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    fetch_url_disabled_this_turn_msg,
    get_last_rag_context,
    is_web_search_allowed_this_turn,
    log_kb_tool_return_to_terminal,
    try_acquire_fetch_url_tool_slot,
    try_acquire_web_image_search_tool_slot,
    try_acquire_web_search_tool_slot,
    web_image_search_disabled_this_turn_msg,
    web_search_disabled_this_turn_msg,
)
from app.chat.web_search_authority import apply_authority_ranking, authority_score, registrable_domain
from app.chat.web_search_reader import fetch_page, read_top_pages
from app.settings import settings
from app.utils.content_guard import guard_untrusted_content
from app.utils.ssrf import is_safe_http_page_url, is_safe_https_image_url

logger = logging.getLogger(__name__)

_BING_HOME_URL = "https://cn.bing.com/"
_BING_SEARCH_URL = "https://cn.bing.com/search"
_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://cn.bing.com/",
}
_DEFAULT_BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"
_DEFAULT_RERANK_ENDPOINT = "https://api.bochaai.com/v1/rerank"
_FRESHNESS_VALUES = frozenset({"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"})
_RERANK_DOC_MAX_CHARS = 1000
_DICT_JUNK_RE = re.compile(
    r"字的来源|组词|拼音|的意思|释义|单词|词汇|词典|definition|meaning of",
    re.IGNORECASE,
)
# 更具体的时间意图在前
_FRESHNESS_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("oneDay", re.compile(r"今天|今日|tonight|today", re.IGNORECASE)),
    ("oneWeek", re.compile(r"本周|近一周|这一周|上周|this\s*week", re.IGNORECASE)),
    ("oneMonth", re.compile(r"本月|近一月|近一个月|这个月|this\s*month", re.IGNORECASE)),
    ("oneYear", re.compile(r"今年|近一年|this\s*year", re.IGNORECASE)),
    ("oneMonth", re.compile(r"最新|近期|最近|recent|latest", re.IGNORECASE)),
)

FreshnessLiteral = Literal["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"]

from app.chat.web_search_providers import (  # noqa: E402
    _bocha_api_key,
    _format_published,
    _web_search_proxy,
)

def _rerank_enabled() -> bool:
    if not bool(getattr(settings, "WEB_SEARCH_RERANK_ENABLED", True)):
        return False
    return bool(_bocha_api_key())


def _build_rerank_document(item: dict) -> str:
    lines = [f"标题: {(item.get('title') or '').strip()}"]
    site = (item.get("siteName") or "").strip()
    if site:
        lines.append(f"站点: {site}")
    published = _format_published(item.get("datePublished"))
    if published:
        lines.append(f"发布: {published}")
    body = (item.get("snippet") or "").strip()
    if body:
        lines.append(body)
    return "\n".join(lines)[:_RERANK_DOC_MAX_CHARS]


def _parse_rerank_results(payload: object) -> list[dict]:
    """兼容 data.results / results。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    raw = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _rerank_web_results(query: str, results: list[dict]) -> tuple[list[dict], dict]:
    """
    博查 Semantic Reranker；失败敞开（原序截断）。
    :return: (ranked_or_fallback, meta)
    """
    meta: dict = {"applied": False, "error": None, "fallback": False}
    if not results:
        return [], meta
    if not _rerank_enabled():
        return results, meta

    import httpx

    endpoint = (
        (getattr(settings, "WEB_SEARCH_RERANK_ENDPOINT", "") or "").strip()
        or _DEFAULT_RERANK_ENDPOINT
    )
    model = (getattr(settings, "WEB_SEARCH_RERANK_MODEL", "") or "").strip() or "gte-rerank"
    timeout = max(3, int(getattr(settings, "WEB_SEARCH_RERANK_TIMEOUT_SECONDS", 10) or 10))
    min_score = float(getattr(settings, "WEB_SEARCH_RERANK_MIN_SCORE", 0.2) or 0.0)

    try:
        documents = [_build_rerank_document(item) for item in results]
        payload = {
            "model": model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "return_documents": False,
        }
        client_kwargs: dict = {"timeout": timeout, "follow_redirects": True}
        proxy = _web_search_proxy()
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {_bocha_api_key()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
        if isinstance(body, dict):
            code = body.get("code")
            if code not in (None, 200, 0, "200", "0"):
                msg = body.get("msg") or body.get("message") or ""
                raise RuntimeError(f"博查重排失败 code={code} {msg}".strip())
        ranked_raw = _parse_rerank_results(body)
        if not ranked_raw:
            raise RuntimeError("empty_rerank_results")
        kept: list[dict] = []
        for row in ranked_raw:
            idx = row.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(results):
                continue
            score = row.get("relevance_score")
            try:
                score_f = float(score)
            except (TypeError, ValueError):
                continue
            if score_f < min_score:
                continue
            item = dict(results[idx])
            item["rerank_score"] = score_f
            kept.append(item)
        meta["applied"] = True
        return kept, meta
    except Exception as e:
        logger.warning("博查 rerank 失败，按召回原序交给后续排序: %s", e)
        meta["error"] = str(e)[:300]
        meta["fallback"] = True
        return results, meta


