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
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from urllib.parse import urlparse

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    fetch_url_disabled_this_turn_msg,
    is_web_search_allowed_this_turn,
    log_kb_tool_return_to_terminal,
    try_acquire_fetch_url_tool_slot,
    try_acquire_web_search_tool_slot,
    web_search_disabled_this_turn_msg,
)
from app.chat.web_search_authority import apply_authority_ranking
from app.chat.web_search_reader import fetch_page, read_top_pages
from app.settings import settings
from app.utils.content_guard import guard_untrusted_content

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


def _web_search_proxy() -> str | None:
    return (getattr(settings, "WEB_SEARCH_PROXY", "") or "").strip() or None


def _bocha_api_key() -> str:
    return (getattr(settings, "WEB_SEARCH_BOCHA_API_KEY", "") or "").strip()


def _normalize_ddgs_region(raw: str) -> str:
    """ddgs 使用 {country}-{language}；兼容旧值 zh-cn。"""
    s = (raw or "").strip() or "cn-zh"
    parts = s.replace("_", "-").split("-")
    if len(parts) == 2 and parts[0].lower() == "zh" and parts[1].lower() == "cn":
        return "cn-zh"
    return s


def _resolve_ddgs_backend() -> str:
    explicit = (getattr(settings, "WEB_SEARCH_DDGS_BACKEND", "") or "").strip()
    if explicit:
        return explicit
    return "auto" if _web_search_proxy() else "bing"


def _candidate_count() -> int:
    n = int(getattr(settings, "WEB_SEARCH_CANDIDATE_COUNT", 15) or 15)
    return max(1, min(50, n))


def _normalize_site(raw: str | None) -> str:
    """去掉协议、路径、www.，得到主机名。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    try:
        host = (urlparse(s).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _query_with_site(query: str, site: str) -> str:
    if not site:
        return query
    needle = f"site:{site}".casefold()
    if needle in (query or "").casefold():
        return query
    return f"{query} site:{site}"


def _is_dictionary_junk(title: str) -> bool:
    return bool(_DICT_JUNK_RE.search(title or ""))


def _drop_dictionary_junk(results: list[dict]) -> list[dict]:
    return [item for item in results if not _is_dictionary_junk(item.get("title") or "")]


def _dedupe_by_url(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in results:
        url = (item.get("url") or "").strip()
        key = url.rstrip("/").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _prepare_candidates(raw: list[dict]) -> list[dict]:
    return _dedupe_by_url(_drop_dictionary_junk(raw))


def _format_published(raw: object) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if "T" in s:
        return s.split("T", 1)[0]
    return s[:10] if len(s) >= 10 else s


def _normalize_freshness(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s in _FRESHNESS_VALUES:
        return s
    folded = s.replace("_", "").replace("-", "").lower()
    aliases = {
        "nolimit": "noLimit",
        "oneday": "oneDay",
        "oneweek": "oneWeek",
        "onemonth": "oneMonth",
        "oneyear": "oneYear",
    }
    return aliases.get(folded)


def _infer_freshness_from_query(query: str) -> str:
    q = query or ""
    for value, pat in _FRESHNESS_INTENT_PATTERNS:
        if pat.search(q):
            return value
    return "noLimit"


def _resolve_freshness(query: str, explicit: str | None) -> str:
    """模型显式 freshness > 固定配置 > auto 按时间意图词。"""
    from_tool = _normalize_freshness(explicit)
    if from_tool:
        return from_tool
    configured = (getattr(settings, "WEB_SEARCH_FRESHNESS", "auto") or "auto").strip()
    if configured and configured.lower() != "auto":
        normalized = _normalize_freshness(configured)
        if normalized:
            return normalized
        logger.warning("WEB_SEARCH_FRESHNESS=%s 无效，回退 auto", configured)
    return _infer_freshness_from_query(query)


def _parse_bocha_pages(payload: object) -> list[dict]:
    """兼容 data.webPages.value / webPages.value / data.value。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    pages = data.get("webPages") if isinstance(data, dict) else None
    if not isinstance(pages, dict):
        pages = payload.get("webPages") if isinstance(payload.get("webPages"), dict) else {}
    values = pages.get("value") if isinstance(pages, dict) else None
    if not isinstance(values, list):
        raw = data.get("value") if isinstance(data, dict) else None
        values = raw if isinstance(raw, list) else []
    out: list[dict] = []
    for r in values:
        if not isinstance(r, dict):
            continue
        title = (r.get("name") or r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("summary") or r.get("snippet") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        site = (r.get("siteName") or r.get("site_name") or "").strip()
        published = _format_published(r.get("datePublished") or r.get("date_published"))
        out.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "siteName": site,
                "datePublished": published,
            }
        )
    return out


