from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

MCP_TRANSPORTS = ("streamable_http", "sse")


def _validate_mcp_url(v: str) -> str:
    from app.utils.ssrf import assert_public_http_url

    return assert_public_http_url((v or "").strip())


class UserAgentMcpServerCreate(BaseModel):
    """
    新增智能体 MCP 服务配置
    :param name: 显示名称
    :param description: 备注说明
    :param transport: 传输类型（streamable_http / sse）
    :param url: MCP 服务 URL（仅 http/https）
    :param headers: 请求头（如 Authorization）；敏感值服务端加密存储
    :param enabled: 对话时是否加载该服务工具
    """

    name: str = Field(..., min_length=1, max_length=100, description="MCP 服务显示名称")
    description: Optional[str] = Field(None, max_length=500, description="备注说明")
    transport: Literal["streamable_http", "sse"] = Field("streamable_http", description="传输类型")
    url: str = Field(..., min_length=1, max_length=512, description="MCP 服务 URL")
    headers: Optional[dict[str, str]] = Field(None, description="请求头（敏感值加密存储）")
    enabled: bool = Field(True, description="对话时是否加载该服务的工具")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_mcp_url(v)


class UserAgentMcpServerUpdate(BaseModel):
    """
    更新智能体 MCP 服务配置
    :param headers: 不传（None）表示保留原请求头；传 {} 表示清空；传新值表示覆盖
    """

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    transport: Optional[Literal["streamable_http", "sse"]] = None
    url: Optional[str] = Field(None, min_length=1, max_length=512)
    headers: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_mcp_url(v)


class UserAgentMcpServerItem(BaseModel):
    """
    MCP 服务列表项（headers 脱敏：只回键名，不回值）
    """

    id: int
    name: str
    description: Optional[str] = None
    transport: str
    url: str
    enabled: bool
    header_keys: list[str] = Field(default_factory=list, description="已配置请求头的键名")
    created_at: str = ""
    updated_at: str = ""


class UserAgentMcpServerListResponse(BaseModel):
    servers: list[UserAgentMcpServerItem]


class UserAgentMcpServerTestRequest(BaseModel):
    """
    测试 MCP 服务连接
    :param transport: 传输类型
    :param url: MCP 服务 URL
    :param headers: 请求头；为 None 且传 server_id 时，使用已保存的请求头测试
    :param server_id: 已保存的服务 ID（编辑场景测试时携带）
    """

    transport: Literal["streamable_http", "sse"] = "streamable_http"
    url: str = Field(..., min_length=1, max_length=512)
    headers: Optional[dict[str, str]] = None
    server_id: Optional[int] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_mcp_url(v)


class UserAgentMcpServerTestResponse(BaseModel):
    ok: bool
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class McpPresetItem(BaseModel):
    key: str
    name: str
    description: str
    transport: str
    url: str
    icon: Optional[str] = Field(None, description="卡片图标：iconify 图标名或图片 URL")
    header_fields: list[dict] = Field(default_factory=list)


class McpPresetListResponse(BaseModel):
    presets: list[McpPresetItem]
