from tortoise import fields

from .base import BaseModel, TimestampMixin


class UserAgentMcpServer(BaseModel, TimestampMixin):
    """智能体自定义 MCP 服务配置（一期仅远程 streamable_http / sse 类型）。"""

    agent = fields.ForeignKeyField(
        "models.UserAgent", related_name="mcp_servers", on_delete=fields.CASCADE
    )
    user = fields.ForeignKeyField(
        "models.User", related_name="agent_mcp_servers", on_delete=fields.CASCADE
    )
    name = fields.CharField(max_length=100, description="MCP 服务显示名称")
    description = fields.TextField(null=True, description="备注说明")
    transport = fields.CharField(
        max_length=32, default="streamable_http", description="传输类型：streamable_http / sse"
    )
    url = fields.CharField(max_length=512, description="MCP 服务 URL（http/https）")
    headers_ciphertext = fields.TextField(
        null=True, description="加密存储的请求头 JSON（如 Authorization/API Key）"
    )
    enabled = fields.BooleanField(default=True, description="对话时是否加载该服务的工具")

    class Meta:
        table = "user_agent_mcp_server"
