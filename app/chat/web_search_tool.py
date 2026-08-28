"""联网搜索工具（免费、无需 API Key）。

Provider：
- ddgs：DuckDuckGo（ddgs 库；海外或配置代理环境可用）
- bing_html：Bing 国内版 HTML 解析（cn.bing.com，国内直连兜底）
- auto（默认）：先 ddgs，失败或无结果时自动回退 bing_html
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
    try_acquire_web_search_tool_slot,
)
from app.settings import settings

_BING_SEARCH_URL = "https://cn.bing.com/search"
_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _search_with_ddgs(query: str, max_results: int, timeout: int) -> list[dict]:
    """DuckDuckGo 搜索；返回 [{title, url, snippet}]，失败抛异常。"""
    from ddgs import DDGS

    proxy = (getattr(settings, "WEB_SEARCH_PROXY", "") or "").strip() or None
    kwargs: dict = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    with DDGS(**kwargs) as ddgs:
        results = list(
            ddgs.text(
                query,
                region=str(settings.WEB_SEARCH_REGION),
                safesearch="moderate",
                max_results=max_results,
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
    """Bing 国内版 HTML 搜索解析（cn.bing.com 直连）；返回 [{title, url, snippet}]，失败抛异常。"""
    import httpx
    from bs4 import BeautifulSoup

    resp = httpx.get(
        _BING_SEARCH_URL,
        params={"q": query},
        headers=_BING_HEADERS,
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[dict] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        url = (a.get("href") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        cap = li.select_one(".b_caption p")
        out.append(
            {
                "title": a.get_text(strip=True),
                "url": url,
                "snippet": cap.get_text(strip=True) if cap else "",
            }
        )
        if len(out) >= max_results:
            break
    return out


def _run_search(query: str) -> tuple[list[dict], str]:
    """
    按 WEB_SEARCH_PROVIDER 调度搜索；auto 模式 ddgs 失败/无结果时回退 bing_html。
    :return: (results, provider_used)
    """
    provider = (getattr(settings, "WEB_SEARCH_PROVIDER", "auto") or "auto").strip().lower()
    max_results = int(settings.WEB_SEARCH_MAX_RESULTS)
    timeout = int(settings.WEB_SEARCH_TIMEOUT_SECONDS)

    if provider == "bing_html":
        return _search_with_bing(query, max_results, timeout), "bing_html"
    if provider == "ddgs":
        return _search_with_ddgs(query, max_results, timeout), "ddgs"

    # auto：ddgs 优先，失败或空结果回退 bing_html
    try:
        results = _search_with_ddgs(query, max_results, timeout)
        if results:
            return results, "ddgs"
    except Exception:
        pass
    return _search_with_bing(query, max_results, timeout), "bing_html"


def make_web_search_tool() -> StructuredTool:
    """构建 web_search 工具：返回编号化的标题/URL/摘要列表，并收集来源供前端展示。"""

    class _WebSearchArgs(BaseModel):
        query: str = Field(
            description="搜索查询词，应与用户意图一致；建议精炼为关键词或短句（中英文均可）。"
        )

    def _web_search(query: str) -> str:
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
