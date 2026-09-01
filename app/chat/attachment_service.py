"""
会话附件：对象存储落库、校验、读取（供多模态与 read_session_attachment 工具使用）。
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func

from app.chat.database import SessionLocal
from app.chat.db_models import ChatAttachment as ChatAttachmentRow
from app.core import object_storage as obs
from app.settings import settings

# 支持的上传附件类型
_EXT_KIND = {
    ".txt": "document",
    ".md": "document",
    ".pdf": "document",
    ".docx": "document",
    ".csv": "table",
    ".xlsx": "table",
    ".xls": "table",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".gif": "image",
}


def _safe_segment(s: str, max_len: int = 96) -> str:
    """
    根据会话ID获取安全的路径段，使用 re.sub 函数
    :param s: 会话ID
    :param max_len: 最大长度
    :return: 安全的路径段
    """
    t = (s or "").strip()
    if not t:
        return "session"
    out = re.sub(r"[^a-zA-Z0-9._-]+", "_", t)
    return (out[:max_len] or "session").strip("_") or "session"


def guess_mime_from_path(filename: str) -> str:
    """
    根据文件路径猜测文件类型，使用 mimetypes.guess_type 函数
    :param filename: 文件路径
    :return: 文件类型
    """
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def classify_kind(filename: str) -> str:
    """
    根据文件路径获取文件类型，使用 _EXT_KIND 字典
    :param filename: 文件路径
    :return: 文件类型
    """
    suf = Path(filename).suffix.lower()
    return _EXT_KIND.get(suf, "other")


def allowed_extension(filename: str) -> bool:
    """
    判断文件是否允许上传，使用 classify_kind 函数
    :param filename: 文件路径
    :return: 是否允许上传
    """
    return classify_kind(filename) != "other"


def attachment_object_key(stored_relpath: str) -> str:
    """
    由 DB 中的存储相对路径得到对象存储 key
    :param stored_relpath: 存储相对路径
    :return: 对象 key
    """
    return obs.join_key(settings.USER_AGENT_CHAT_UPLOAD_ROOT, stored_relpath)


def _agent_key_prefix(user_id: int, agent_id: int) -> str:
    return obs.join_key(
        settings.USER_AGENT_CHAT_UPLOAD_ROOT, f"user_{user_id}", f"agent_{agent_id}"
    )


def _session_key_prefix(user_id: int, agent_id: int, session_id: str) -> str:
    return obs.join_key(_agent_key_prefix(user_id, agent_id), _safe_segment(session_id))


def purge_attachments_for_session(user_id: int, agent_id: int, session_id: str) -> None:
    """删除某会话的附件库记录与对象存储中的对象。"""
    db = SessionLocal()
    try:
        db.query(ChatAttachmentRow).filter(
            ChatAttachmentRow.user_id == user_id,
            ChatAttachmentRow.agent_id == agent_id,
            ChatAttachmentRow.session_id == session_id,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    obs.delete_prefix(_session_key_prefix(user_id, agent_id, session_id))


def purge_attachments_for_agent(user_id: int, agent_id: int) -> None:
    """删除某智能体下全部会话附件记录与对象存储中的对象。"""
    db = SessionLocal()
    try:
        db.query(ChatAttachmentRow).filter(
            ChatAttachmentRow.user_id == user_id,
            ChatAttachmentRow.agent_id == agent_id,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    obs.delete_prefix(_agent_key_prefix(user_id, agent_id))


def get_attachment_row(
    attachment_id: str, *, user_id: int, agent_id: int, session_id: str
) -> ChatAttachmentRow | None:
    """
    根据附件ID、用户ID、智能体ID、会话ID获取附件行
    :param attachment_id: 附件ID
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 附件行
    """
    db = SessionLocal()
    try:
        return (
            db.query(ChatAttachmentRow)
            .filter(
                ChatAttachmentRow.id == attachment_id,
                ChatAttachmentRow.user_id == user_id,
                ChatAttachmentRow.agent_id == agent_id,
                ChatAttachmentRow.session_id == session_id,
            )
            .first()
        )
    finally:
        db.close()


def file_bytes_for_attachment(
    attachment_id: str, *, user_id: int, agent_id: int, session_id: str
) -> bytes | None:
    """
    根据附件ID、用户ID、智能体ID、会话ID获取附件字节
    :param attachment_id: 附件ID
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 附件字节
    """
    row = get_attachment_row(attachment_id, user_id=user_id, agent_id=agent_id, session_id=session_id)
    if not row:
        return None
    try:
        return obs.read_bytes(attachment_object_key(row.stored_relpath))
    except obs.ObjectNotFoundError:
        return None


def list_session_attachments(user_id: int, agent_id: int, session_id: str) -> list[dict[str, Any]]:
    """
    根据用户ID、智能体ID、会话ID获取会话附件列表
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 会话附件列表
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(ChatAttachmentRow)
            .filter(
                ChatAttachmentRow.user_id == user_id,
                ChatAttachmentRow.agent_id == agent_id,
                ChatAttachmentRow.session_id == session_id,
            )
            .order_by(ChatAttachmentRow.created_at.asc())
            .all()
        )
        return [
            {
                "id": r.id,
                "filename": r.original_filename,
                "kind": r.kind,
                "mime": r.mime,
                "size_bytes": r.size_bytes,
            }
            for r in rows
        ]
    finally:
        db.close()


