"""联网搜索读页：SSRF 校验 + pinned/代理抓取 + 主文抽取。"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait

import httpx

from app.settings import settings
from app.utils.egress import assert_httpx_response_public, build_pinned_sync_client
from app.utils.ssrf import UnsafeUrlError, validate_public_http_url

logger = logging.getLogger(__name__)

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_MIN_EXTRACT_CHARS = 80
_MAX_REDIRECTS = 3
_OVERALL_DEADLINE_SECONDS = 15
_READ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_WS_RE = re.compile(r"\s+")
_CT_CHARSET_RE = re.compile(r"charset\s*=\s*([^\s;]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"""<meta\b[^>]*?\bcharset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE | re.DOTALL,
)
_CODEC_ALIASES = {
    "gb2312": "gbk",
    "gb-2312": "gbk",
    "gb_2312": "gbk",
    "gb_2312-80": "gbk",
    "chinese": "gbk",
    "csiso58gb231280": "gbk",
    "hz-gb-2312": "gbk",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "iso-8859-1": None,
    "latin-1": None,
    "latin1": None,
    "iso8859-1": None,
    "us-ascii": "ascii",
    "ascii": "ascii",
}


def _proxy() -> str | None:
    return (getattr(settings, "WEB_SEARCH_PROXY", "") or "").strip() or None


def _timeout_seconds() -> float:
    return max(2.0, float(getattr(settings, "WEB_SEARCH_READ_TIMEOUT_SECONDS", 8) or 8))


def _max_bytes() -> int:
    return max(8 * 1024, int(getattr(settings, "WEB_SEARCH_READ_MAX_BYTES", 524288) or 524288))


def _max_chars() -> int:
    return max(80, int(getattr(settings, "WEB_SEARCH_READ_MAX_CHARS", 2000) or 2000))


def extract_html_title(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    if soup.title:
        return _WS_RE.sub(" ", soup.title.get_text(" ", strip=True)).strip()
    return ""


def extract_main_text(html: str, max_chars: int) -> str:
    """去掉导航噪音，优先 article/main，否则拼接 p 文本。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript"]):
        tag.decompose()
    node = (
        soup.select_one("article")
        or soup.select_one("main")
        or soup.select_one("[role=main]")
    )
    if node:
        text = node.get_text(" ", strip=True)
    else:
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    text = _WS_RE.sub(" ", text).strip()
    return text[: max(1, int(max_chars))]


def _normalize_codec(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().strip("\"'").lower().replace("_", "-")
    if not n:
        return None
    if n in _CODEC_ALIASES:
        return _CODEC_ALIASES[n]
    return n


def _charset_from_content_type(content_type: str) -> str | None:
    m = _CT_CHARSET_RE.search(content_type or "")
    if not m:
        return None
    return _normalize_codec(m.group(1))


def _charset_from_meta(raw: bytes) -> str | None:
    head = raw[:4096]
    m = _META_CHARSET_RE.search(head)
    if not m:
        return None
    return _normalize_codec(m.group(1).decode("ascii", errors="ignore"))


def _try_decode(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_html_bytes(raw: bytes, *, content_type: str = "") -> str:
    """BOM / 合法 UTF-8 优先；否则 meta、响应头（忽略 latin-1），再试 gb18030。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        text = _try_decode(raw[3:], "utf-8")
        if text is not None:
            return text
    utf8 = _try_decode(raw, "utf-8")
    if utf8 is not None:
        return utf8
    declared: list[str] = []
    for enc in (_charset_from_meta(raw), _charset_from_content_type(content_type)):
        if enc and enc not in declared and enc != "utf-8":
            declared.append(enc)
    for enc in declared + ["gb18030", "gbk"]:
        text = _try_decode(raw, enc)
        if text is not None:
            return text
    return raw.decode("gb18030", errors="replace")


def _is_html_content_type(value: str) -> bool:
    low = (value or "").split(";", 1)[0].strip().lower()
    return any(low == t or low.startswith(t) for t in _HTML_TYPES)


def _redirect_target(resp: httpx.Response) -> str | None:
    if not resp.is_redirect:
        return None
    location = resp.headers.get("location")
    if not location:
        return None
    assert_httpx_response_public(resp)
    return str(resp.url.join(location))


def _request_with_proxy(url: str, timeout: float) -> httpx.Response:
    proxy = _proxy()
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        proxy=proxy,
        trust_env=False,
        headers=_READ_HEADERS,
    ) as client:
        return client.get(url)


