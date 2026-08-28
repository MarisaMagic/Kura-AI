"""MCP 工具动态加载：按智能体已启用配置连接远程 MCP 服务并转为 LangChain 工具。"""

from app.mcp_client.presets import MCP_SERVER_PRESETS, get_preset_by_key
from app.mcp_client.service import load_agent_mcp_tools, test_mcp_server_connection

__all__ = [
    "MCP_SERVER_PRESETS",
    "get_preset_by_key",
    "load_agent_mcp_tools",
    "test_mcp_server_connection",
]
