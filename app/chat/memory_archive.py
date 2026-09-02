"""
将会话中超出窗口的整轮对话切块、嵌入并写入 Milvus（增量，按 turn 水位线）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.chat.database import SessionLocal
from app.chat.db_models import ChatMemoryCursor, ChatSession as ChatSessionRow
from app.chat.memory_scope import memory_scope_for
from app.chat.memory_turns import group_turns, split_system_prefix
from app.chat.milvus_memory import get_chat_memory_milvus
from app.chat.message_codec import msg_content_to_str
from app.chat.storage import storage
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.settings import settings


def _milvus_text_cap() -> int:
    return max(512, int(getattr(settings, "CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH", 8192) or 8192))

logger = logging.getLogger(__name__)


def _short_session_digest(user_id: int, agent_id: int, session_id: str) -> str:
    """
    生成会话记忆的隔离键, 只能检索当前会话的记忆
    将用户ID、智能体ID和会话ID拼接成字符串, 并使用 SHA-256 哈希函数生成一个 24 字符的哈希值。
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: 隔离键
    """
    raw = f"{user_id}:{agent_id}:{session_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _format_tool_message(msg: ToolMessage) -> str:
    """
    格式化工具消息
    :param msg: 消息
    :return: 格式化后的消息
    """
    name = (getattr(msg, "name", None) or "") or ""
    body = msg_content_to_str(msg.content)
    if len(body) > 2000:
        body = body[:2000] + "…"
    return f"工具({name}): {body}" if name else f"工具: {body}"


def _format_ai_message(msg: AIMessage) -> str:
    """
    格式化助手消息
    :param msg: 消息
    :return: 格式化后的消息
    """
    parts: list[str] = []
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        parts.append(f"[tool_calls] {tool_calls}")
    body = msg_content_to_str(getattr(msg, "content", ""))
    if body:
        parts.append(body)
    text = "\n".join(parts)
    if len(text) > 12000:
        text = text[:12000] + "…"
    return f"助手: {text}"


def turn_to_text(turn: list[BaseMessage]) -> str:
    """
    将轮次转换为文本
    :param turn: 轮次
    :return: 文本
    """
    lines: list[str] = []
    for msg in turn:
        if isinstance(msg, HumanMessage):
            t = msg_content_to_str(msg.content)
            if len(t) > 8000:
                t = t[:8000] + "…"
            lines.append(f"用户: {t}")
        elif isinstance(msg, AIMessage):
            lines.append(_format_ai_message(msg))
        elif isinstance(msg, ToolMessage):
            lines.append(_format_tool_message(msg))
        else:
            t = msg_content_to_str(getattr(msg, "content", ""))
            lines.append(f"{msg.__class__.__name__}: {t}"[:4000])
    return "\n".join(lines).strip()


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """
    将文本分块
    :param text: 文本
    :param max_chars: 最大字符数
    :return: 分块后的文本列表
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + max_chars])
        i += max_chars
    return chunks


def _get_or_create_cursor(db: Any, session_ref_id: int) -> ChatMemoryCursor:
    """
    获取或创建会话记忆归档水位线
    :param db: 数据库
    :param session_ref_id: 会话ID
    :return: 会话记忆归档水位线
    """
    row = db.query(ChatMemoryCursor).filter(ChatMemoryCursor.session_ref_id == session_ref_id).first()
    if row:
        return row  # 如果已经存在则返回已有的水位线
    # 如果不存在则创建新的水位线，初始化水位线为 -1, 表示没有归档过任何轮次
    row = ChatMemoryCursor(session_ref_id=session_ref_id, last_archived_turn_index=-1)  
    db.add(row)
    db.flush()
    return row


