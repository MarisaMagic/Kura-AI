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


def _is_https_url(url: str) -> bool:
    return (url or "").strip().lower().startswith("https://")


_STRIP_IMAGE_QUERY_KEYS = frozenset(
    {
        "w",
        "h",
        "width",
        "height",
        "q",
        "quality",
        "size",
        "x-oss-process",
        "x-bce-process",
        "imageview2",
        "imagemogr2",
        "image_process",
        "x-image-process",
    }
)


def _normalize_image_url(url: str) -> str:
    """去掉 fragment 与常见尺寸/缓存 query，host+path 小写，便于同一原图去重。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.rstrip("/").casefold()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        folded = key.casefold()
        if folded in _STRIP_IMAGE_QUERY_KEYS or folded.startswith(("x-oss-", "imageview", "imagemogr")):
            continue
        kept.append((key, value))
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    scheme = (parsed.scheme or "https").lower()
    return urlunparse((scheme, netloc, path, "", urlencode(kept), "")).casefold()


def _already_shown_image_urls() -> set[str]:
    ctx = get_last_rag_context(clear=False) or {}
    seen: set[str] = set()
    for src in ctx.get("web_sources") or []:
        if not isinstance(src, dict):
            continue
        key = _normalize_image_url((src.get("image_url") or "").strip())
        if key:
            seen.add(key)
    return seen


def _drop_already_shown_images(results: list[dict]) -> list[dict]:
    seen = _already_shown_image_urls()
    if not seen:
        return list(results)
    out: list[dict] = []
    for item in results:
        key = _normalize_image_url((item.get("contentUrl") or "").strip())
        if key and key in seen:
            continue
        out.append(item)
    return out


def _parse_bocha_images(payload: object) -> list[dict]:
    """兼容 data.images.value / images.value；只保留 https contentUrl。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, dict):
        raw_images = payload.get("images") if isinstance(payload, dict) else None
        images = raw_images if isinstance(raw_images, dict) else {}
    values = images.get("value") if isinstance(images, dict) else None
    if not isinstance(values, list):
        return []
    out: list[dict] = []
    for r in values:
        if not isinstance(r, dict):
            continue
        content_url = (r.get("contentUrl") or r.get("content_url") or "").strip()
        if not _is_https_url(content_url):
            continue
        title = (r.get("name") or r.get("title") or "").strip()
        page_url = (r.get("hostPageUrl") or r.get("host_page_url") or r.get("url") or "").strip()
        if page_url and not page_url.lower().startswith(("http://", "https://")):
            page_url = ""
        item: dict = {
            "title": title,
            "contentUrl": content_url,
            "hostPageUrl": page_url,
        }
        width = r.get("width")
        height = r.get("height")
        if width not in (None, ""):
            item["width"] = width
        if height not in (None, ""):
            item["height"] = height
        out.append(item)
    return out