def _search_with_bocha(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str = "noLimit",
    include: str = "",
) -> list[dict]:
    """博查 Web Search；返回 [{title, url, snippet, siteName, datePublished}]，失败抛异常。"""
    import httpx

    key = _bocha_api_key()
    if not key:
        raise RuntimeError("未配置 WEB_SEARCH_BOCHA_API_KEY")
    endpoint = (
        (getattr(settings, "WEB_SEARCH_BOCHA_ENDPOINT", "") or "").strip()
        or _DEFAULT_BOCHA_ENDPOINT
    )
    count = max(1, min(50, int(max_results)))
    used_freshness = _normalize_freshness(freshness) or "noLimit"
    client_kwargs: dict = {"timeout": timeout, "follow_redirects": True}
    proxy = _web_search_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy
    body: dict = {
        "query": query,
        "count": count,
        "summary": True,
        "freshness": used_freshness,
    }
    if include:
        body["include"] = include
    with httpx.Client(**client_kwargs) as client:
        resp = client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        payload = resp.json()
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 200, 0, "200", "0"):
            msg = payload.get("msg") or payload.get("message") or ""
            raise RuntimeError(f"博查搜索失败 code={code} {msg}".strip())
    return _parse_bocha_pages(payload)


def _search_bocha_with_freshness_fallback(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str,
    include: str = "",
) -> tuple[list[dict], str]:
    """先按 freshness 搜；0 条且非 noLimit 时再搜 noLimit。"""
    used = _normalize_freshness(freshness) or "noLimit"
    results = _prepare_candidates(
        _search_with_bocha(query, max_results, timeout, used, include=include)
    )
    if results or used == "noLimit":
        return results, used
    logger.info("博查 freshness=%s 无结果，回退 noLimit", used)
    results = _prepare_candidates(
        _search_with_bocha(query, max_results, timeout, "noLimit", include=include)
    )
    return results, "noLimit"


def _search_with_ddgs(query: str, max_results: int, timeout: int) -> list[dict]:
    """ddgs 搜索；返回 [{title, url, snippet}]，失败抛异常。"""
    from ddgs import DDGS

    proxy = _web_search_proxy()
    backend = _resolve_ddgs_backend()
    region = _normalize_ddgs_region(str(getattr(settings, "WEB_SEARCH_REGION", "cn-zh")))
    kwargs: dict = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    with DDGS(**kwargs) as ddgs:
        results = list(
            ddgs.text(
                query,
                region=region,
                safesearch="moderate",
                max_results=max(1, min(50, int(max_results))),
                backend=backend,
            )
            or []
        )
    return [
        {
            "title": (r.get("title") or "").strip(),
            "url": (r.get("href") or "").strip(),
            "snippet": (r.get("body") or "").strip(),
            "siteName": "",
            "datePublished": "",
        }
        for r in results
    ]