def session_attachments_count(user_id: int, agent_id: int, session_id: str) -> int:
    """
    根据用户ID、智能体ID、会话ID获取会话附件数量
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 会话附件数量
    """
    db = SessionLocal()
    try:
        return (
            db.query(ChatAttachmentRow)
            .filter(
                ChatAttachmentRow.user_id == user_id,
                ChatAttachmentRow.agent_id == agent_id,
                ChatAttachmentRow.session_id == session_id,
            )
            .count()
        )
    finally:
        db.close()


def count_session_image_attachments(user_id: int, agent_id: int, session_id: str) -> int:
    """
    当前会话中 kind 为 image 的附件数量（与上传时 classify_kind 一致）。
    """
    db = SessionLocal()
    try:
        return (
            db.query(ChatAttachmentRow)
            .filter(
                ChatAttachmentRow.user_id == user_id,
                ChatAttachmentRow.agent_id == agent_id,
                ChatAttachmentRow.session_id == session_id,
                ChatAttachmentRow.kind == "image",
            )
            .count()
        )
    finally:
        db.close()


def session_attachments_total_bytes(user_id: int, agent_id: int, session_id: str) -> int:
    """
    根据用户ID、智能体ID、会话ID获取会话附件总大小
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 会话附件总大小
    """
    db = SessionLocal()
    try:
        q = db.query(func.coalesce(func.sum(ChatAttachmentRow.size_bytes), 0)).filter(
            ChatAttachmentRow.user_id == user_id,
            ChatAttachmentRow.agent_id == agent_id,
            ChatAttachmentRow.session_id == session_id,
        )
        return int(q.scalar() or 0)
    finally:
        db.close()


