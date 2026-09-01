"""联网搜索工具。

Provider：
- bocha：博查 Web Search JSON API（国内无代理主路径，需 WEB_SEARCH_BOCHA_API_KEY）
- ddgs：ddgs 库（有 WEB_SEARCH_PROXY 时优先；无代理默认 bing backend）
- bing_html：Bing 国内版 HTML 解析（最后兜底）
- auto（默认）：无代理且已配 Key 时 bocha → bing_html；有代理时 ddgs → bocha → bing_html
"""

from __future__ import annotations

import logging
import re

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    is_web_search_allowed_this_turn,
    log_kb_tool_return_to_terminal,
    try_acquire_web_search_tool_slot,
    web_search_disabled_this_turn_msg,
)
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
_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")
_DICT_JUNK_RE = re.compile(
    r"字的来源|组词|拼音|的意思|释义|单词|词汇|词典|definition|meaning of",
    re.IGNORECASE,
)


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


def _fetch_limit(max_results: int) -> int:
    """多取若干条，供相关性过滤后仍能凑满 max_results。"""
    n = max(1, int(max_results))
    return max(n * 2, n + 3)


def _extract_query_tokens(query: str) -> list[str]:
    """连续 CJK 片段、2/3 字 n-gram，以及英文/数字词（≥2 字符），去重保序。"""
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


def _is_dictionary_junk(title: str) -> bool:
    return bool(_DICT_JUNK_RE.search(title or ""))


def _token_in_blob(token: str, blob: str, blob_cf: str) -> bool:
    if token.isascii():
        return token.casefold() in blob_cf
    return token in blob


def _filter_relevant_results(query: str, results: list[dict]) -> list[dict]:
    """丢弃字典 junk；中文须命中任一 n-gram；拉丁词≥2 时须命中不少于一半。"""
    tokens = _extract_query_tokens(query)
    cjk_tokens = [t for t in tokens if not t.isascii()]
    latin_tokens = [t for t in tokens if t.isascii()]
    if len(latin_tokens) >= 2:
        latin_need = (len(latin_tokens) + 1) // 2
    elif len(latin_tokens) == 1:
        latin_need = 1
    else:
        latin_need = 0

    out: list[dict] = []
    for item in results:
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if _is_dictionary_junk(title):
            continue
        blob = f"{title}\n{snippet}"
        blob_cf = blob.casefold()
        if cjk_tokens and not any(_token_in_blob(t, blob, blob_cf) for t in cjk_tokens):
            continue
        if latin_need:
            hits = sum(1 for t in latin_tokens if _token_in_blob(t, blob, blob_cf))
            if hits < latin_need:
                continue
        out.append(item)
    return out


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
        out.append({"title": title, "url": url, "snippet": snippet})
    return out


def _search_with_bocha(query: str, max_results: int, timeout: int) -> list[dict]:
    """博查 Web Search；返回 [{title, url, snippet}]，失败抛异常。"""
    import httpx

    key = _bocha_api_key()
    if not key:
        raise RuntimeError("未配置 WEB_SEARCH_BOCHA_API_KEY")
    endpoint = (
        (getattr(settings, "WEB_SEARCH_BOCHA_ENDPOINT", "") or "").strip()
        or _DEFAULT_BOCHA_ENDPOINT
    )
    count = min(50, _fetch_limit(max_results))
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
            raise RuntimeError(f"博查搜索失败 code={code} {msg}".strip())
    return _parse_bocha_pages(payload)


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
                max_results=_fetch_limit(max_results),
                backend=backend,
            )
            or []
        )
    return [
        {
            "title": (r.get("title") or "").strip(),
            "url": (r.get("href") or "").strip(),
            "snippet": (r.get("body") or "").strip(),
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

    fetch_n = _fetch_limit(max_results)
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
            }
        )
        if len(out) >= fetch_n:
            break
    return out


