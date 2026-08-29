"""MCP 服务连接与工具加载（超时容错 + 工具名冲突检测）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

MCP_CONNECT_TIMEOUT_SECONDS = 12

# 内置工具名集合：MCP 工具与之撞名时跳过（保留内置实现）
_BUILTIN_TOOL_NAMES = {
    "search_knowledge_base",
    "search_knowledge_by_image",
    "search_session_memory",
    "search_session_attachment",
    "read_session_attachment",
    "list_session_attachments_brief",
    "web_search",
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


def _connection_dict(transport: str, url: str, headers: dict[str, str] | None) -> dict[str, Any]:
    conn: dict[str, Any] = {"transport": transport, "url": url}
    if headers:
        conn["headers"] = headers
    return conn


async def _list_tools(transport: str, url: str, headers: dict[str, str] | None) -> list:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from app.utils.ssrf import assert_public_http_url

    # 连接前再检一次，降低 DNS 重绑定窗口
    assert_public_http_url(url)
    client = MultiServerMCPClient({"server": _connection_dict(transport, url, headers)})
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


async def load_agent_mcp_tools(agent_id: int) -> tuple[list, list[dict]]:
    """
    加载指定智能体全部已启用 MCP 服务的 LangChain 工具。
    单个服务连接失败仅记录错误、不中断对话；工具名与内置工具或彼此冲突时跳过。
    :return: (tools, errors)；errors 元素 {"name": 服务名, "error": 错误说明}
    """
    from app.models.user_agent_mcp import UserAgentMcpServer

    rows = await UserAgentMcpServer.filter(agent_id=agent_id, enabled=True).all()
    if not rows:
        return [], []

    all_tools: list = []
    errors: list[dict] = []
    seen_names: set[str] = set(_BUILTIN_TOOL_NAMES)

    for row in rows:
        try:
            tools = await _list_tools(row.transport, row.url, decrypt_headers(row.headers_ciphertext))
        except Exception as e:
            errors.append({"name": row.name, "error": str(e)[:300]})
            continue
        for t in tools:
            tname = getattr(t, "name", "") or ""
            if not tname or tname in seen_names:
                errors.append(
                    {"name": row.name, "error": f"工具名冲突或为空，已跳过：{tname or '(未命名)'}"}
                )
                continue
            seen_names.add(tname)
            all_tools.append(_guard_mcp_tool_output(t))
    return all_tools, errors