def _dedupe_images(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in results:
        key = _normalize_image_url((item.get("contentUrl") or "").strip())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _image_max_results() -> int:
    n = int(getattr(settings, "WEB_IMAGE_SEARCH_MAX_RESULTS", 6) or 6)
    return max(1, min(20, n))


def _image_markdown_count() -> int:
    n = int(getattr(settings, "WEB_IMAGE_SEARCH_MARKDOWN_COUNT", 4) or 4)
    return max(0, min(_image_max_results(), n))


def _image_candidate_count() -> int:
    n = int(getattr(settings, "WEB_IMAGE_SEARCH_CANDIDATE_COUNT", 18) or 18)
    return max(_image_max_results(), min(50, n))


# 图床/社交/聚合站：来源页通常不适合作为引用。只压域名，不维护媒体品牌表。
_IMAGE_JUNK_DOMAINS = frozenset(
    {
        "pinterest.com",
        "pinterest.co.uk",
        "pinimg.com",
        "blogspot.com",
        "blogger.com",
        "tumblr.com",
        "weebly.com",
        "wixsite.com",
        "wix.com",
        "livejournal.com",
        "typepad.com",
        "fc2.com",
        "pixnet.net",
        "xiaohongshu.com",
        "xhslink.com",
        "weibo.com",
        "weibo.cn",
        "douyin.com",
        "kuaishou.com",
        "tiktok.com",
        "instagram.com",
        "facebook.com",
        "reddit.com",
        "imgur.com",
        "giphy.com",
        "tenor.com",
        "toutiao.com",
        "ixigua.com",
        "360kuai.com",
    }
)
_IMAGE_JUNK_HOSTS = frozenset(
    {
        "tieba.baidu.com",
        "zhidao.baidu.com",
        "jingyan.baidu.com",
        "wenku.baidu.com",
        "image.baidu.com",
        "img.baidu.com",
    }
)
_IMAGE_PREFERRED_DOMAINS = frozenset(
    {
        "wikipedia.org",
        "wikimedia.org",
        "unsplash.com",
        "pexels.com",
        "pixabay.com",
    }
)


def _page_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_junk_image_host(url: str) -> bool:
    host = _page_host(url)
    if not host:
        return False
    if host in _IMAGE_JUNK_HOSTS:
        return True
    domain = registrable_domain(host)
    return domain in _IMAGE_JUNK_DOMAINS


def _is_preferred_image_host(url: str) -> bool:
    host = _page_host(url)
    if not host:
        return False
    domain = registrable_domain(host)
    return domain in _IMAGE_PREFERRED_DOMAINS or any(
        host == d or host.endswith("." + d) for d in _IMAGE_PREFERRED_DOMAINS
    )


def _image_dims(item: dict) -> tuple[int, int] | None:
    try:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _image_size_score(item: dict) -> float:
    dims = _image_dims(item)
    if not dims:
        return 0.45
    long_edge = max(dims)
    return max(0.0, min(1.0, (long_edge - 160) / 1440))


def _filter_tiny_images(results: list[dict]) -> list[dict]:
    """丢掉过小图；若全部过小则原样保留。"""
    min_edge = max(0, int(getattr(settings, "WEB_IMAGE_SEARCH_MIN_EDGE", 240) or 0))
    sized: list[dict] = []
    tiny: list[dict] = []
    for raw in results:
        item = dict(raw)
        dims = _image_dims(item)
        if min_edge and dims and max(dims) < min_edge:
            tiny.append(item)
        else:
            sized.append(item)
    return sized or tiny


def _dashscope_rerank_endpoint() -> str:
    """与知识库 rag_utils._rerank_endpoint 同一拼接规则，避免 import 整条 KB 检索栈。"""
    host = (getattr(settings, "RERANK_BINDING_HOST", None) or "").strip().rstrip("/")
    if not host:
        return ""
    if "services/rerank" in host:
        return host
    if host.endswith("/api/v1"):
        return f"{host}/services/rerank/text-rerank/text-rerank"
    return host


def _image_vl_rerank_enabled() -> bool:
    if not bool(getattr(settings, "WEB_IMAGE_RERANK_ENABLED", True)):
        return False
    model = (getattr(settings, "RERANK_MODEL", None) or "").strip()
    key = (getattr(settings, "RERANK_API_KEY", None) or "").strip()
    host = (getattr(settings, "RERANK_BINDING_HOST", None) or "").strip()
    if not (model and key and host):
        return False
    return "vl-rerank" in model.casefold()


def _is_vl_download_url_error(status_code: int, body: str) -> bool:
    if status_code < 400:
        return False
    blob = (body or "").lower()
    return "download form url error" in blob or (
        "invalidparameter" in blob and "download" in blob
    )


def _apply_vl_rerank_rows(
    out: list[dict], send_idx: list[int], items: list, *, index_map: list[int] | None = None
) -> int:
    wrote = 0
    for row in items:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if not isinstance(idx, int) or idx < 0:
            continue
        if index_map is not None:
            if idx >= len(index_map):
                continue
            dest = index_map[idx]
        else:
            if idx >= len(send_idx):
                continue
            dest = send_idx[idx]
        try:
            score = float(row.get("relevance_score"))
        except (TypeError, ValueError):
            continue
        out[dest]["rerank_score"] = max(0.0, min(1.0, score))
        wrote += 1
    return wrote


def _post_vl_rerank(
    client,
    *,
    endpoint: str,
    headers: dict,
    model: str,
    query: str,
    documents: list[dict],
) -> tuple[int, str, list]:
    payload = {
        "model": model,
        "input": {"query": {"text": query}, "documents": documents},
        "parameters": {"top_n": len(documents), "return_documents": False},
    }
    resp = client.post(endpoint, headers=headers, json=payload)
    raw_text = getattr(resp, "text", None)
    text = raw_text[:500] if isinstance(raw_text, str) else ""
    if resp.status_code >= 400:
        return resp.status_code, text, []
    body = resp.json()
    items = (body.get("output") or {}).get("results") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        return resp.status_code, text or "empty_rerank_results", []
    return resp.status_code, text, items


def _rerank_web_images(query: str, results: list[dict]) -> tuple[list[dict], dict]:
    """
    DashScope qwen3-vl-rerank：query 文本 + https 原图 URL。
    整批拉图失败时逐张重试；失败敞开。返回 (results, meta)。
    """
    meta: dict = {
        "applied": False,
        "fallback": False,
        "error": None,
        "skipped": None,
        "skipped_bad": 0,
    }
    if not results:
        return results, meta
    if not _image_vl_rerank_enabled():
        meta["skipped"] = "disabled"
        return results, meta

    import httpx

    model = (getattr(settings, "RERANK_MODEL", None) or "").strip()
    key = (getattr(settings, "RERANK_API_KEY", None) or "").strip()
    endpoint = _dashscope_rerank_endpoint()
    if not endpoint:
        meta["skipped"] = "no_endpoint"
        return results, meta

    cap = max(1, min(40, int(getattr(settings, "RERANK_MAX_CANDIDATES", 30) or 30)))
    out = [dict(item) for item in results]
    send_idx: list[int] = []
    documents: list[dict] = []
    for i, item in enumerate(out):
        url = (item.get("contentUrl") or "").strip()
        if not _is_https_url(url):
            continue
        if len(documents) >= cap:
            break
        send_idx.append(i)
        documents.append({"image": url})
    if not documents:
        meta["skipped"] = "no_https"
        return out, meta

    timeout = max(5, int(getattr(settings, "RERANK_TIMEOUT_SECONDS", 15) or 15))
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    client_kwargs: dict = {"timeout": timeout, "follow_redirects": True}
    proxy = _web_search_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy

    def _finish_applied(wrote: int, skipped_bad: int) -> tuple[list[dict], dict]:
        if wrote <= 0:
            meta["fallback"] = True
            meta["error"] = meta.get("error") or "empty_rerank_results"
            return [dict(item) for item in results], meta
        meta["applied"] = True
        meta["skipped_bad"] = skipped_bad
        return out, meta

    try:
        with httpx.Client(**client_kwargs) as client:
            status, text, items = _post_vl_rerank(
                client,
                endpoint=endpoint,
                headers=headers,
                model=model,
                query=query,
                documents=documents,
            )
            if items:
                wrote = _apply_vl_rerank_rows(out, send_idx, items)
                return _finish_applied(wrote, 0)
            if status == 0:
                raise RuntimeError(text or "empty_rerank_results")
            # 整批超时不再逐张，避免把延迟拉满
            err_msg = f"HTTP {status}: {text}" if status >= 400 else (text or "empty_rerank_results")
            if not _is_vl_download_url_error(status, text):
                raise RuntimeError(err_msg)
            logger.info("搜图 VL 整批拉图失败，改为逐张重试: %s", err_msg[:200])
            wrote = 0
            skipped_bad = 0
            for local_i, doc in enumerate(documents):
                try:
                    st, body, rows = _post_vl_rerank(
                        client,
                        endpoint=endpoint,
                        headers=headers,
                        model=model,
                        query=query,
                        documents=[doc],
                    )
                except httpx.TimeoutException:
                    skipped_bad += 1
                    continue
                except Exception:
                    skipped_bad += 1
                    continue
                if rows:
                    wrote += _apply_vl_rerank_rows(
                        out, send_idx, rows, index_map=[send_idx[local_i]]
                    )
                    continue
                if _is_vl_download_url_error(st, body) or st >= 400:
                    skipped_bad += 1
                    continue
                skipped_bad += 1
            if wrote <= 0:
                meta["error"] = err_msg[:300]
                meta["skipped_bad"] = skipped_bad
                meta["fallback"] = True
                return [dict(item) for item in results], meta
            return _finish_applied(wrote, skipped_bad)
    except httpx.TimeoutException as e:
        logger.warning("搜图 VL rerank 超时，按召回原序: %s", e)
        meta["error"] = str(e)[:300]
        meta["fallback"] = True
        return [dict(item) for item in results], meta
    except Exception as e:
        logger.warning("搜图 VL rerank 失败，按召回原序: %s", e)
        meta["error"] = str(e)[:300]
        meta["fallback"] = True
        return [dict(item) for item in results], meta


def _rank_image_results(query: str, results: list[dict]) -> list[dict]:
    """先丢掉过小图（若还有更大图），再按视觉相关分 + 来源权威度 + 尺寸融合，聚合站靠后。"""
    top_k = _image_max_results()
    if not results:
        return []
    pool = _filter_tiny_images(results)
    n = len(pool)
    auth_blend = float(getattr(settings, "WEB_SEARCH_AUTHORITY_BLEND", 0.25) or 0.0)
    size_w = float(getattr(settings, "WEB_IMAGE_SIZE_BLEND", 0.15) or 0.0)
    auth_blend = max(0.0, min(1.0, auth_blend))
    size_w = max(0.0, min(1.0, size_w))
    extra = auth_blend + size_w
    if extra > 0.9:
        scale = 0.9 / extra
        auth_blend *= scale
        size_w *= scale
    rel_w = 1.0 - auth_blend - size_w
    scored: list[dict] = []
    for i, item in enumerate(pool):
        page = (item.get("hostPageUrl") or "").strip()
        auth = authority_score(query, {"url": page, "title": item.get("title") or ""}) if page else 0.28
        size = _image_size_score(item)
        rel = item.get("rerank_score")
        try:
            rel_f = float(rel) if rel is not None else ((n - i) / n if n else 0.0)
        except (TypeError, ValueError):
            rel_f = (n - i) / n if n else 0.0
        rel_f = max(0.0, min(1.0, rel_f))
        final = rel_w * rel_f + auth_blend * auth + size_w * size
        if page and _is_junk_image_host(page):
            final -= 0.40
        if page and _is_preferred_image_host(page):
            final += 0.12
        if not page:
            final -= 0.08
        item["authority_score"] = auth
        item["image_rank_score"] = final
        scored.append(item)
    scored.sort(key=lambda x: float(x.get("image_rank_score") or 0.0), reverse=True)
    clean = [x for x in scored if not _is_junk_image_host((x.get("hostPageUrl") or ""))]
    if len(clean) >= top_k:
        scored = clean
    elif clean:
        junks = [x for x in scored if _is_junk_image_host((x.get("hostPageUrl") or ""))]
        scored = clean + junks
    return scored[:top_k]


def _prepare_ranked_images(query: str, results: list[dict]) -> list[dict]:
    """本轮已展示去重 → 过小图过滤 → VL 重排（坏链剔除再试）→ 权威度融合截断。"""
    pool = _drop_already_shown_images(_dedupe_images(results))
    pool = _filter_tiny_images(pool)
    pool, meta = _rerank_web_images(query, pool)
    if meta.get("fallback"):
        emit_rag_step("⚠️", "重排失败，已按原序", (meta.get("error") or "")[:200])
    elif meta.get("applied"):
        n_scored = sum(1 for x in pool if x.get("rerank_score") is not None)
        skipped = int(meta.get("skipped_bad") or 0)
        detail = f"{n_scored} 条"
        if skipped:
            detail += f"（跳过 {skipped} 条坏链）"
        emit_rag_step("📑", "图片语义重排", detail)
    return _rank_image_results(query, pool)


def _search_images_with_bocha(query: str, max_results: int, timeout: int) -> list[dict]:
    """博查 Web Search，只消费 images 列表。"""
    import httpx

    key = _bocha_api_key()
    if not key:
        raise RuntimeError("未配置 WEB_SEARCH_BOCHA_API_KEY")
    endpoint = (
        (getattr(settings, "WEB_SEARCH_BOCHA_ENDPOINT", "") or "").strip()
        or _DEFAULT_BOCHA_ENDPOINT
    )
    count = max(1, min(50, int(max_results)))
    client_kwargs: dict = {"timeout": timeout, "follow_redirects": True}
    proxy = _web_search_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        resp = client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "count": count,
                "summary": True,
                "freshness": "noLimit",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 200, 0, "200", "0"):
            msg = payload.get("msg") or payload.get("message") or ""
            raise RuntimeError(f"博查搜图失败 code={code} {msg}".strip())
    return _parse_bocha_images(payload)


