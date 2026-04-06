from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class UserAgentCommon(BaseModel):
    name: str = Field(..., max_length=100, description="智能体名称")
    model_name: str = Field(..., max_length=100, description="模型名称")
    base_url: Optional[str] = Field(
        None,
        max_length=512,
        description="OpenAI 兼容接口 Base URL，留空则使用 SDK 默认地址",
    )
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    enable_web: bool = False
    enable_code: bool = False
    opening_message: Optional[str] = None
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    @field_validator("base_url", mode="before")
    @classmethod
    def normalize_base_url(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        HttpUrl(v)
        return v


class UserAgentCreate(UserAgentCommon):
    api_key: str = Field(..., min_length=1, max_length=4096, description="模型厂商 API Key，服务端加密存储")


class UserAgentUpdate(UserAgentCommon):
    id: int = Field(..., description="智能体 ID")
    api_key: Optional[str] = Field(
        None,
        max_length=4096,
        description="留空或不传则保留原 Key；传入新值则覆盖",
    )