def _run_search(query: str) -> tuple[list[dict], str]:
    """
    按 WEB_SEARCH_PROVIDER 调度搜索。
    auto：有代理时 ddgs → bocha → bing_html；无代理且有 Key 时 bocha → bing_html。
    :return: (results, provider_used)
    """
    provider = (getattr(settings, "WEB_SEARCH_PROVIDER", "auto") or "auto").strip().lower()
    max_results = int(settings.WEB_SEARCH_MAX_RESULTS)
    timeout = int(settings.WEB_SEARCH_TIMEOUT_SECONDS)

    def _keep(raw: list[dict]) -> list[dict]:
        return _filter_relevant_results(query, raw)[:max_results]

    def _try_ddgs() -> tuple[list[dict], str] | None:
        try:
            results = _keep(_search_with_ddgs(query, max_results, timeout))
            if results:
                return results, "ddgs"
            logger.info("ddgs 结果经相关性过滤后为空")
        except Exception as e:
            logger.warning("ddgs 搜索失败 backend=%s: %s", _resolve_ddgs_backend(), e)
        return None

    def _try_bocha() -> tuple[list[dict], str] | None:
        if not _bocha_api_key():
            return None
        try:
            results = _keep(_search_with_bocha(query, max_results, timeout))
            if results:
                return results, "bocha"
            logger.info("博查结果经相关性过滤后为空")
        except Exception as e:
            logger.warning("博查搜索失败: %s", e)
        return None

    if provider == "bing_html":
        return _keep(_search_with_bing(query, max_results, timeout)), "bing_html"
    if provider == "ddgs":
        return _keep(_search_with_ddgs(query, max_results, timeout)), "ddgs"
    if provider == "bocha":
        return _keep(_search_with_bocha(query, max_results, timeout)), "bocha"

    has_proxy = bool(_web_search_proxy())
    has_bocha = bool(_bocha_api_key())

    if has_proxy:
        hit = _try_ddgs()
        if hit:
            return hit
        hit = _try_bocha()
        if hit:
            return hit
        return _keep(_search_with_bing(query, max_results, timeout)), "bing_html"

    if has_bocha:
        hit = _try_bocha()
        if hit:
            return hit
        return _keep(_search_with_bing(query, max_results, timeout)), "bing_html"

    logger.warning(
        "未配置 WEB_SEARCH_BOCHA_API_KEY，无代理时回退 ddgs bing + bing_html；"
        "申请 Key: https://open.bochaai.com/"
    )
    hit = _try_ddgs()
    if hit:
        return hit
    return _keep(_search_with_bing(query, max_results, timeout)), "bing_html"


def make_web_search_tool() -> StructuredTool:
    """构建 web_search 工具：返回编号化的标题/URL/摘要列表，并收集来源供前端展示。"""

    class _WebSearchArgs(BaseModel):
        query: str = Field(
            description="搜索查询词，应与用户意图一致；建议精炼为关键词或短句（中英文均可）。"
        )

    def _web_search(query: str) -> str:
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

        emit_rag_step("🌐", "联网搜索", q)
        try:
            results, provider_used = _run_search(q)
        except Exception as e:
            emit_rag_step("⚠️", "联网搜索失败", str(e)[:200])
            _set_last_rag_context({"web_sources": []})
            err_msg = (
                f"联网搜索出错：{e}。请如实告知用户本次联网搜索失败、可稍后重试；"
                "不得编造任何搜索结果或来源链接。"
            )
            log_kb_tool_return_to_terminal(err_msg, tool_label="web_search")
            return err_msg

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
            web_sources.append({"index": i, "title": title, "url": url, "snippet": snippet})
            blocks.append(f"[{i}] {title}\nURL: {url}\n摘要: {snippet}")

        out = (
            "以下为联网搜索结果摘要，回答时凡引用必须以 [来源N] 标注（N 与编号一致），"
            "并可在结尾附上对应 URL：\n\n" + "\n\n".join(blocks)
        )
        # 网页摘要属非可信外部内容：隔离包裹 + 长度上限，缓解间接提示注入
        out = guard_untrusted_content(out)
        emit_rag_step("📑", "联网搜索完成", f"命中 {len(results)} 条结果（{provider_used}）")
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
            "返回内容：编号 [N] 的标题、URL、摘要列表。"
            "回答纪律：仅依据搜索结果作答，凡引用必须以 [来源N] 标注来源；"
            "结果不足以回答时如实说明，不得编造事实或来源链接。"
            "调用约束：同一用户提问轮次内调用次数有限，得到结果后应直接整合为最终回答，勿重复检索同一内容。"
        ),
        args_schema=_WebSearchArgs,
        func=_web_search,
    )
