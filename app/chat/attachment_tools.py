"""会话附件读取工具（按需读取 pdf/docx/txt/md/csv/xlsx）。

P2：`prefer_async=True` 时注册 coroutine 版（对话异步 Agent）；
同步路径（/chat、工具线程）沿用同步实现。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from app.chat.attachment_bm25_search import (
    asearch_attachment_text_bm25,
    search_attachment_text_bm25,
)
from app.chat.attachment_service import (
    aformat_attachment_hint,
    aread_attachment_text,
    format_attachment_hint,
    read_attachment_text,
)


def _normalize_search_args(attachment_id: str, top_k: int, max_snippet_chars: int) -> tuple[str, int, int]:
    try:
        tk = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        tk = 5
    try:
        msc = max(200, min(int(max_snippet_chars), 4000))
    except (TypeError, ValueError):
        msc = 800
    return (attachment_id or "").strip(), tk, msc


def make_session_attachment_tools(
    user_id: int, agent_id: int, session_id: str, *, prefer_async: bool = False
) -> list:
    """构造会话附件三工具（BM25 检索 / 正文读取 / 附件列表）。

    :param prefer_async: True 时注册 coroutine 版（对话异步 Agent 专用）
    """

    def _search_attachment_text(
        attachment_id: str,
        query: str,
        top_k: int = 5,
        max_snippet_chars: int = 800,
    ) -> str:
        aid, tk, msc = _normalize_search_args(attachment_id, top_k, max_snippet_chars)
        if not aid:
            return "错误：attachment_id 为空。"
        return search_attachment_text_bm25(  # 调用附件全文检索工具（BM25 打分），返回最相关的若干正文片段（含大致页码/字符范围）
            aid,
            (query or "").strip(),  # 查询关键词
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            top_k=tk,
            max_snippet_chars=msc,
        )

    async def _asearch_attachment_text(
        attachment_id: str,
        query: str,
        top_k: int = 5,
        max_snippet_chars: int = 800,
    ) -> str:
        aid, tk, msc = _normalize_search_args(attachment_id, top_k, max_snippet_chars)
        if not aid:
            return "错误：attachment_id 为空。"
        return await asearch_attachment_text_bm25(
            aid,
            (query or "").strip(),
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            top_k=tk,
            max_snippet_chars=msc,
        )

    def _read_attachment(attachment_id: str, max_chars: int = 12000) -> str:
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

    async def _aread_attachment(attachment_id: str, max_chars: int = 12000) -> str:
        aid = (attachment_id or "").strip()
        if not aid:
            return "错误：attachment_id 为空。"
        return await aread_attachment_text(
            aid,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            max_chars=min(max(1000, max_chars), 50000),
        )

    def _list_attachments() -> str:
        return format_attachment_hint(user_id, agent_id, session_id) or "本会话暂无附件。"

    async def _alist_attachments() -> str:
        return await aformat_attachment_hint(user_id, agent_id, session_id) or "本会话暂无附件。"

    search_options = {
        "name": "search_session_attachment",
        "description": (
            "在单份会话附件全文内做 BM25 关键词检索，返回最相关的若干正文片段（含大致页码/字符范围）。\n\n"
            "何时使用：文档较长或不确定答案在文首/文尾时，根据用户意图构造简短检索句或关键词后再调用，用于定位段落。\n"
            "何时不要使用：图片附件；用户已明确只要文首少量内容且可直接 read_session_attachment。\n"
            "query：可由用户问题压缩为关键词或短语（中英文均可；中文将分词）。\n"
            "attachment_id 来自系统消息中的会话附件列表或 list_session_attachments_brief。"
        ),
    }
    read_options = {
        "name": "read_session_attachment",
        "description": (
            "读取本会话中「文本类/表格类」附件的正文片段（pdf、docx、txt、md、csv、xlsx）。\n\n"
            "何时使用：用户问题需要引用某份文档/表格的具体文字或数据，且 attachment_id 已知时调用。\n"
            "长文档优先用 search_session_attachment 定位后再读本工具，避免只看到文首截断。\n"
            "何时不要使用：kind为 image 的附件；用户消息里已包含的图片（多模态）请直接根据图像回答，不要调用本工具试图「读图」。\n"
            "参数 attachment_id 来自系统消息中的会话附件列表或 list_session_attachments_brief 的返回。"
        ),
    }
    list_options = {
        "name": "list_session_attachments_brief",
        "description": (
            "返回本会话已上传附件的 attachment_id、文件名、类型、大小。\n\n"
            "何时使用：仅在需要从多份「文档/表格类」附件中挑选 read_session_attachment 的目标、而系统消息里又未列出足够信息时调用。\n"
            "何时不要重复调用：若系统消息已包含同一份附件列表，禁止为相同信息再次调用本工具。\n"
            "图片相关：若用户已在当前消息中附上图片并询问图像内容，不要调用本工具；应直接基于多模态输入作答。"
        ),
    }

    if prefer_async:
        return [
            StructuredTool.from_function(coroutine=_asearch_attachment_text, **search_options),
            StructuredTool.from_function(coroutine=_aread_attachment, **read_options),
            StructuredTool.from_function(coroutine=_alist_attachments, **list_options),
        ]
    return [
        StructuredTool.from_function(func=_search_attachment_text, **search_options),
        StructuredTool.from_function(func=_read_attachment, **read_options),
        StructuredTool.from_function(func=_list_attachments, **list_options),
    ]