def save_uploaded_file(
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    original_filename: str,
    raw: bytes,
) -> dict[str, Any]:
    """
    保存上传文件并写入 mg_chat_attachments。返回 {id, filename, kind, mime, size_bytes}。
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param original_filename: 原始文件名
    :param raw: 文件字节
    :return: 附件ID、文件名、文件类型、文件大小
    """
    name = (original_filename or "file").strip()
    if not allowed_extension(name):
        raise ValueError(f"不支持的文件类型：{Path(name).suffix or '（无扩展名）'}")

    from app.utils.upload_sniff import assert_upload_magic

    assert_upload_magic(name, raw)

    size = len(raw)
    if size <= 0:
        raise ValueError("空文件")
    if size > int(settings.CHAT_UPLOAD_MAX_BYTES_PER_FILE):
        raise ValueError(f"单文件超过上限（{settings.CHAT_UPLOAD_MAX_BYTES_PER_FILE} 字节）")

    total = session_attachments_total_bytes(user_id, agent_id, session_id)
    if total + size > int(settings.CHAT_UPLOAD_MAX_SESSION_BYTES):
        raise ValueError("本会话附件总大小超过上限")

    cnt = session_attachments_count(user_id, agent_id, session_id)
    if cnt >= int(settings.CHAT_UPLOAD_MAX_ATTACHMENTS_PER_SESSION):
        raise ValueError("本会话附件数量超过上限")

    aid = uuid.uuid4().hex  # 生成附件ID
    kind = classify_kind(name)  # 获取文件类型
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(name).name)[:180] or "file"  # 生成安全文件名
    sub = obs.join_key(
        f"user_{user_id}", f"agent_{agent_id}", _safe_segment(session_id), f"{aid}_{safe_name}"
    )  # 生成存储相对路径（存入 DB；完整对象 key 由 attachment_object_key 拼前缀）
    mime = guess_mime_from_path(name)  # 获取文件类型
    obs.save_bytes(attachment_object_key(sub), raw, content_type=mime)  # 写入对象存储

    # 写入数据库
    db = SessionLocal()
    try:
        row = ChatAttachmentRow(
            id=aid,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            original_filename=name,
            stored_relpath=sub,
            mime=mime,
            size_bytes=size,
            kind=kind,
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    return {"id": aid, "filename": name, "kind": kind, "mime": mime, "size_bytes": size}


def format_attachment_hint(user_id: int, agent_id: int, session_id: str) -> str:
    """
    本会话附件元数据列表（仅事实，不含如何调用工具）。
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 本会话附件元数据列表
    """
    items = list_session_attachments(user_id, agent_id, session_id)
    if not items:
        return ""
    lines = ["本会话中用户已上传的附件（元数据）："]
    for it in items:
        lines.append(
            f"- attachment_id={it['id']} 文件名={it['filename']} 类型={it['kind']} 大小={it['size_bytes']} 字节"
        )
    return "\n".join(lines)


def build_storable_human_content(
    user_text: str,
    attachment_ids: list[str],
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    supports_vision: bool,
) -> str | list[dict[str, Any]]:
    """
    构造写入会话历史的 HumanMessage.content（含 image_ref；文档不内联正文）。
    :param user_text: 用户文本
    :param attachment_ids: 附件ID列表
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param supports_vision: 是否支持视觉
    :return: 写入会话历史的 HumanMessage.content
    """
    ids = [x.strip() for x in (attachment_ids or []) if x and str(x).strip()]  # 获取附件ID列表
    text = (user_text or "").strip() or "（请根据附件回答问题。）"  # 获取用户文本

    if not ids:  # 如果没有附件ID，则返回用户文本
        return text

    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]  # 存储块列表
    for aid in ids:  # 遍历附件ID列表
        row = get_attachment_row(aid, user_id=user_id, agent_id=agent_id, session_id=session_id)  # 获取附件行
        if not row:
            raise ValueError(f"附件不存在或无权访问：{aid}")
        if row.kind == "image":  # 如果附件类型为图片
            if not supports_vision:
                raise ValueError(
                    "当前智能体未启用「多模态视觉」或所用模型不支持图片：无法处理图片附件。"
                    "请在智能体中开启多模态视觉后重试，或仅上传文本文档/表格。"
                )  # 如果支持视觉，则创建图片块
            blocks.append(  # 添加图片块到块列表
                {
                    "type": "image_ref",  # 创建图片块
                    "attachment_id": aid,  # 附件ID
                    "mime": row.mime,  # 文件类型
                    "filename": row.original_filename,  # 文件名
                }
            )
        else:
            blocks.append(  # 添加文件块到块列表
                {
                    "type": "file_ref",  # 创建文件块
                    "attachment_id": aid,  # 附件ID
                    "filename": row.original_filename,  # 文件名
                    "kind": row.kind,  # 文件类型
                    "mime": row.mime,  # 文件类型
                }
            )

    if len(blocks) > 1:
        return blocks
    return blocks


@dataclass(frozen=True)
class AttachmentPlaintextExtract:
    """附件全文抽取结果（供 read_attachment_text 与 BM25 检索共用）。"""

    text: str
    page_starts: tuple[int, ...]
    """PDF 时每页在 text 中的起始字符下标（第 i 页对应 page_starts[i]）；非 PDF 为空元组。"""


