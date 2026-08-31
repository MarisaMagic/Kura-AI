from typing import List, Optional

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
    opening_message: Optional[str] = None
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    supports_vision: bool = Field(
        default=False,
        description="启用后允许本会话图片附件以多模态理解（需模型支持视觉）；关闭时上传图片将被拒绝",
    )
    sub_model_name: Optional[str] = Field(
        None,
        max_length=100,
        description="子智能体模型名称（记忆重写/选档/RAG 打分改写等打杂任务）；留空则跟随主模型配置",
    )
    sub_base_url: Optional[str] = Field(
        None,
        max_length=512,
        description="子智能体 Base URL；留空则跟随主模型配置",
    )

    @field_validator("base_url", "sub_base_url", mode="before")
    @classmethod
    def normalize_base_url(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

    @field_validator("base_url", "sub_base_url")
    @classmethod
    def validate_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        HttpUrl(v)
        from app.utils.ssrf import assert_public_http_url

        return assert_public_http_url(v)

    @field_validator("sub_model_name", mode="before")
    @classmethod
    def normalize_sub_model_name(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v


class UserAgentCreate(UserAgentCommon):
    api_key: str = Field(..., min_length=1, max_length=4096, description="模型厂商 API Key，服务端加密存储")
    sub_api_key: Optional[str] = Field(
        None,
        max_length=4096,
        description="子智能体 API Key；填写子智能体模型时必填，留空则跟随主配置",
    )


class UserAgentUpdate(UserAgentCommon):
    id: int = Field(..., description="智能体 ID")
    api_key: Optional[str] = Field(
        None,
        max_length=4096,
        description="留空或不传则保留原 Key；传入新值则覆盖",
    )
    sub_api_key: Optional[str] = Field(
        None,
        max_length=4096,
        description="不传则保留原子 Key；传空字符串则清除（恢复跟随主配置）；传入新值则覆盖",
    )


class SubLlmTestIn(BaseModel):
    """子智能体连通性测试入参；api_key 为空且带 agent_id 时使用该智能体已保存的子 Key。"""

    model_name: str = Field(..., min_length=1, max_length=100, description="子智能体模型名称")
    base_url: Optional[str] = Field(None, max_length=512, description="子智能体 Base URL")
    api_key: Optional[str] = Field(None, max_length=4096, description="子智能体 API Key")
    agent_id: Optional[int] = Field(None, description="智能体 ID（编辑页测试已保存 Key 时传）")

    @field_validator("base_url", mode="before")
    @classmethod
    def normalize_test_base_url(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

    @field_validator("base_url")
    @classmethod
    def validate_test_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        HttpUrl(v)
        from app.utils.ssrf import assert_public_http_url

        return assert_public_http_url(v)


class UserAgentPublishIn(BaseModel):
    agent_id: int = Field(..., description="智能体 ID")
    user_ids: List[int] = Field(..., min_length=1, description="指定共享的用户 ID 列表（至少 1 人）")


class UserAgentOfflineIn(BaseModel):
    agent_id: int = Field(..., description="智能体 ID")


class UserAgentShareIn(BaseModel):
    agent_id: int = Field(..., description="智能体 ID")
    user_ids: List[int] = Field(..., min_length=1, description="要添加/移除的共享用户 ID 列表")
