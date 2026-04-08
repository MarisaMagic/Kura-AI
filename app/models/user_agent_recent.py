"""用户最近使用的智能体（服务端记录，每用户最多保留 3 条）。"""

from tortoise import fields

from .base import BaseModel


class UserAgentRecent(BaseModel):
    user = fields.ForeignKeyField(
        "models.User", related_name="agent_recents", on_delete=fields.CASCADE, description="用户"
    )
    agent = fields.ForeignKeyField(
        "models.UserAgent", related_name="recent_usages", on_delete=fields.CASCADE, description="智能体"
    )
    last_used_at = fields.DatetimeField(index=True, description="最近使用时间")

    class Meta:
        table = "user_agent_recent"
        unique_together = (("user", "agent"),)
