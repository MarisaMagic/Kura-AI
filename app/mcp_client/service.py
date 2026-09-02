"""MCP 服务连接与工具加载（超时容错 + 工具名冲突检测）。"""

from __future__ import annotations

import asyncio
import json
import logging
from app.mcp_client.tool_policy import wrap_mcp_tool_with_confirmation
from typing import Any

MCP_CONNECT_TIMEOUT_SECONDS = 12
MCP_TOOL_SCHEMA_TTL_SECONDS = 120

logger = logging.getLogger(__name__)

# 内置工具名集合：MCP 工具与之撞名时跳过（保留内置实现）
_BUILTIN_TOOL_NAMES = {
    "search_knowledge_base",
    "search_knowledge_by_image",
    "search_session_memory",
    "search_session_attachment",
    "read_session_attachment",
    "list_session_attachments_brief",
    "web_search",
    "fetch_url",
    "web_image_search",
}


def decrypt_headers(headers_ciphertext: str | None) -> dict[str, str]:
    """解密请求头 JSON；异常或为空时返回空 dict。"""
    from app.utils.api_key_crypto import decrypt_api_key_safe

    plain = decrypt_api_key_safe(headers_ciphertext)
    if not plain:
        return {}
    try:
        data = json.loads(plain)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def encrypt_headers(headers: dict[str, str] | None) -> str | None:
    """加密请求头 dict 为密文；空 dict 返回 None。"""
    from app.utils.api_key_crypto import encrypt_api_key

    if not headers:
        return None
    cleaned = {str(k).strip(): str(v) for k, v in headers.items() if str(k).strip()}
    if not cleaned:
        return None
    return encrypt_api_key(json.dumps(cleaned, ensure_ascii=False))


def mcp_tool_schema_cache_key(agent_id: int, server_id: int) -> str:
    return f"mcp_tool_schema:{int(agent_id)}:{int(server_id)}"


def invalidate_mcp_tool_schema_cache(agent_id: int, server_id: int) -> None:
    from app.chat.cache import cache

    cache.delete(mcp_tool_schema_cache_key(agent_id, server_id))


def _tool_args_json_schema(tool: Any) -> dict[str, Any]:
    schema_cls = getattr(tool, "args_schema", None)
    if schema_cls is not None:
        try:
            if hasattr(schema_cls, "model_json_schema"):
                out = schema_cls.model_json_schema()
                if isinstance(out, dict):
                    return out
            if hasattr(schema_cls, "schema"):
                out = schema_cls.schema()
                if isinstance(out, dict):
                    return out
        except Exception:
            pass
    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return {"type": "object", "properties": args}
    return {"type": "object", "properties": {}}


def _snapshot_mcp_tools(tools: list) -> list[dict[str, Any]]:
    snaps: list[dict[str, Any]] = []
    for t in tools:
        snaps.append(
            {
                "name": getattr(t, "name", "") or "",
                "description": getattr(t, "description", "") or "",
                "args_schema": _tool_args_json_schema(t),
            }
        )
    return snaps


def _frozen_args_schema(json_schema: dict | None, *, tool_name: str):
    from pydantic import BaseModel, ConfigDict

    schema = dict(json_schema) if isinstance(json_schema, dict) else {"type": "object", "properties": {}}

    class Frozen(BaseModel):
        model_config = ConfigDict(extra="allow")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return schema

    safe = "".join(c if c.isalnum() else "_" for c in (tool_name or "mcp")) or "mcp"
    Frozen.__name__ = f"{safe}StubArgs"
    Frozen.__qualname__ = Frozen.__name__
    return Frozen


def _stub_mcp_tools_from_snapshot(snaps: list, server_name: str, error: str) -> list:
    """用上次成功的 schema 包一层「MCP 不可用」stub，避免 tools 列表整表消失。"""
    from langchain_core.tools import StructuredTool

    err_text = f"MCP_UNAVAILABLE: 服务「{server_name}」当前不可用：{error[:200]}"
    tools: list = []
    if not isinstance(snaps, list):
        return tools
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        name = str(snap.get("name") or "").strip()
        if not name:
            continue
        desc = str(snap.get("description") or name)
        raw_schema = snap.get("args_schema")
        schema = _frozen_args_schema(raw_schema if isinstance(raw_schema, dict) else None, tool_name=name)

        async def _coro(*args: Any, _msg: str = err_text, **kwargs: Any) -> str:
            return _msg

        tools.append(
            StructuredTool.from_function(
                coroutine=_coro,
                name=name,
                description=desc,
                args_schema=schema,
            )
        )
    return tools