def _search_images_with_ddgs(query: str, max_results: int, timeout: int) -> list[dict]:
    """ddgs.images；映射 title / image / url。"""
    from ddgs import DDGS

    proxy = _web_search_proxy()
    backend = _resolve_ddgs_backend()
    region = _normalize_ddgs_region(str(getattr(settings, "WEB_SEARCH_REGION", "cn-zh")))
    kwargs: dict = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    with DDGS(**kwargs) as ddgs:
        results = list(
            ddgs.images(
                query,
                region=region,
                safesearch="moderate",
                max_results=max(1, min(50, int(max_results))),
                backend=backend,
            )
            or []
        )
    out: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        content_url = (r.get("image") or r.get("image_url") or "").strip()
        if not _is_https_url(content_url):
            continue
        page_url = (r.get("url") or "").strip()
        if page_url and not page_url.lower().startswith(("http://", "https://")):
            page_url = ""
        item: dict = {
            "title": (r.get("title") or "").strip(),
            "contentUrl": content_url,
            "hostPageUrl": page_url,
        }
        width = r.get("width")
        height = r.get("height")
        if width not in (None, ""):
            item["width"] = width
        if height not in (None, ""):
            item["height"] = height
        out.append(item)
    return out


def _run_image_search(query: str) -> tuple[list[dict], str]:
    """
    按 WEB_SEARCH_PROVIDER 调度图片检索（不做 bing_html、不走网页 rerank/读页）。
    auto：有 Key 先博查，空则 ddgs；强制 bocha / ddgs 跟配置。
    返回候选（未按来源筛选截断）。
    """
    provider = (getattr(settings, "WEB_SEARCH_PROVIDER", "auto") or "auto").strip().lower()
    candidate_n = _image_candidate_count()
    timeout = int(settings.WEB_SEARCH_TIMEOUT_SECONDS)

    def _try_bocha() -> tuple[list[dict], str] | None:
        if not _bocha_api_key():
            return None
        try:
            results = _dedupe_images(_search_images_with_bocha(query, candidate_n, timeout))
            if results:
                return results, "bocha"
            logger.info("博查搜图结果经 https/去重后为空")
        except Exception as e:
            logger.warning("博查搜图失败: %s", e)
        return None

    def _try_ddgs() -> tuple[list[dict], str] | None:
        try:
            results = _dedupe_images(_search_images_with_ddgs(query, candidate_n, timeout))
            if results:
                return results, "ddgs"
            logger.info("ddgs 搜图结果经 https/去重后为空")
        except Exception as e:
            logger.warning("ddgs 搜图失败: %s", e)
        return None

    if provider == "ddgs":
        results = _dedupe_images(_search_images_with_ddgs(query, candidate_n, timeout))
        return results, "ddgs"
    if provider == "bocha":
        results = _dedupe_images(_search_images_with_bocha(query, candidate_n, timeout))
        return results, "bocha"

    hit = _try_bocha()
    if hit:
        return hit
    hit = _try_ddgs()
    if hit:
        return hit
    return [], "none"


