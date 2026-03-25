from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.user_agent_avatar import validate_api_key_env_name


class UserAgentBase(BaseModel):
    name: str = Field(..., max_length=100, description="智能体名称")
    model_name: str = Field(..., max_length=100, description="模型名称")
    api_key_env_name: str = Field(..., max_length=100, description="API Key 环境变量名")
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    enable_web: bool = False
    enable_code: bool = False
    opening_message: Optional[str] = None
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    @field_validator("api_key_env_name")
    @classmethod
    def check_env_name(cls, v: str) -> str:
        if not validate_api_key_env_name(v):
            raise ValueError("环境变量名须为大写字母、数字、下划线，且以大写字母开头")
        return v


class UserAgentCreate(UserAgentBase):
    pass


class UserAgentUpdate(UserAgentBase):
    id: int = Field(..., description="智能体 ID")