def _connection_dict(transport: str, url: str, headers: dict[str, str] | None) -> dict[str, Any]:
    conn: dict[str, Any] = {"transport": transport, "url": url}
    if headers:
        conn["headers"] = headers
    return conn


async def _list_tools(transport: str, url: str, headers: dict[str, str] | None) -> list:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from app.utils.egress import build_mcp_httpx_client_factory

    # 连接前再检一次，降低 DNS 重绑定窗口
    conn = _connection_dict(transport, url, headers)
    conn["httpx_client_factory"] = build_mcp_httpx_client_factory(url)
    client = MultiServerMCPClient({"server": conn})
    return await asyncio.wait_for(client.get_tools(), timeout=MCP_CONNECT_TIMEOUT_SECONDS)


async def test_mcp_server_connection(
    transport: str, url: str, headers: dict[str, str] | None
) -> dict[str, Any]:
    """测试连接：成功返回工具名列表；失败抛出异常由调用方包装。"""
    tools = await _list_tools(transport, url, headers)
    return {
        "ok": True,
        "tool_count": len(tools),
        "tool_names": [(getattr(t, "name", "") or "") for t in tools],
    }


def _guard_mcp_tool_output(tool: Any) -> Any:
    """包装 MCP 工具：字符串输出按非可信内容隔离并截断，缓解间接提示注入与上下文膨胀。"""
    from langchain_core.tools import StructuredTool

    from app.utils.content_guard import guard_untrusted_content

    async def _guarded_coroutine(*args: Any, _tool: Any = tool, **kwargs: Any) -> Any:
        result = await _tool.ainvoke(kwargs if kwargs else (args[0] if args else {}))
        if isinstance(result, str):
            from app.settings import settings

            return guard_untrusted_content(
                result, max_chars=int(getattr(settings, "MCP_TOOL_RESULT_MAX_CHARS", 8000))
            )
        return result

    return StructuredTool.from_function(
        coroutine=_guarded_coroutine,
        name=getattr(tool, "name", None) or "mcp_tool",
        description=getattr(tool, "description", "") or "",
        args_schema=getattr(tool, "args_schema", None),
    )


async def load_agent_mcp_tools(
    agent_id: int,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
) -> tuple[list, list[dict]]:
    """
    加载指定智能体全部已启用 MCP 服务的 LangChain 工具。
    单个服务连接失败仅记录错误、不中断对话；工具名与内置工具或彼此冲突时跳过。
    :return: (tools, errors)；errors 元素 {"name": 服务名, "error": 错误说明}
    """
    from app.chat.cache import cache
    from app.models.user_agent_mcp import UserAgentMcpServer

    rows = await UserAgentMcpServer.filter(agent_id=agent_id, enabled=True).all()
    if not rows:
        return [], []

    all_tools: list = []
    errors: list[dict] = []
    seen_names: set[str] = set(_BUILTIN_TOOL_NAMES)

    for row in rows:
        schema_key = mcp_tool_schema_cache_key(agent_id, row.id)
        try:
            tools = await _list_tools(row.transport, row.url, decrypt_headers(row.headers_ciphertext))
            cache.set_json(schema_key, _snapshot_mcp_tools(tools), MCP_TOOL_SCHEMA_TTL_SECONDS)
        except Exception as e:
            err_text = str(e)[:300]
            errors.append({"name": row.name, "error": err_text})
            cached = cache.get_json(schema_key)
            if cached:
                logger.warning(
                    "MCP list_tools 失败，使用缓存 schema stub agent_id=%s server=%s: %s",
                    agent_id,
                    row.name,
                    err_text,
                )
                tools = _stub_mcp_tools_from_snapshot(cached, row.name, err_text)
            else:
                continue
        for t in tools:
            tname = getattr(t, "name", "") or ""
            if not tname or tname in seen_names:
                errors.append(
                    {"name": row.name, "error": f"工具名冲突或为空，已跳过：{tname or '(未命名)'}"}
                )
                continue
            seen_names.add(tname)
            guarded = _guard_mcp_tool_output(t)
            all_tools.append(
                wrap_mcp_tool_with_confirmation(
                    guarded,
                    server_name=row.name,
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    confirm_policy=getattr(row, "confirm_policy", "auto") or "auto",
                )
            )
    all_tools.sort(key=lambda t: str(getattr(t, "name", "") or ""))
    return all_tools, errors