def _run_image_searches(queries: list[str]) -> tuple[list[dict], str]:
    """最多两路并行搜图，按 contentUrl 去重后截断。"""
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
        return [], ""
    if len(uniq) == 1:
        results, provider = _run_image_search(uniq[0])
        return _prepare_ranked_images(uniq[0], results), provider

    merged: list[dict] = []
    provider = ""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_run_image_search, q) for q in uniq]
        for fut in futs:
            try:
                results, prov = fut.result()
            except Exception as e:
                logger.warning("一路图片搜索失败: %s", e)
                continue
            if results:
                merged.extend(results)
                provider = provider or prov
    return _prepare_ranked_images(uniq[0], _dedupe_images(merged)), provider or "none"


def _sanitize_md_alt(title: str) -> str:
    alt = (
        (title or "")
        .replace("[", "")
        .replace("]", "")
        .replace("(", "")
        .replace(")", "")
        .replace("\n", " ")
        .strip()
    )
    return alt or "图片"


def _image_display_title(item: dict) -> str:
    """空标题用来源主机，避免「(无标题)」括号破坏 Markdown 图片语法。"""
    title = (item.get("title") or "").strip()
    if title and title not in ("(无标题)", "无标题"):
        return title
    for key in ("hostPageUrl", "contentUrl"):
        host = _page_host((item.get(key) or "").strip())
        if host:
            return host
    return "图片"


