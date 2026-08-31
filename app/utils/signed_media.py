"""媒体文件 HMAC 签名 URL：头像 / 智能体头像 / 知识库图片。"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.settings import settings

KIND_USER_AVATAR = "user_avatar"
KIND_AGENT_AVATAR = "user_agents_avatar"
KIND_KB_IMAGE = "user_agent_images"

_KIND_PREFIX = {
    KIND_USER_AVATAR: "/api/v1/media/user_avatar",
    KIND_AGENT_AVATAR: "/api/v1/media/user_agents_avatar",
    KIND_KB_IMAGE: "/api/v1/media/user_agent_images",
}
_PREFIX_KIND = {v: k for k, v in _KIND_PREFIX.items()}

_MEDIA_URL_RE = re.compile(
    r"(?P<origin>(?:https?://[^/\s\"')]+)?)(?P<prefix>/api/v1/media/"
    r"(?:user_avatar|user_agents_avatar|user_agent_images)/)"
    r"(?P<path>[^\s\"'?)]+)(?:\?[^\s\"')]+)?"
)


def _kind_root(kind: str) -> Path:
    if kind == KIND_USER_AVATAR:
        return Path(settings.USER_AVATAR_ROOT)
    if kind == KIND_AGENT_AVATAR:
        return Path(settings.USER_AGENT_AVATAR_ROOT)
    if kind == KIND_KB_IMAGE:
        return Path(settings.USER_AGENT_KB_IMAGES_ROOT)
    raise HTTPException(status_code=404, detail="未知媒体类型")


def _normalize_relpath(relpath: str) -> str:
    return (relpath or "").strip().replace("\\", "/").lstrip("/")


def _sign(kind: str, relpath: str, exp: int) -> str:
    payload = f"{kind}\n{_normalize_relpath(relpath)}\n{exp}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def sign_media_url(kind: str, relpath: str, *, absolute: bool = False) -> str:
    """生成带 exp/sig 的媒体 URL。absolute=True 时前置 PUBLIC_API_BASE。"""
    rel = _normalize_relpath(relpath)
    if not rel:
        return ""
    prefix = _KIND_PREFIX.get(kind)
    if not prefix:
        return ""
    ttl = max(60, int(getattr(settings, "MEDIA_SIGNED_URL_TTL_SECONDS", 86400)))
    exp = int(time.time()) + ttl
    sig = _sign(kind, rel, exp)
    path_part = f"{prefix}/{rel}?{urlencode({'exp': exp, 'sig': sig})}"
    if absolute:
        base = (getattr(settings, "PUBLIC_API_BASE", None) or "").strip().rstrip("/")
        if base:
            return f"{base}{path_part}"
    return path_part


def verify_media_signature(kind: str, relpath: str, exp: int, sig: str) -> None:
    rel = _normalize_relpath(relpath)
    if not rel or not sig:
        raise HTTPException(status_code=403, detail="无效的媒体签名")
    try:
        exp_i = int(exp)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="无效的媒体签名") from exc
    if exp_i < int(time.time()):
        raise HTTPException(status_code=403, detail="媒体链接已过期")
    expected = _sign(kind, rel, exp_i)
    if not hmac.compare_digest(expected, str(sig)):
        raise HTTPException(status_code=403, detail="无效的媒体签名")


def resolve_media_file(kind: str, relpath: str) -> Path:
    rel = _normalize_relpath(relpath)
    if not rel or ".." in Path(rel).parts:
        raise HTTPException(status_code=403, detail="非法路径")
    root = _kind_root(kind).resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="非法路径") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return candidate


def serve_signed_media(kind: str, relpath: str, exp: int, sig: str) -> FileResponse:
    verify_media_signature(kind, relpath, exp, sig)
    path = resolve_media_file(kind, relpath)
    return FileResponse(
        path,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"},
    )


def resign_media_url(url: str) -> str:
    """将已有媒体 URL 换成当前有效签名的同源相对路径。非媒体 URL 原样返回。

    始终去掉 http(s) origin / PUBLIC_API_BASE，避免前端 CSP img-src 'self' 拦截跨源图片。
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    split = urlsplit(raw)
    path = split.path or ""
    kind = None
    rel = ""
    for prefix, k in _PREFIX_KIND.items():
        if path == prefix or path.startswith(prefix + "/"):
            kind = k
            rel = path[len(prefix) :].lstrip("/")
            break
    if not kind or not rel:
        return raw
    return sign_media_url(kind, rel, absolute=False)


def resign_media_urls_in_text(text: str) -> str:
    if not text or "/api/v1/media/" not in text:
        return text

    def _repl(m: re.Match) -> str:
        prefix = m.group("prefix")
        rel = m.group("path")
        kind = _PREFIX_KIND.get(prefix.rstrip("/"))
        if not kind:
            return m.group(0)
        return sign_media_url(kind, rel, absolute=False)

    return _MEDIA_URL_RE.sub(_repl, text)


def _resign_obj(value: Any) -> Any:
    if isinstance(value, str):
        if "/api/v1/media/" in value:
            if value.strip().startswith("http") or value.strip().startswith("/api/v1/media/"):
                if " " not in value.strip() and "\n" not in value:
                    return resign_media_url(value)
            return resign_media_urls_in_text(value)
        return value
    if isinstance(value, list):
        return [_resign_obj(x) for x in value]
    if isinstance(value, dict):
        return {k: _resign_obj(v) for k, v in value.items()}
    return value


def resign_message_payload(msg: dict[str, Any]) -> dict[str, Any]:
    """会话消息返回前重签媒体 URL（正文 / sources / rag_steps / content_json）。"""
    out = dict(msg)
    content = out.get("content")
    if isinstance(content, str):
        out["content"] = resign_media_urls_in_text(content)
    elif isinstance(content, list):
        out["content"] = _resign_obj(content)
    for key in ("sources", "rag_steps", "rag_trace", "content_json"):
        if out.get(key) is not None:
            out[key] = _resign_obj(out[key])
    return out