def extract_attachment_plaintext(
    attachment_id: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
) -> tuple[AttachmentPlaintextExtract | None, str | None]:
    """
    抽取附件完整纯文本（不截断）。成功返回 (extract, None)，失败返回 (None, 错误说明)。
    """
    row = get_attachment_row(attachment_id, user_id=user_id, agent_id=agent_id, session_id=session_id)
    if not row:
        return None, "错误：附件不存在或无权访问。"

    kind = row.kind
    suf = Path(row.original_filename).suffix.lower()
    key = attachment_object_key(row.stored_relpath)

    if kind == "image":
        return None, (
            "该附件为图片；若已启用多模态视觉，模型可直接理解。否则无法以文本工具读取图片内容。"
        )

    try:
        if suf in (".txt", ".md"):
            raw = obs.read_bytes(key)
            text = raw.decode("utf-8", errors="replace")
            return AttachmentPlaintextExtract(text=text, page_starts=()), None
    except obs.ObjectNotFoundError:
        return None, "错误：附件文件已丢失。"
    except Exception as e:
        return None, f"读取附件失败：{e}"

    # PDF/DOCX/表格解析库要求本地路径：下载到临时文件后解析，退出自动清理
    try:
        with obs.download_temp(key, suffix=suf) as path:
            return _extract_plaintext_from_local(path, suf)
    except obs.ObjectNotFoundError:
        return None, "错误：附件文件已丢失。"
    except Exception as e:
        return None, f"读取附件失败：{e}"


def _extract_plaintext_from_local(
    path: str, suf: str
) -> tuple[AttachmentPlaintextExtract | None, str | None]:
    """从本地文件路径抽取纯文本（对象存储场景下 path 为临时文件）。"""
    if suf == ".pdf":
        from langchain_community.document_loaders import PyPDFLoader

        loader = PyPDFLoader(path)
        docs = loader.load()
        # 抽取 pdf 中每一页的文本内容
        parts = [(d.page_content or "").strip() for d in docs]
        # 计算每一页的文本内容在 text 中的起始字符下标
        page_starts: list[int] = []
        acc = 0  # 累加每一页的文本内容的长度，用于计算每一页的文本内容在 text 中的起始字符下标
        for i, p in enumerate(parts):
            page_starts.append(acc) # 添加每一页的文本内容在 text 中的起始字符下标
            acc += len(p) # 累加每一页的文本内容的长度
            if i + 1 < len(parts): # 如果还有下一页，则添加换行符
                acc += 2
        text = "\n\n".join(parts) # 将每一页的文本内容拼接起来
        text = (text or "").strip()
        return AttachmentPlaintextExtract(text=text, page_starts=tuple(page_starts)), None # 返回附件全文抽取结果

    if suf == ".docx":
        from langchain_community.document_loaders import Docx2txtLoader

        loader = Docx2txtLoader(path)
        docs = loader.load()
        text = "\n\n".join((d.page_content or "") for d in docs)
        text = (text or "").strip()
        return AttachmentPlaintextExtract(text=text, page_starts=()), None

    if suf == ".csv":
        import pandas as pd

        df = pd.read_csv(path, nrows=2000)
        text = df.to_string()
        return AttachmentPlaintextExtract(text=text, page_starts=()), None

    if suf in (".xlsx", ".xls"):
        import pandas as pd

        df = pd.read_excel(path, nrows=2000, header=None)
        text = df.to_string()
        return AttachmentPlaintextExtract(text=text, page_starts=()), None

    return None, "不支持的附件类型。"


def read_attachment_text(
    attachment_id: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    max_chars: int = 12000,
) -> str:
    """
    供工具读取：抽取文本（pdf/docx/txt/md/csv/xlsx）。
    :param attachment_id: 附件ID
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :param max_chars: 最大字符数
    :return: 附件文本
    """
    ext, err = extract_attachment_plaintext(
        attachment_id, user_id=user_id, agent_id=agent_id, session_id=session_id
    )
    if err:
        return err
    assert ext is not None
    text = ext.text
    return text[:max_chars] + ("…\n（已截断）" if len(text) > max_chars else "")