def _format_image_block(index: int, item: dict, *, with_markdown: bool) -> str:
    title = _image_display_title(item)
    page = (item.get("hostPageUrl") or "").strip()
    content = (item.get("contentUrl") or "").strip()
    lines = [f"[{index}] {title}"]
    if page:
        lines.append(f"来源页: {page}")
    if with_markdown and _is_https_url(content):
        lines.append(
            "展示此图片时，将下面这一行原样复制到回答中（不要放进代码块，不要改写括号内地址）："
        )
        lines.append(f"![{_sanitize_md_alt(title)}]({content})")
    return "\n".join(lines)


def _format_image_search_output(results: list[dict]) -> str:
    md_n = _image_markdown_count()
    blocks = [
        _format_image_block(i, item, with_markdown=i <= md_n)
        for i, item in enumerate(results, start=1)
    ]
    return (
        "以下为联网图片搜索结果。展示图片时必须把工具给出的 Markdown 图片行原样复制到回答中"
        "（不要放进代码块，不要改写括号内地址，不要改成 /api/v1/media/）。"
        "引用时以 [来源N] 标注（N 与编号一致）。\n\n" + "\n\n".join(blocks)
    )


def _merge_image_web_sources(new_sources: list[dict]) -> None:
    """与已有 web_sources 合并，避免先 web_search 再搜图冲掉网页来源；同图不重复进 chips。"""
    existing = get_last_rag_context(clear=False)
    ctx = dict(existing) if existing else {}
    prev = list(ctx.get("web_sources") or [])
    offset = 0
    seen_img: set[str] = set()
    for src in prev:
        try:
            offset = max(offset, int(src.get("index") or 0))
        except (TypeError, ValueError):
            pass
        key = _normalize_image_url((src.get("image_url") or "").strip()) if isinstance(src, dict) else ""
        if key:
            seen_img.add(key)
    merged = list(prev)
    for src in new_sources:
        item = dict(src)
        img_key = _normalize_image_url((item.get("image_url") or "").strip())
        if img_key and img_key in seen_img:
            continue
        try:
            local = int(item.get("index") or 0)
        except (TypeError, ValueError):
            local = 0
        item["index"] = offset + local if local else offset + len(merged) - len(prev) + 1
        merged.append(item)
        if img_key:
            seen_img.add(img_key)
    ctx["web_sources"] = merged
    _set_last_rag_context(ctx)


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