def archive_session_memory(user_id: int, agent_id: int, session_id: str) -> None:
    """
    将已离开原文窗口的轮次增量写入 Milvus，并更新 mg_chat_memory_cursor。
    原文窗口由会话压缩 metadata（compact_until_turn_index）决定，摘要本身不入库。
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return
    if not (settings.EMBEDDING_API_KEY or "").strip():
        logger.debug("archive_session_memory: skip (no EMBEDDING_API_KEY)")
        return

    messages = storage.load(user_id, agent_id, session_id)  # 加载会话消息
    if not messages:
        return

    _, body = split_system_prefix(messages)  # 将消息体按系统消息分组, 返回系统消息列表和非系统消息列表。
    turns = group_turns(body)  # 将消息体按用户轮次分组（不含前缀 System）。
    if not turns:
        return

    from app.chat.compact import verbatim_keep_from_for_session

    keep_from = verbatim_keep_from_for_session(user_id, agent_id, session_id, len(turns))
    max_archivable = keep_from - 1  # 仅归档已离开原文窗口的轮次
    if max_archivable < 0:
        return

    mem_scope = memory_scope_for(user_id, agent_id, session_id)  # 获取会话记忆隔离键
    digest = _short_session_digest(user_id, agent_id, session_id)  # 生成会话记忆的隔离键哈希值
    cap = _milvus_text_cap()  # 获取会话记忆的文本最大长度
    max_chunk = max(256, min(int(getattr(settings, "CHAT_MEMORY_CHUNK_MAX_CHARS", 1400) or 1400), cap))

    milvus = get_chat_memory_milvus()  # 获取会话记忆集合
    milvus.init_collection()
    embedder = get_multimodal_embedding_service()  # 与知识库一致：DashScope MultiModalEmbedding

    db = SessionLocal()
    try:
        sess = (
            db.query(ChatSessionRow)  # 查询会话
            .filter(
                ChatSessionRow.user_id == user_id,
                ChatSessionRow.agent_id == agent_id,
                ChatSessionRow.session_id == session_id,
            )
            .first()
        )
        if not sess:
            return

        cursor = _get_or_create_cursor(db, sess.id)  # 获取或创建会话记忆归档水位线
        start = cursor.last_archived_turn_index + 1  # 获取下一个可归档的轮次索引
        if start > max_archivable:
            db.commit()
            return

        insert_rows: list[dict[str, Any]] = []  # 构建插入数据
        # 遍历可归档的轮次, 将轮次转换为文本, 并分块插入到 Milvus 中
        for turn_idx in range(start, max_archivable + 1):
            turn = turns[turn_idx]
            full_text = turn_to_text(turn) # 将轮次转换为文本
            sub_chunks = _chunk_text(full_text, max_chunk) # 将文本分块
            if not sub_chunks:
                continue
            for ci, chunk_text in enumerate(sub_chunks):
                chunk_id = f"mem_{digest}_t{turn_idx}_c{ci}" # 构建分块ID
                insert_rows.append(
                    {
                        "memory_scope": mem_scope,
                        "text": chunk_text[:cap],
                        "turn_index": turn_idx,
                        "chunk_index": ci,
                        "chunk_id": chunk_id,
                    }
                )

        if not insert_rows:
            db.commit()
            return

        texts = [r["text"] for r in insert_rows]
        dense_list, sparse_list = embedder.get_all_embeddings(texts) # 获取密集向量和稀疏向量
        for r, d_emb, s_emb in zip(insert_rows, dense_list, sparse_list):
            r["dense_embedding"] = d_emb
            r["sparse_embedding"] = s_emb

        milvus.insert(insert_rows) # 插入数据到 Milvus
        cursor.last_archived_turn_index = max_archivable # 更新会话记忆归档水位线
        db.commit() # 提交事务
    except Exception:
        logger.exception("archive_session_memory failed")
        db.rollback()
    finally:
        db.close()


def schedule_archive_session_memory(user_id: int, agent_id: int, session_id: str) -> None:
    """
    对话落库后调用：按配置同步或后台线程归档。
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: None
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return

    # 定义归档会话记忆的线程函数
    def _run() -> None:
        try:
            archive_session_memory(user_id, agent_id, session_id) # 归档会话记忆
        except Exception:
            logger.exception("schedule_archive_session_memory")

    if getattr(settings, "CHAT_MEMORY_ARCHIVE_ASYNC", True): # 如果配置为异步归档
        import threading # 导入线程模块

        threading.Thread(target=_run, name="chat-memory-archive", daemon=True).start() # 创建线程并启动
    else:
        _run() # 同步归档


def purge_session_memory_vectors(user_id: int, agent_id: int, session_id: str) -> None:
    """
    删除会话在 Milvus 中的全部记忆向量（删会话前调用）。
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param session_id: 会话ID
    :return: None
    """
    try:
        mem_scope = memory_scope_for(user_id, agent_id, session_id)
        mgr = get_chat_memory_milvus()
        if mgr.collection_exists():
            mgr.delete_by_scope(mem_scope)
    except Exception:
        logger.exception("purge_session_memory_vectors")
