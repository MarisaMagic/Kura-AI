"""会话附件读取工具（按需读取 pdf/docx/txt/md/csv/xlsx）。"""

from __future__ import annotations

from langchain_core.tools import tool

from app.chat.attachment_bm25_search import search_attachment_text_bm25
from app.chat.attachment_service import format_attachment_hint, read_attachment_text


def make_session_attachment_tools(user_id: int, agent_id: int, session_id: str) -> list:
    @tool
    def search_session_attachment(
        attachment_id: str,
        query: str,
        top_k: int = 5,
        max_snippet_chars: int = 800,
    ) -> str:
        """在单份会话附件全文内做 BM25 关键词检索，返回最相关的若干正文片段（含大致页码/字符范围）。

        何时使用：文档较长或不确定答案在文首/文尾时，根据用户意图构造简短检索句或关键词后再调用，用于定位段落。
        何时不要使用：图片附件；用户已明确只要文首少量内容且可直接 read_session_attachment。
        query：可由用户问题压缩为关键词或短语（中英文均可；中文将分词）。
        attachment_id 来自系统消息中的会话附件列表或 list_session_attachments_brief。
        """
        aid = (attachment_id or "").strip()
        if not aid:
            return "错误：attachment_id 为空。"
        try:
            tk = max(1, min(int(top_k), 20))
        except (TypeError, ValueError):
            tk = 5
        try:
            msc = max(200, min(int(max_snippet_chars), 4000))
        except (TypeError, ValueError):
            msc = 800
        return search_attachment_text_bm25(
            aid,
            (query or "").strip(),
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            top_k=tk,
            max_snippet_chars=msc,
        )

    @tool
    def read_session_attachment(attachment_id: str, max_chars: int = 12000) -> str:
        """读取本会话中「文本类/表格类」附件的正文片段（pdf、docx、txt、md、csv、xlsx）。

        何时使用：用户问题需要引用某份文档/表格的具体文字或数据，且 attachment_id 已知时调用。
        长文档优先用 search_session_attachment 定位后再读本工具，避免只看到文首截断。
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

    return [search_session_attachment, read_session_attachment, list_session_attachments_brief]