def make_web_image_search_tool() -> StructuredTool:
    """文字搜图：返回现成 Markdown 图片行，供模型原样复制到回答中。"""

    class _WebImageSearchArgs(BaseModel):
        query: str = Field(
            description=(
                "图片检索关键词：用专名（角色/作品/品牌）加画面或风格词（立绘、官方、Q 版、手办等）。"
                "禁止「这个人物」「这张图」「类似图片」。不要用本工具查新闻事实；事实请用 web_search。"
            )
        )
        extra_query: str | None = Field(
            default=None,
            description=(
                "可选：另一种风格或检索说法（如「Q版」相对 query 的「立绘」），与 query 并行合并。"
                "不要为同一空词再单独调用一次 web_image_search。"
            ),
        )

    def _web_image_search(query: str, extra_query: str | None = None) -> str:
        if not is_web_search_allowed_this_turn():
            limit_msg = web_image_search_disabled_this_turn_msg()
            log_kb_tool_return_to_terminal(limit_msg, tool_label="web_image_search")
            return limit_msg

        if not try_acquire_web_image_search_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: web_image_search has reached the per-turn call limit. "
                "Use the existing image results above to compose the final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg, tool_label="web_image_search")
            return limit_msg

        q = (query or "").strip()
        if not q:
            return "错误：query 为空。"

        extra = (extra_query or "").strip()
        detail = q + ("（双查询）" if extra else "")
        emit_rag_step("🖼️", "图片搜索", detail)
        try:
            results, provider_used = _run_image_searches([q, extra])
        except Exception as e:
            emit_rag_step("⚠️", "图片搜索失败", str(e)[:200])
            err_msg = (
                f"WEB_IMAGE_SEARCH_FAILED: 联网搜图出错：{e}。"
                "请如实告知用户本次搜图失败、可稍后重试；不得编造图片或图片链接。"
            )
            log_kb_tool_return_to_terminal(err_msg, tool_label="web_image_search")
            return err_msg

        emit_rag_step(
            "🔍",
            "图片搜索召回",
            f"{len(results)} 条（{provider_used or 'none'}）",
        )
        if not results:
            empty_msg = (
                "WEB_IMAGE_SEARCH_NO_RESULTS: 本次联网搜图未找到可用的 https 图片。"
                "请如实告知用户未搜到相关图片（可建议换个关键词重试），不得编造图片或图片链接。"
            )
            log_kb_tool_return_to_terminal(empty_msg, tool_label="web_image_search")
            return empty_msg

        image_sources: list[dict] = []
        for i, item in enumerate(results, start=1):
            title = _image_display_title(item)
            page = (item.get("hostPageUrl") or "").strip()
            image_url = (item.get("contentUrl") or "").strip()
            src: dict = {
                "index": i,
                "title": title,
                "url": page,
                "image_url": image_url,
                "content_type": "image",
            }
            image_sources.append(src)

        out = _format_image_search_output(results)
        out = guard_untrusted_content(out)
        log_kb_tool_return_to_terminal(out, tool_label="web_image_search")
        _merge_image_web_sources(image_sources)
        return out

    return StructuredTool.from_function(
        name="web_image_search",
        description=(
            "联网文字搜图（公开 https 图片）。"
            "何时使用：用户需要配图、外观、示例图、某角色/物品的其它图。"
            "何时不要使用：查新闻/事实/价格/版本等文字信息请用 web_search；用户给出具体网页请用 fetch_url。"
            "查询建议：query 必须是专名加风格或外观词（如 Alice Margatroid 立绘）；"
            "禁止「这个人物」「这张图」「类似图片」。多种画风用 extra_query 写第二种，不要用空词再搜。"
            "返回内容：编号 [N] 的标题、来源页，以及前若干条现成的 Markdown 图片行 `![...](https://...)`。"
            "回答纪律：展示图片时必须把工具给出的那一行 Markdown 原样复制到回答中（不要放进代码块）；"
            "禁止改写成 /api/v1/media/、禁止编造或改写括号内图片地址；看不清或工具失败时如实说明。"
            "调用约束：同一用户提问轮次内调用次数有限。"
        ),
        args_schema=_WebImageSearchArgs,
        func=_web_image_search,
    )
