from typing import Optional

from pydantic import BaseModel, Field


class UserAgentCommon(BaseModel):
    name: str = Field(..., max_length=100, description="智能体名称")
    model_name: str = Field(..., max_length=100, description="模型名称")
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    enable_web: bool = False
    enable_code: bool = False
    opening_message: Optional[str] = None
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)


class UserAgentCreate(UserAgentCommon):
    api_key: str = Field(..., min_length=1, max_length=4096, description="模型厂商 API Key，服务端加密存储")


class UserAgentUpdate(UserAgentCommon):
    id: int = Field(..., description="智能体 ID")
    api_key: Optional[str] = Field(
        None,
        max_length=4096,
        description="留空或不传则保留原 Key；传入新值则覆盖",
    )
