from tortoise import fields

from .base import BaseModel, TimestampMixin


class UserAgent(BaseModel, TimestampMixin):
    user = fields.ForeignKeyField("models.User", related_name="user_agents", on_delete=fields.CASCADE)
    name = fields.CharField(max_length=100, description="智能体名称", index=True)
    model_name = fields.CharField(max_length=100, description="模型名称")
    api_key_env_name = fields.CharField(max_length=100, description="API Key 环境变量名")
    description = fields.TextField(null=True, description="简介")
    system_prompt = fields.TextField(null=True, description="提示词")
    enable_web = fields.BooleanField(default=False, description="联网能力")
    enable_code = fields.BooleanField(default=False, description="写代码能力")
    opening_message = fields.TextField(null=True, description="开场白")
    # SQLite 列默认仍为 0.7（见迁移）；应用层默认 0.1 由 Pydantic / 前端表单提供
    temperature = fields.FloatField(default=0.7, description="温度")
    avatar_filename = fields.CharField(max_length=255, null=True, description="自定义头像文件名")

    class Meta:
        table = "user_agent"