def _search_with_bing(query: str, max_results: int, timeout: int) -> list[dict]:
    """Bing 国内版 HTML 搜索解析（cn.bing.com）；返回 [{title, url, snippet}]，失败抛异常。"""
    import httpx
    from bs4 import BeautifulSoup

    proxy = _web_search_proxy()
    client_kwargs: dict = {
        "headers": _BING_HEADERS,
        "timeout": timeout,
        "follow_redirects": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    fetch_n = max(1, min(50, int(max_results)))
    with httpx.Client(**client_kwargs) as client:
        try:
            client.get(_BING_HOME_URL)
        except Exception:
            logger.debug("Bing 首页 cookie 预热失败", exc_info=True)
        resp = client.get(
            _BING_SEARCH_URL,
            params={
                "q": query,
                "mkt": "zh-CN",
                "setlang": "zh-hans",
                "cc": "CN",
                "ensearch": "0",
            },
        )
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        url = (a.get("href") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        title = a.get_text(strip=True)
        if _is_dictionary_junk(title):
            continue
        cap = li.select_one(".b_caption p")
        out.append(
            {
                "title": title,
                "url": url,
                "snippet": cap.get_text(strip=True) if cap else "",
                "siteName": "",
                "datePublished": "",
            }
        )
        if len(out) >= fetch_n:
            break
    return out


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

    try:
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


def _run_search(query: str, freshness: str, site: str = "") -> tuple[list[dict], str, str]:
    """
    按 WEB_SEARCH_PROVIDER 调度搜索，返回召回候选（未截断到 MAX_RESULTS）。
    auto：有代理时 ddgs → bocha → bing_html；无代理且有 Key 时 bocha → bing_html。
    :return: (results, provider_used, freshness_used)
    """
    provider = (getattr(settings, "WEB_SEARCH_PROVIDER", "auto") or "auto").strip().lower()
    candidate_n = _candidate_count()
    timeout = int(settings.WEB_SEARCH_TIMEOUT_SECONDS)
    q_site = _query_with_site(query, site)

    def _try_ddgs() -> tuple[list[dict], str, str] | None:
        try:
            results = _prepare_candidates(_search_with_ddgs(q_site, candidate_n, timeout))
            if results:
                return results, "ddgs", freshness
            logger.info("ddgs 结果经 junk/去重后为空")
        except Exception as e:
            logger.warning("ddgs 搜索失败 backend=%s: %s", _resolve_ddgs_backend(), e)
        return None

    def _try_bocha() -> tuple[list[dict], str, str] | None:
        if not _bocha_api_key():
            return None
        try:
            results, used = _search_bocha_with_freshness_fallback(
                query, candidate_n, timeout, freshness, include=site
            )
            if results:
                return results, "bocha", used
            logger.info("博查结果经 junk/去重后为空")
        except Exception as e:
            logger.warning("博查搜索失败: %s", e)
        return None

    if provider == "bing_html":
        return (
            _prepare_candidates(_search_with_bing(q_site, candidate_n, timeout)),
            "bing_html",
            freshness,
        )
    if provider == "ddgs":
        return (
            _prepare_candidates(_search_with_ddgs(q_site, candidate_n, timeout)),
            "ddgs",
            freshness,
        )
    if provider == "bocha":
        results, used = _search_bocha_with_freshness_fallback(
            query, candidate_n, timeout, freshness, include=site
        )
        return results, "bocha", used

    has_proxy = bool(_web_search_proxy())
    has_bocha = bool(_bocha_api_key())

    if has_proxy:
        hit = _try_ddgs()
        if hit:
            return hit
        hit = _try_bocha()
        if hit:
            return hit
        return (
            _prepare_candidates(_search_with_bing(q_site, candidate_n, timeout)),
            "bing_html",
            freshness,
        )

    if has_bocha:
        hit = _try_bocha()
        if hit:
            return hit
        return (
            _prepare_candidates(_search_with_bing(q_site, candidate_n, timeout)),
            "bing_html",
            freshness,
        )

    logger.warning(
        "未配置 WEB_SEARCH_BOCHA_API_KEY，无代理时回退 ddgs bing + bing_html；"
        "申请 Key: https://open.bochaai.com/"
    )
    hit = _try_ddgs()
    if hit:
        return hit
    return (
        _prepare_candidates(_search_with_bing(q_site, candidate_n, timeout)),
        "bing_html",
        freshness,
    )


def _run_searches(
    queries: list[str],
    freshness: str,
    site: str = "",
) -> tuple[list[dict], str, str]:
    """最多两路并行召回，URL 去重后截到 50；一路失败不拖垮另一路。"""
    qs = [q.strip() for q in queries if (q or "").strip()]
    seen_q: set[str] = set()
    uniq: list[str] = []
    for q in qs:
        key = q.casefold()
        if key not in seen_q:
            seen_q.add(key)
            uniq.append(q)
    uniq = uniq[:2]
    if not uniq:
        return [], "", freshness
    if len(uniq) == 1:
        return _run_search(uniq[0], freshness, site)

    merged: list[dict] = []
    provider = ""
    used_fresh = freshness
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_run_search, q, freshness, site) for q in uniq]
        for fut in futs:
            try:
                results, prov, fr = fut.result()
            except Exception as e:
                logger.warning("一路联网搜索失败: %s", e)
                continue
            if results:
                merged.extend(results)
                provider = provider or prov
                used_fresh = fr
    return _dedupe_by_url(merged)[:50], provider or "none", used_fresh