def _request_pinned(url: str, timeout: float) -> httpx.Response:
    client = build_pinned_sync_client(url, timeout=timeout)
    try:
        return client.get(url, headers=_READ_HEADERS)
    finally:
        client.close()


def _fetch_html(url: str) -> tuple[str, str]:
    """校验并抓取 HTML；返回 (html, final_url)，失败抛异常。"""
    timeout = _timeout_seconds()
    max_bytes = _max_bytes()
    current = (url or "").strip()
    for _ in range(_MAX_REDIRECTS + 1):
        validate_public_http_url(current)
        if _proxy():
            resp = _request_with_proxy(current, timeout)
        else:
            resp = _request_pinned(current, timeout)
        nxt = _redirect_target(resp)
        if nxt:
            current = nxt
            continue
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"HTTP {resp.status_code}")
        if not _is_html_content_type(resp.headers.get("content-type") or ""):
            raise RuntimeError("non-html content-type")
        raw = resp.content
        if len(raw) > max_bytes:
            raise RuntimeError("body too large")
        html = decode_html_bytes(raw, content_type=resp.headers.get("content-type") or "")
        return html, str(resp.url) or current
    raise RuntimeError("too many redirects")


def fetch_page(url: str, *, max_chars: int | None = None) -> dict:
    """
    供 fetch_url / 搜索读页复用。
    :return: {ok, title, text, url, error}
    """
    limit = max_chars if max_chars is not None else _max_chars()
    limit = max(80, int(limit))
    try:
        html, final_url = _fetch_html(url)
        text = extract_main_text(html, limit)
        title = extract_html_title(html)
        if len(text) < _MIN_EXTRACT_CHARS:
            return {
                "ok": False,
                "title": title,
                "text": "",
                "url": final_url,
                "error": "extracted_too_short",
            }
        return {"ok": True, "title": title, "text": text, "url": final_url, "error": ""}
    except (UnsafeUrlError, httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        logger.info("读页失败 url=%s: %s", (url or "")[:160], e)
        return {"ok": False, "title": "", "text": "", "url": (url or "").strip(), "error": str(e)[:300]}


def _read_one(url: str) -> str | None:
    page = fetch_page(url)
    if page.get("ok"):
        return str(page.get("text") or "")
    return None


def read_top_pages(results: list[dict]) -> tuple[list[dict], dict]:
    """
    并行读取前 N 条正文；失败则按序向后补位，直到成功数达到 N 或名单耗尽。
    :return: (copies, meta)
    """
    out = [dict(item) for item in results]
    for item in out:
        item["read_ok"] = False
        item.pop("page_text", None)
    meta = {"attempted": 0, "ok": 0, "enabled": True}
    if not bool(getattr(settings, "WEB_SEARCH_READ_ENABLED", True)):
        meta["enabled"] = False
        return out, meta
    top_n = max(0, int(getattr(settings, "WEB_SEARCH_READ_TOP_N", 3) or 3))
    indices = [i for i, item in enumerate(out) if (item.get("url") or "").strip()]
    if not indices or top_n <= 0:
        return out, meta

    started = time.monotonic()

    def _run_batch(idxs: list[int]) -> None:
        left = _OVERALL_DEADLINE_SECONDS - (time.monotonic() - started)
        if left <= 0.4 or not idxs:
            return
        workers = min(len(idxs), 3)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_read_one, (out[i].get("url") or "").strip()): i for i in idxs
            }
            done, pending = wait(futs, timeout=left)
            for fut in pending:
                fut.cancel()
            for fut in done:
                meta["attempted"] += 1
                idx = futs[fut]
                try:
                    text = fut.result()
                except Exception as e:
                    logger.info("读页任务异常: %s", e)
                    continue
                if text:
                    out[idx]["page_text"] = text
                    out[idx]["read_ok"] = True
                    meta["ok"] += 1

    first, rest = indices[:top_n], indices[top_n:]
    _run_batch(first)
    while meta["ok"] < top_n and rest:
        if _OVERALL_DEADLINE_SECONDS - (time.monotonic() - started) <= 0.4:
            break
        take = min(top_n - meta["ok"], 3)
        batch, rest = rest[:take], rest[take:]
        _run_batch(batch)
    return out, meta
