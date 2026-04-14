"""会话附件读取工具（按需读取 pdf/docx/txt/md/csv/xlsx）。"""

from __future__ import annotations

from langchain_core.tools import tool

from app.chat.attachment_service import format_attachment_hint, read_attachment_text


def make_session_attachment_tools(user_id: int, agent_id: int, session_id: str) -> list:
    @tool
    def read_session_attachment(attachment_id: str, max_chars: int = 12000) -> str:
        """读取本会话中「文本类/表格类」附件的正文片段（pdf、docx、txt、md、csv、xlsx）。

        何时使用：用户问题需要引用某份文档/表格的具体文字或数据，且 attachment_id 已知时调用。
        何时不要使用：kind为 image 的附件；用户消息里已包含的图片（多模态）请直接根据图像回答，不要调用本工具试图「读图」。
        参数 attachment_id 来自系统消息中的会话附件列表或 list_session_attachments_brief 的返回。
        """
        aid = (attachment_id or "").strip()
        if not aid:
            return "错误：attachment_id 为空。"
        return read_attachment_text(
            aid,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            max_chars=min(max(1000, max_chars), 50000),
        )

    @tool
    def list_session_attachments_brief() -> str:
        """返回本会话已上传附件的 attachment_id、文件名、类型、大小。

        何时使用：仅在需要从多份「文档/表格类」附件中挑选 read_session_attachment 的目标、而系统消息里又未列出足够信息时调用。
        何时不要重复调用：若系统消息已包含同一份附件列表，禁止为相同信息再次调用本工具。
        图片相关：若用户已在当前消息中附上图片并询问图像内容，不要调用本工具；应直接基于多模态输入作答。
        """
        return format_attachment_hint(user_id, agent_id, session_id) or "本会话暂无附件。"

    return [read_session_attachment, list_session_attachments_brief]
