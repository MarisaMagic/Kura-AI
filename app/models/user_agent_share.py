"""智能体定向共享：发布后仅名单内用户可查看并对话。"""

from tortoise import fields

from .base import BaseModel, TimestampMixin


class UserAgentShare(BaseModel, TimestampMixin):
    agent = fields.ForeignKeyField(
        "models.UserAgent", related_name="shares", on_delete=fields.CASCADE, description="智能体"
    )
    user = fields.ForeignKeyField(
        "models.User", related_name="agent_shares", on_delete=fields.CASCADE, description="被共享用户"
    )

    class Meta:
        table = "user_agent_share"
        unique_together = (("agent", "user"),)
