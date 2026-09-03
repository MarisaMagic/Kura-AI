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


from app.settings import settings

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


