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
from app.kb.embedding import EmbeddingService
from app.settings import settings


def _milvus_text_cap() -> int:
    return max(512, int(getattr(settings, "CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH", 8192) or 8192))

logger = logging.getLogger(__name__)


def _short_session_digest(user_id: int, agent_id: int, session_id: str) -> str:
    raw = f"{user_id}:{agent_id}:{session_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _format_tool_message(msg: ToolMessage) -> str:
    name = (getattr(msg, "name", None) or "") or ""
    body = msg_content_to_str(msg.content)
    if len(body) > 2000:
        body = body[:2000] + "…"
    return f"工具({name}): {body}" if name else f"工具: {body}"


def _format_ai_message(msg: AIMessage) -> str:
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


def _turn_to_text(turn: list[BaseMessage]) -> str:
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
    row = db.query(ChatMemoryCursor).filter(ChatMemoryCursor.session_ref_id == session_ref_id).first()
    if row:
        return row
    row = ChatMemoryCursor(session_ref_id=session_ref_id, last_archived_turn_index=-1)
    db.add(row)
    db.flush()
    return row


def archive_session_memory(user_id: int, agent_id: int, session_id: str) -> None:
    """
    将超出窗口的轮次增量写入 Milvus，并更新 mg_chat_memory_cursor。
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return
    if not (settings.EMBEDDING_API_KEY or "").strip():
        logger.debug("archive_session_memory: skip (no EMBEDDING_API_KEY)")
        return

    messages = storage.load(user_id, agent_id, session_id)
    if not messages:
        return

    _, body = split_system_prefix(messages)
    turns = group_turns(body)
    window = max(1, int(getattr(settings, "CHAT_MEMORY_WINDOW_TURNS", 10) or 10))
    if len(turns) <= window:
        return

    max_archivable = len(turns) - window - 1
    if max_archivable < 0:
        return

    mem_scope = memory_scope_for(user_id, agent_id, session_id)
    digest = _short_session_digest(user_id, agent_id, session_id)
    cap = _milvus_text_cap()
    max_chunk = max(256, min(int(getattr(settings, "CHAT_MEMORY_CHUNK_MAX_CHARS", 1400) or 1400), cap))

    milvus = get_chat_memory_milvus()
    milvus.init_collection()
    embedder = EmbeddingService()

    db = SessionLocal()
    try:
        sess = (
            db.query(ChatSessionRow)
            .filter(
                ChatSessionRow.user_id == user_id,
                ChatSessionRow.agent_id == agent_id,
                ChatSessionRow.session_id == session_id,
            )
            .first()
        )
        if not sess:
            return

        cursor = _get_or_create_cursor(db, sess.id)
        start = cursor.last_archived_turn_index + 1
        if start > max_archivable:
            db.commit()
            return

        insert_rows: list[dict[str, Any]] = []
        for turn_idx in range(start, max_archivable + 1):
            turn = turns[turn_idx]
            full_text = _turn_to_text(turn)
            sub_chunks = _chunk_text(full_text, max_chunk)
            if not sub_chunks:
                continue
            for ci, chunk_text in enumerate(sub_chunks):
                chunk_id = f"mem_{digest}_t{turn_idx}_c{ci}"
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
        dense_list, sparse_list = embedder.get_all_embeddings(texts)
        for r, d_emb, s_emb in zip(insert_rows, dense_list, sparse_list):
            r["dense_embedding"] = d_emb
            r["sparse_embedding"] = s_emb

        milvus.insert(insert_rows)
        cursor.last_archived_turn_index = max_archivable
        db.commit()
    except Exception:
        logger.exception("archive_session_memory failed")
        db.rollback()
    finally:
        db.close()


def schedule_archive_session_memory(user_id: int, agent_id: int, session_id: str) -> None:
    """对话落库后调用：按配置同步或后台线程归档。"""
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return

    def _run() -> None:
        try:
            archive_session_memory(user_id, agent_id, session_id)
        except Exception:
            logger.exception("schedule_archive_session_memory")

    if getattr(settings, "CHAT_MEMORY_ARCHIVE_ASYNC", True):
        import threading

        threading.Thread(target=_run, name="chat-memory-archive", daemon=True).start()
    else:
        _run()


def purge_session_memory_vectors(user_id: int, agent_id: int, session_id: str) -> None:
    """删除会话在 Milvus 中的全部记忆向量（删会话前调用）。"""
    try:
        mem_scope = memory_scope_for(user_id, agent_id, session_id)
        mgr = get_chat_memory_milvus()
        if mgr.collection_exists():
            mgr.delete_by_scope(mem_scope)
    except Exception:
        logger.exception("purge_session_memory_vectors")