def _format_result_block(index: int, item: dict) -> str:
    title = (item.get("title") or "").strip() or "(无标题)"
    url = (item.get("url") or "").strip()
    snippet = (item.get("snippet") or "").strip()
    lines = [f"[{index}] {title}"]
    site = (item.get("siteName") or "").strip()
    if site:
        lines.append(f"站点: {site}")
    published = _format_published(item.get("datePublished"))
    if published:
        lines.append(f"发布: {published}")
    lines.append(f"URL: {url}")
    lines.append(f"摘要: {snippet}")
    page_text = (item.get("page_text") or "").strip()
    if page_text:
        lines.append(f"正文摘录: {page_text}")
    return "\n".join(lines)


def make_web_search_tool() -> StructuredTool:
    """构建 web_search 工具：返回编号化的标题/URL/摘要列表，并收集来源供前端展示。"""

    class _WebSearchArgs(BaseModel):
        query: str = Field(
            description=(
                "搜索查询词，应与用户意图一致；建议精炼为关键词或短句（中英文均可）。"
                "涉及时效时可在 query 中带上年份或月份。"
            )
        )
        freshness: FreshnessLiteral | None = Field(
            default=None,
            description=(
                "可选时间范围：noLimit / oneDay / oneWeek / oneMonth / oneYear。"
                "用户问题涉及时效（今天、本周、最新、近期等）时应设置；不涉及则省略。"
            ),
        )
        site: str | None = Field(
            default=None,
            description=(
                "可选：限定检索的主机或注册域（如 moe.gov.cn）。"
                "需要只看某一站点时填写；不要带协议或路径。"
            ),
        )
        extra_query: str | None = Field(
            default=None,
            description=(
                "可选：同一意图的第二种检索说法，与 query 并行搜索后合并。"
                "不要为同一意图再单独调用一次 web_search。"
            ),
        )

        @field_validator("freshness", mode="before")
        @classmethod
        def _coerce_freshness(cls, v):
            if v is None or (isinstance(v, str) and not str(v).strip()):
                return None
            return _normalize_freshness(str(v))

    def _web_search(
        query: str,
        freshness: str | None = None,
        site: str | None = None,
        extra_query: str | None = None,
    ) -> str:
        if not is_web_search_allowed_this_turn():
            limit_msg = web_search_disabled_this_turn_msg()
            log_kb_tool_return_to_terminal(limit_msg, tool_label="web_search")
            return limit_msg

        if not try_acquire_web_search_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: web_search has reached the per-turn call limit. "
                "Use the existing search results above to compose the final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="web_search")
            return limit_msg

        q = (query or "").strip()
        if not q:
            return "错误：query 为空。"

        resolved_freshness = _resolve_freshness(q, freshness)
        site_host = _normalize_site(site)
        extra = (extra_query or "").strip()
        detail = f"{q}（freshness={resolved_freshness}"
        if site_host:
            detail += f"，site={site_host}"
        if extra:
            detail += "，双查询"
        detail += "）"
        emit_rag_step("🌐", "联网搜索", detail)
        try:
            results, provider_used, freshness_used = _run_searches(
                [q, extra], resolved_freshness, site_host
            )
        except Exception as e:
            emit_rag_step("⚠️", "联网搜索失败", str(e)[:200])
            _set_last_rag_context({"web_sources": []})
            err_msg = (
                f"联网搜索出错：{e}。请如实告知用户本次联网搜索失败、可稍后重试；"
                "不得编造任何搜索结果或来源链接。"
            )
            log_kb_tool_return_to_terminal(err_msg, tool_label="web_search")
            return err_msg

        emit_rag_step(
            "🔍",
            "联网搜索召回",
            f"{len(results)} 条（{provider_used}，freshness={freshness_used}）",
        )

        if not results:
            emit_rag_step("🔍", "联网搜索无结果", q)
            _set_last_rag_context({"web_sources": []})
            empty_msg = (
                "WEB_SEARCH_NO_RESULTS: 本次联网搜索未找到相关结果。"
                "请如实告知用户未搜到相关内容（可建议换个关键词重试），不得编造搜索结果或来源链接。"
            )
            log_kb_tool_return_to_terminal(empty_msg, tool_label="web_search")
            return empty_msg

        results, rerank_meta = _rerank_web_results(q, results)
        if rerank_meta.get("fallback"):
            emit_rag_step(
                "⚠️",
                "重排失败，已按原序",
                (rerank_meta.get("error") or "")[:200],
            )
        elif rerank_meta.get("applied"):
            emit_rag_step("📑", "重排后保留", f"{len(results)} 条（{provider_used}）")
        else:
            emit_rag_step("📑", "联网搜索完成", f"命中 {len(results)} 条结果（{provider_used}）")

        if not results:
            emit_rag_step("🔍", "联网搜索无结果", "重排后无高于阈值的结果")
            _set_last_rag_context({"web_sources": []})
            empty_msg = (
                "WEB_SEARCH_NO_RESULTS: 本次联网搜索未找到相关结果。"
                "请如实告知用户未搜到相关内容（可建议换个关键词重试），不得编造搜索结果或来源链接。"
            )
            log_kb_tool_return_to_terminal(empty_msg, tool_label="web_search")
            return empty_msg

        results = apply_authority_ranking(q, results, freshness=freshness_used)
        emit_rag_step("⚖️", "权威度重排完成", f"保留 {len(results)} 条")

        results, read_meta = read_top_pages(results)
        if read_meta.get("enabled"):
            emit_rag_step(
                "📖",
                "读页",
                f"成功 {read_meta.get('ok', 0)}/{read_meta.get('attempted', 0)}",
            )

        if not results:
            emit_rag_step("🔍", "联网搜索无结果", q)
            _set_last_rag_context({"web_sources": []})
            empty_msg = (
                "WEB_SEARCH_NO_RESULTS: 本次联网搜索未找到相关结果。"
                "请如实告知用户未搜到相关内容（可建议换个关键词重试），不得编造搜索结果或来源链接。"
            )
            log_kb_tool_return_to_terminal(empty_msg, tool_label="web_search")
            return empty_msg

        web_sources: list[dict] = []
        blocks: list[str] = []
        for i, item in enumerate(results, start=1):
            title = (item.get("title") or "").strip() or "(无标题)"
            url = (item.get("url") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            site = (item.get("siteName") or "").strip()
            published = _format_published(item.get("datePublished"))
            src: dict = {
                "index": i,
                "title": title,
                "url": url,
                "snippet": snippet,
                "read_ok": bool(item.get("read_ok")),
            }
            if site:
                src["siteName"] = site
            if published:
                src["datePublished"] = published
            web_sources.append(src)
            blocks.append(_format_result_block(i, item))

        out = (
            "以下为联网搜索结果摘要，回答时凡引用必须以 [来源N] 标注（N 与编号一致），"
            "并可在结尾附上对应 URL：\n\n" + "\n\n".join(blocks)
        )
        # 网页摘要属非可信外部内容：隔离包裹 + 长度上限，缓解间接提示注入
        out = guard_untrusted_content(out)
        log_kb_tool_return_to_terminal(out, tool_label="web_search")
        _set_last_rag_context({"web_sources": web_sources})
        return out

    return StructuredTool.from_function(
        name="web_search",
        description=(
            "联网搜索（实时网页结果）。"
            "何时使用：用户问题涉及最新/实时信息（新闻、价格、版本发布、天气股价、赛事比分、近期事件等），"
            "或你的知识无法确定且需要查证的公开事实。"
            "何时不要使用：纯创作、闲聊、数学计算、代码编写等无需外部信息的场景；知识库检索已开启时不要使用。"
            "查询建议：涉及时效时改写 query（可带年份或月份）并设置 freshness；"
            "需要限定站点时填写 site；同一意图的另一种说法用 extra_query，不要再调一次 web_search。"
            "用户给出 http(s) 链接、或需要深读某条搜索结果时，应调用 fetch_url，而不是再搜一遍。"
            "得到结果后应直接整合为最终回答，不要为同一意图反复检索。"
            "返回内容：编号 [N] 的标题、站点、发布日期、URL、摘要，以及可能的正文摘录。"
            "站点与正文摘录可用于判断来源是否可靠、信息是否仍有效；"
            "摘录与摘要冲突时以正文为准，并按 [来源N] 引用。"
            "回答纪律：仅依据搜索结果作答，凡引用必须以 [来源N] 标注来源；"
            "结果不足以回答时如实说明，不得编造事实或来源链接。"
            "调用约束：同一用户提问轮次内调用次数有限。"
        ),
        args_schema=_WebSearchArgs,
        func=_web_search,
    )


def make_fetch_url_tool() -> StructuredTool:
    """读取用户或搜索结果给出的单个 http(s) 页面正文。"""

    class _FetchUrlArgs(BaseModel):
        url: str = Field(description="要读取的公开 http(s) 网页地址。")

    def _fetch_url(url: str) -> str:
        if not is_web_search_allowed_this_turn():
            limit_msg = fetch_url_disabled_this_turn_msg()
            log_kb_tool_return_to_terminal(limit_msg, tool_label="fetch_url")
            return limit_msg

        if not try_acquire_fetch_url_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: fetch_url has reached the per-turn call limit. "
                "Use the existing page content above to compose the final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="fetch_url")
            return limit_msg

        u = (url or "").strip()
        if not u:
            return "错误：url 为空。"
        if not u.lower().startswith(("http://", "https://")):
            return "错误：url 仅支持 http(s)。"

        emit_rag_step("🔗", "读取网页", u)
        max_chars = max(80, int(getattr(settings, "WEB_SEARCH_FETCH_MAX_CHARS", 4000) or 4000))
        page = fetch_page(u, max_chars=max_chars)
        if not page.get("ok"):
            err = (page.get("error") or "unknown")[:200]
            emit_rag_step("⚠️", "读页失败", err)
            _set_last_rag_context({"web_sources": []})
            err_msg = (
                f"FETCH_URL_FAILED: 无法读取该网页（{err}）。"
                "请如实告知用户未能打开该链接；不得编造页面正文或来源。"
            )
            log_kb_tool_return_to_terminal(err_msg, tool_label="fetch_url")
            return err_msg

        title = (page.get("title") or "").strip() or "(无标题)"
        final_url = (page.get("url") or u).strip()
        text = (page.get("text") or "").strip()
        src = {
            "index": 1,
            "title": title,
            "url": final_url,
            "snippet": text[:400],
            "read_ok": True,
        }
        block = f"[1] {title}\nURL: {final_url}\n正文摘录: {text}"
        out = (
            "以下为指定网页的正文摘录，回答时凡引用必须以 [来源1] 标注，"
            "并保证引用的 URL 与下面逐字一致：\n\n" + block
        )
        out = guard_untrusted_content(out)
        emit_rag_step("📑", "读页完成", title)
        log_kb_tool_return_to_terminal(out, tool_label="fetch_url")
        _set_last_rag_context({"web_sources": [src]})
        return out

    return StructuredTool.from_function(
        name="fetch_url",
        description=(
            "读取指定公开网页的正文（静态 HTML）。"
            "何时使用：用户直接给出 http(s) 链接；或 web_search 已返回某条 URL、需要精读该页。"
            "何时不要使用：没有具体网址时请用 web_search；不要用本工具打开内网或需要登录的页面。"
            "返回内容：编号 [1] 的标题、URL、正文摘录。"
            "回答纪律：仅依据摘录作答，引用必须标注 [来源1]；读取失败时如实说明，不得编造正文。"
            "调用约束：同一轮次数有限。"
        ),
        args_schema=_FetchUrlArgs,
        func=_fetch_url,
    )
