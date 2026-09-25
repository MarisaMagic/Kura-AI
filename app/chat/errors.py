"""对话存储相关的领域异常。"""

from __future__ import annotations


class ChatQuotaExceeded(Exception):
    """写入侧配额超限（会话数 / 每会话消息数等）。

    :param reason: session_limit / message_limit
    :param limit: 触发的上限值（用于展示）
    """

    def __init__(self, reason: str, *, limit: int = 0, detail: str = ""):
        self.reason = str(reason or "")
        self.limit = int(limit or 0)
        self.detail = detail or self._default_detail(self.reason, self.limit)
        super().__init__(self.detail)

    @staticmethod
    def _default_detail(reason: str, limit: int) -> str:
        if reason == "session_limit":
            return f"会话数已达上限（{limit}），请删除部分旧会话后再试。"
        if reason == "message_limit":
            return f"本会话消息数已达上限（{limit}），请新建会话继续。"
        return "已达到存储配额上限。"
