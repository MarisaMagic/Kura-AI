from tortoise import fields

from .base import BaseModel, TimestampMixin


class UserAgent(BaseModel, TimestampMixin):
    user = fields.ForeignKeyField("models.User", related_name="user_agents", on_delete=fields.CASCADE)
    name = fields.CharField(max_length=100, description="智能体名称", index=True)
    model_name = fields.CharField(max_length=100, description="模型名称")
    base_url = fields.CharField(max_length=512, null=True, description="OpenAI 兼容 API Base URL")
    api_key_ciphertext = fields.TextField(null=True, description="加密存储的 API Key")
    description = fields.TextField(null=True, description="简介")
    system_prompt = fields.TextField(null=True, description="提示词")
    # 已弃用：联网能力改为对话页「联网搜索」开关（use_web_search）控制；字段保留仅为兼容历史数据
    enable_web = fields.BooleanField(default=False, description="联网能力（已弃用）")
    opening_message = fields.TextField(null=True, description="开场白")
    temperature = fields.FloatField(default=0.1, description="温度")
    avatar_filename = fields.CharField(max_length=255, null=True, description="自定义头像文件名")
    supports_vision = fields.BooleanField(
        default=False,
        description="启用后允许会话中的图片附件以多模态方式理解（需模型支持视觉）",
    )
    is_published = fields.BooleanField(default=False, description="已发布（仅共享名单内用户可查看并对话）")

    class Meta:
        table = "user_agent"
