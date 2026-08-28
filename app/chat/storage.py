"""
对话持久化: PostgreSQL + Redis (按 user_id + agent_id + session_id)
"""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import desc

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import ChatMessage as ChatMessageRow
from app.chat.db_models import ChatSession as ChatSessionRow
from app.chat.message_codec import envelope_to_langchain_message, msg_content_to_str, serialize_message_envelope
from app.chat.preview_session import (
    EDITOR_PREVIEW_SESSION_PREFIX,
    is_editor_preview_session,
)


class ConversationStorage:
    def _build_session_info_dict(self, db, s: ChatSessionRow) -> dict:
        """
        由会话 ORM 行生成列表项（预览与计数），供全量列表与分页列表复用。
        """
        count = db.query(ChatMessageRow).filter(ChatMessageRow.session_ref_id == s.id).count()
        last_human = (
            db.query(ChatMessageRow)
            .filter(
                ChatMessageRow.session_ref_id == s.id,
                ChatMessageRow.message_type == "human",
            )
            .order_by(desc(ChatMessageRow.id))
            .first()
        )
        preview = ""
        if last_human:
            if last_human.content_json and isinstance(last_human.content_json, dict):
                env = last_human.content_json
                lc = env.get("lc")
                preview = msg_content_to_str(lc).strip()
            elif (last_human.content or "").strip():
                preview = (last_human.content or "").strip()
            preview = preview.replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "…"
        return {
            "session_id": s.session_id,
            "agent_id": s.agent_id,
            "updated_at": s.updated_at.isoformat(),
            "message_count": count,
            "last_user_preview": preview,
        }

    @staticmethod
    def _messages_cache_key(user_id: int, agent_id: int, session_id: str) -> str:
        """
        生成消息缓存的 Redis 键
        """
        return f"chat_messages:{user_id}:{agent_id}:{session_id}"

    @staticmethod
    def _sessions_cache_key(user_id: int, agent_id: int) -> str:
        """
        生成会话缓存的 Redis 键
        """
        return f"chat_sessions:{user_id}:{agent_id}"

    @staticmethod
    def _sessions_all_cache_key(user_id: int) -> str:
        """
        当前用户跨全部智能体的会话列表（有序，与侧栏「最近对话」分页一致）
        """
        return f"chat_sessions_all:{user_id}"

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
        """
        将数据库记录转换为 LangChain 消息列表
        (HumanMessage, AIMessage, SystemMessage)
        """
        messages = []
        for msg_data in records:
            msg_type = msg_data.get("type")
            cj = msg_data.get("content_json")
            if cj and isinstance(cj, dict) and cj.get("v"):
                messages.append(envelope_to_langchain_message(cj))
                continue
            content = msg_data.get("content", "")
            if msg_type == "human":
                messages.append(HumanMessage(content=content))
            elif msg_type == "ai":
                messages.append(AIMessage(content=content))
            elif msg_type == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def save(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        messages: list,
        metadata: dict | None = None,
        extra_message_data: list | None = None,
    ) -> None:
        """
        保存对话会话和消息到 PostgreSQL 数据库并更新 Redis 缓存
        用于用户在对话过程中, 保存最新一轮对话消息到数据库并更新 Redis 缓存
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :param messages: 消息列表
        :param metadata: 元数据
        :param extra_message_data: 额外消息数据
        """
        # 1. 创建 PostgreSQL 数据库连接
        db = SessionLocal()
        try:
            # 2. 查询会话是否存在
            session = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ChatSessionRow.session_id == session_id,
                )
                .first()
            )
            if not session:
                # 如果会话不存在，创建新的 ChatSessionRow 对象
                # 并设置 metadata (元数据, 默认为空字典)
                # 添加到数据库, 并通过 flush() 立即获取 id (插入后立即获取主键)
                session = ChatSessionRow(
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    metadata_json=metadata or {},
                )
                db.add(session)
                db.flush()
            else:
                # 如果会话存在，更新 metadata
                session.metadata_json = metadata or {}

            # 3. 删除会话关联的旧消息
            db.query(ChatMessageRow).filter(ChatMessageRow.session_ref_id == session.id).delete(
                synchronize_session=False
            )

            # 4. 将新消息添加到数据库并更新 Redis 缓存
            serialized = [] # 序列化后的消息列表
            now = datetime.utcnow() # 当前时间
            # 遍历新消息列表, 创建 ChatMessageRow 对象并添加新消息到数据库
            for idx, msg in enumerate(messages):
                rag_trace = None
                rag_steps = None
                error_text = None
                image_references = None
                sources = None
                thinking_text = None
                # 检查额外消息数据, 如果有 RAG 追踪信息, 则添加到消息中
                if extra_message_data and idx < len(extra_message_data):
                    extra = extra_message_data[idx] or {}
                    rag_trace = extra.get("rag_trace")
                    rag_steps = extra.get("rag_steps")
                    image_references = extra.get("image_references")  # 获取图片引用
                    sources = extra.get("sources")  # 获取知识库来源列表
                    thinking_text = extra.get("thinking_text")  # 获取工具调用前过渡文本
                    raw_err = extra.get("error_text")
                    if raw_err is not None:
                        error_text = str(raw_err).strip() or None

                envelope = serialize_message_envelope(msg)
                preview = msg_content_to_str(getattr(msg, "content", ""))
                if len(preview) > 65500:
                    preview = preview[:65500] + "…"

                # 创建 ChatMessageRow 对象并添加新消息到数据库
                db.add(
                    ChatMessageRow(
                        session_ref_id=session.id,
                        message_type=msg.type,
                        content=preview,
                        content_json=envelope,
                        timestamp=now,
                        rag_trace=rag_trace,
                        rag_steps=rag_steps,
                        error_text=error_text,
                        image_references=image_references,  # 添加图片引用
                        sources=sources,  # 添加知识库来源
                        thinking_text=thinking_text,  # 工具调用前过渡文本
                    )
                )
                # 将新消息添加到序列化列表
                serialized.append(
                    {
                        "type": msg.type,
                        "content": preview,
                        "content_json": envelope,
                        "timestamp": now.isoformat(),
                        "rag_trace": rag_trace,
                        "rag_steps": rag_steps,
                        "error_text": error_text,
                        "image_references": image_references,  # 添加图片引用
                        "sources": sources,  # 添加知识库来源
                        "thinking_text": thinking_text,  # 工具调用前过渡文本
                    }
                )

            # 5. 更新会话的更新时间
            session.updated_at = now
            # 6. 提交事务到数据库
            db.commit()

            # 7. 更新 Redis 缓存 (使用 _messages_cache_key)
            cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), serialized)
            # 8. 删除会话缓存 (使用 _sessions_cache_key)
            cache.delete(self._sessions_cache_key(user_id, agent_id))
            cache.delete(self._sessions_all_cache_key(user_id))
        finally:
            db.close()

    def load(self, user_id: int, agent_id: int, session_id: str) -> list:
        """
        从缓存或数据库加载会话消息
        用于用户打开历史会话时，加载之前的会话消息到对话框中显示
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :return: 消息列表
        """
        cached = cache.get_json(self._messages_cache_key(user_id, agent_id, session_id))
        # 1. 检查 Redis 缓存, 如果存在则直接返回, 转换为 LangChain 消息列表
        if cached is not None:
            return self._to_langchain_messages(cached)

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话消息
        records = self.get_session_messages(user_id, agent_id, session_id)
        # 将消息列表添加到 Redis 缓存
        cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), records)
        # 将消息列表转换为 LangChain 消息列表并返回
        return self._to_langchain_messages(records)

    def list_session_infos(self, user_id: int, agent_id: int) -> list[dict]:
        """
        获取用户与特定 agent 的所有会话信息列表
        用于用户打开智能体弹窗时，显示对话历史列表
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :return: 会话列表
        """
        cached = cache.get_json(self._sessions_cache_key(user_id, agent_id))
        # 1. 检查 Redis 缓存, 如果存在则直接返回（剔除编辑器试聊会话）
        if cached is not None:
            return [x for x in cached if not is_editor_preview_session(x.get("session_id"))]

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话信息列表
        db = SessionLocal() # 创建 PostgreSQL 数据库连接
        try:
            # 查询符合 user_id 和 agent_id 的所有会话, 并按更新时间降序排序
            sessions = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ~ChatSessionRow.session_id.like(EDITOR_PREVIEW_SESSION_PREFIX + "%"),
                )
                .order_by(desc(ChatSessionRow.updated_at), desc(ChatSessionRow.id))
                .all()
            )
            result = [self._build_session_info_dict(db, s) for s in sessions]
            # 3. 更新 Redis 缓存 (使用 _sessions_cache_key)
            # 将会话信息列表添加到 Redis 缓存
            cache.set_json(self._sessions_cache_key(user_id, agent_id), result)
            # 返回会话信息列表
            return result
        finally:
            db.close()

    def list_session_infos_paginated(
        self, user_id: int, agent_id: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        """
        分页查询会话列表（直接查库，不走全量 Redis 缓存）。
        按 updated_at 降序，同时间以 id 降序。
        不返回编辑器试聊会话（__editor_preview_ 前缀）。
        """
        db = SessionLocal()
        try:
            q = db.query(ChatSessionRow).filter(
                ChatSessionRow.user_id == user_id,
                ChatSessionRow.agent_id == agent_id,
                ~ChatSessionRow.session_id.like(EDITOR_PREVIEW_SESSION_PREFIX + "%"),
            )
            total = q.count()
            rows = (
                q.order_by(desc(ChatSessionRow.updated_at), desc(ChatSessionRow.id))
                .offset(offset)
                .limit(limit)
                .all()
            )
            items = [self._build_session_info_dict(db, s) for s in rows]
            return items, total
        finally:
            db.close()

    def list_session_infos_all_paginated(
        self, user_id: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        """
        分页查询当前用户全部智能体下的会话（不按 agent 过滤），按 updated_at 降序。
        优先读 Redis 全量列表缓存，未命中则查库并回填。
        用于用户打开侧栏「最近对话」时，显示全部智能体下的会话列表
        :param user_id: 用户 ID
        :param limit: 分页条数
        :param offset: 分页偏移
        :return: 会话列表
        """
        # 1. 检查 Redis 全量列表缓存, 如果存在则直接返回（剔除编辑器试聊会话）
        key = self._sessions_all_cache_key(user_id)
        cached = cache.get_json(key)
        if cached is not None:
            filtered = [x for x in cached if not is_editor_preview_session(x.get("session_id"))]
            # 返回分页后的会话列表
            total = len(filtered)
            return filtered[offset : offset + limit], total

        # 2. 如果 Redis 全量列表缓存不存在, 则从数据库加载会话列表
        db = SessionLocal()
        try:
            q = db.query(ChatSessionRow).filter(
                ChatSessionRow.user_id == user_id,
                ~ChatSessionRow.session_id.like(EDITOR_PREVIEW_SESSION_PREFIX + "%"),
            )
            rows = (
                q.order_by(desc(ChatSessionRow.updated_at), desc(ChatSessionRow.id)).all()
            )
            items = [self._build_session_info_dict(db, s) for s in rows]
            # 3. 更新 Redis 全量列表缓存 (使用 _sessions_all_cache_key)
            # 将会话列表添加到 Redis 全量列表缓存
            cache.set_json(key, items)
            total = len(items)
            # 返回分页后的会话列表
            return items[offset : offset + limit], total
        finally:
            db.close()

    def get_session_messages(self, user_id: int, agent_id: int, session_id: str) -> list[dict]:
        """
        从数据库获取指定会话的所有消息记录
        用于在加载会话消息时, 如果 Redis 缓存不存在, 则从数据库加载会话消息
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :return: 消息列表
        """
        # 1. 检查 Redis 缓存, 如果存在则直接返回
        cached = cache.get_json(self._messages_cache_key(user_id, agent_id, session_id))
        if cached is not None:
            return cached 

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话消息
        db = SessionLocal() # 创建 PostgreSQL 数据库连接
        try:
            # 查询符合 user_id 和 agent_id 和 session_id 的会话
            session = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ChatSessionRow.session_id == session_id,
                )
                .first()
            )
            # 如果会话不存在, 则返回空列表
            if not session: 
                return []

            # 查询符合 user_id 和 agent_id 和 session_id 的会话的消息记录, 并按 id 升序排序
            rows = (
                db.query(ChatMessageRow)
                .filter(ChatMessageRow.session_ref_id == session.id)
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
            # 将消息记录转换为字典列表
            result = []
            for row in rows:
                env = row.content_json if isinstance(row.content_json, dict) else None
                item = {
                    "type": row.message_type,
                    "content": row.content,
                    "timestamp": row.timestamp.isoformat(),
                    "rag_trace": row.rag_trace,
                    "rag_steps": row.rag_steps,
                    "error_text": row.error_text,
                    "image_references": row.image_references,
                    "sources": row.sources,
                    "thinking_text": row.thinking_text,
                }
                if env:
                    item["content_json"] = env
                result.append(item)
            # 3. 更新 Redis 缓存 (使用 _messages_cache_key)
            # 将消息列表添加到 Redis 缓存
            cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), result)
            # 返回消息列表
            return result
        finally:
            db.close()

    def delete_session(self, user_id: int, agent_id: int, session_id: str) -> bool:
        """
        删除指定会话
        用于用户删除历史会话时，删除数据库中的会话记录并更新 Redis 缓存
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :return: 是否删除成功
        """
        db = SessionLocal()
        try:
            # 查询符合 user_id 和 agent_id 和 session_id 的会话
            session = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ChatSessionRow.session_id == session_id,
                )
                .first()
            )
            # 如果会话不存在, 则返回 False
            if not session:
                return False

            # 删除 Milvus 中本会话的记忆向量（PG 会话行删除后无法再查 session_id）
            try:
                from app.chat.memory_archive import purge_session_memory_vectors

                purge_session_memory_vectors(user_id, agent_id, session_id)
            except Exception:
                pass

            # 删除会话（级联删除消息与 mg_chat_memory_cursor）
            db.delete(session)
            # 提交事务到数据库
            db.commit()
            # 删除 Redis 缓存 (使用 _messages_cache_key)
            cache.delete(self._messages_cache_key(user_id, agent_id, session_id))
            # 删除 Redis 缓存 (使用 _sessions_cache_key)
            cache.delete(self._sessions_cache_key(user_id, agent_id))
            cache.delete(self._sessions_all_cache_key(user_id))
            return True
        finally:
            db.close()


storage = ConversationStorage()
