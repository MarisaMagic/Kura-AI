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

from app.chat.web_image_search import (  # noqa: E402
    _is_https_url,
    _safe_page_url,
    _normalize_image_url,
    _already_shown_image_urls,
    _drop_already_shown_images,
    _parse_bocha_images,
    _dedupe_images,
    _image_max_results,
    _image_markdown_count,
    _image_candidate_count,
    _page_host,
    _is_junk_image_host,
    _is_preferred_image_host,
    _image_dims,
    _image_size_score,
    _filter_tiny_images,
    _dashscope_rerank_endpoint,
    _image_vl_rerank_enabled,
    _is_vl_download_url_error,
    _apply_vl_rerank_rows,
    _post_vl_rerank,
    _rerank_web_images,
    _rank_image_results,
    _prepare_ranked_images,
    _search_images_with_bocha,
    _search_images_with_ddgs,
    _run_image_search,
    _run_image_searches,
    _clean_image_title_text,
    _sanitize_md_alt,
    _image_display_title,
    _format_image_block,
    _format_image_search_output,
    _merge_image_web_sources,
    make_web_image_search_tool,
)
from app.chat.web_search_providers import (  # noqa: E402
    _web_search_proxy,
    _bocha_api_key,
    _normalize_ddgs_region,
    _resolve_ddgs_backend,
    _candidate_count,
    _normalize_site,
    _query_with_site,
    _is_dictionary_junk,
    _drop_dictionary_junk,
    _dedupe_by_url,
    _prepare_candidates,
    _format_published,
    _normalize_freshness,
    _infer_freshness_from_query,
    _resolve_freshness,
    _parse_bocha_pages,
    _search_with_bocha,
    _search_bocha_with_freshness_fallback,
    _search_with_ddgs,
    _search_with_bing,
    _run_search,
    _run_searches,
    _format_result_block,
)
from app.chat.web_search_rerank import (  # noqa: E402
    _rerank_enabled,
    _build_rerank_document,
    _parse_rerank_results,
    _rerank_web_results,
)

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
            logger.warning("联网搜索失败: %s", e)
            emit_rag_step("⚠️", "联网搜索失败", "")
            _set_last_rag_context({"web_sources": []})
            err_msg = (
                "WEB_SEARCH_FAILED: 本次联网搜索失败，可稍后重试。"
                "请如实告知用户；不得编造任何搜索结果或来源链接。"
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
                "",
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
            logger.info("fetch_url 读页失败: %s", err)
            emit_rag_step("⚠️", "读页失败", "")
            _set_last_rag_context({"web_sources": []})
            err_msg = (
                "FETCH_URL_FAILED: 无法读取该网页。"
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


