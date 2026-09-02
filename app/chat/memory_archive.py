"""
将会话中超出窗口的整轮对话切块、嵌入并写入 Milvus（增量，按 turn 水位线）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.chat.memory_scope import memory_scope_for
from app.chat.memory_turns import group_turn_pairs, split_system_prefix
from app.chat.milvus_memory import get_chat_memory_milvus
from app.chat.message_codec import msg_content_to_str
from app.chat.storage import storage

_META_ARCHIVED_TURN_KEYS = "memory_archived_turn_keys"
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


def path_turn_keys_for_session(user_id: int, agent_id: int, session_id: str) -> list[int]:
    """当前路径各轮的稳定身份（turn_key = 轮首用户消息行 id）。无消息时返回空列表。"""
    pairs = storage.load_path_with_ids(user_id, agent_id, session_id)
    if not pairs:
        return []
    msgs = [m for _, m in pairs]
    _, body = split_system_prefix(msgs)
    body_pairs = pairs[len(pairs) - len(body):] if body else []
    return [t[0][0] for t in group_turn_pairs(body_pairs) if t]


def archived_turn_keys_on_path(
    user_id: int, agent_id: int, session_id: str, path_turn_keys: list[int] | None = None
) -> list[int]:
    """已归档且落在当前路径上的 turn_key 列表（会话记忆检索的允许集合）。"""
    if path_turn_keys is None:
        path_turn_keys = path_turn_keys_for_session(user_id, agent_id, session_id)
    if not path_turn_keys:
        return []
    meta = storage.get_session_metadata(user_id, agent_id, session_id)
    archived = {int(k) for k in (meta.get(_META_ARCHIVED_TURN_KEYS) or [])}
    return [k for k in path_turn_keys if k in archived]


def archive_session_memory(user_id: int, agent_id: int, session_id: str) -> None:
    """
    将当前路径上已离开原文窗口且未归档的轮次增量写入 Milvus（按 turn_key 幂等去重）。
    原文窗口由会话压缩 metadata（compact_states）决定，摘要本身不入库。
    分叉后各分支共享前缀只归档一次；非当前路径的分支轮次不处理，切回后继续聊时自然归档。
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return
    if not (settings.EMBEDDING_API_KEY or "").strip():
        logger.debug("archive_session_memory: skip (no EMBEDDING_API_KEY)")
        return

    pairs = storage.load_path_with_ids(user_id, agent_id, session_id)  # 加载当前路径消息（带行 id）
    if not pairs:
        return

    msgs = [m for _, m in pairs]
    _, body = split_system_prefix(msgs)  # 将消息体按系统消息分组, 返回系统消息列表和非系统消息列表。
    body_pairs = pairs[len(pairs) - len(body):] if body else []
    turn_pairs_list = group_turn_pairs(body_pairs)  # 按用户轮次分组，轮首为用户消息行 id
    if not turn_pairs_list:
        return
    path_turn_keys = [t[0][0] for t in turn_pairs_list]

    from app.chat.compact import verbatim_keep_from_for_session

    keep_from = verbatim_keep_from_for_session(
        user_id, agent_id, session_id, path_turn_keys=path_turn_keys
    )
    if keep_from <= 0:
        return

    meta = storage.get_session_metadata(user_id, agent_id, session_id)
    archived = {int(k) for k in (meta.get(_META_ARCHIVED_TURN_KEYS) or [])}
    # 仅归档已离开原文窗口、且未按 turn_key 归档过的轮次
    pending = [
        (idx, tp) for idx, tp in enumerate(turn_pairs_list[:keep_from]) if tp[0][0] not in archived
    ]
    if not pending:
        return

    mem_scope = memory_scope_for(user_id, agent_id, session_id)  # 获取会话记忆隔离键
    digest = _short_session_digest(user_id, agent_id, session_id)  # 生成会话记忆的隔离键哈希值
    cap = _milvus_text_cap()  # 获取会话记忆的文本最大长度
    max_chunk = max(256, min(int(getattr(settings, "CHAT_MEMORY_CHUNK_MAX_CHARS", 1400) or 1400), cap))

    milvus = get_chat_memory_milvus()  # 获取会话记忆集合
    milvus.init_collection()
    embedder = get_multimodal_embedding_service()  # 与知识库一致：DashScope MultiModalEmbedding

    try:
        insert_rows: list[dict[str, Any]] = []  # 构建插入数据
        new_keys: set[int] = set()
        # 遍历待归档的轮次, 将轮次转换为文本, 并分块插入到 Milvus 中
        for turn_idx, turn_pairs in pending:
            turn_key = turn_pairs[0][0]
            turn = [m for _, m in turn_pairs]
            full_text = turn_to_text(turn)  # 将轮次转换为文本
            sub_chunks = _chunk_text(full_text, max_chunk)  # 将文本分块
            if not sub_chunks:
                continue
            new_keys.add(turn_key)
            for ci, chunk_text in enumerate(sub_chunks):
                chunk_id = f"mem_{digest}_k{turn_key}_c{ci}"  # 分块 ID 含 turn_key，跨分支不撞车
                insert_rows.append(
                    {
                        "memory_scope": mem_scope,
                        "text": chunk_text[:cap],
                        "turn_index": turn_idx,
                        "turn_key": turn_key,
                        "chunk_index": ci,
                        "chunk_id": chunk_id,
                    }
                )

        if not insert_rows:
            return

        texts = [r["text"] for r in insert_rows]
        dense_list = embedder.get_text_embeddings(texts)
        for r, d_emb in zip(insert_rows, dense_list):
            r["dense_embedding"] = d_emb

        milvus.insert(insert_rows)  # 插入数据到 Milvus
        storage.patch_session_metadata(
            user_id,
            agent_id,
            session_id,
            {_META_ARCHIVED_TURN_KEYS: sorted(archived | new_keys)},
        )
    except Exception:
        logger.exception("archive_session_memory failed")


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
