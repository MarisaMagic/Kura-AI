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


class ConversationStorage:
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
    def _to_langchain_messages(records: list[dict]) -> list:
        """
        将数据库记录转换为 LangChain 消息列表
        (HumanMessage, AIMessage, SystemMessage)
        """
        messages = []
        for msg_data in records:
            msg_type = msg_data.get("type")
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
                # 检查额外消息数据, 如果有 RAG 追踪信息, 则添加到消息中
                if extra_message_data and idx < len(extra_message_data):
                    extra = extra_message_data[idx] or {}
                    rag_trace = extra.get("rag_trace")

                # 创建 ChatMessageRow 对象并添加新消息到数据库
                db.add(
                    ChatMessageRow(
                        session_ref_id=session.id,
                        message_type=msg.type,
                        content=str(msg.content),
                        timestamp=now,
                        rag_trace=rag_trace,
                    )
                )
                # 将新消息添加到序列化列表
                serialized.append(
                    {
                        "type": msg.type,
                        "content": str(msg.content),
                        "timestamp": now.isoformat(),
                        "rag_trace": rag_trace,
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
        finally:
            db.close()

    def load(self, user_id: int, agent_id: int, session_id: str) -> list:
        """
        从缓存或数据库加载会话消息
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
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :return: 会话列表
        """
        cached = cache.get_json(self._sessions_cache_key(user_id, agent_id))
        # 1. 检查 Redis 缓存, 如果存在则直接返回
        if cached is not None:
            return cached

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话信息列表
        db = SessionLocal() # 创建 PostgreSQL 数据库连接
        try:
            # 查询符合 user_id 和 agent_id 的所有会话, 并按更新时间降序排序
            sessions = (
                db.query(ChatSessionRow)
                .filter(ChatSessionRow.user_id == user_id, ChatSessionRow.agent_id == agent_id)
                .order_by(ChatSessionRow.updated_at.desc())
                .all()
            )
            result = [] # 会话信息列表
            # 遍历会话列表, 获取会话信息
            for s in sessions:
                count = db.query(ChatMessageRow).filter(ChatMessageRow.session_ref_id == s.id).count() # 获取会话消息数量
                # 获取最后一条人类消息
                last_human = (
                    db.query(ChatMessageRow)
                    .filter(
                        ChatMessageRow.session_ref_id == s.id,
                        ChatMessageRow.message_type == "human",
                    )
                    .order_by(desc(ChatMessageRow.id))
                    .first()
                )
                preview = "" # 最后一条人类消息的预览文本
                if last_human and (last_human.content or "").strip():
                    preview = (last_human.content or "").strip().replace("\n", " ")
                    if len(preview) > 120:
                        preview = preview[:120] + "…"
                # 将会话信息添加到会话信息列表 (session_id, updated_at, message_count, last_user_preview)
                result.append(
                    {
                        "session_id": s.session_id,
                        "updated_at": s.updated_at.isoformat(),
                        "message_count": count,
                        "last_user_preview": preview,
                    }
                )
            # 3. 更新 Redis 缓存 (使用 _sessions_cache_key)
            # 将会话信息列表添加到 Redis 缓存
            cache.set_json(self._sessions_cache_key(user_id, agent_id), result)
            # 返回会话信息列表
            return result
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
            result = [
                {
                    "type": row.message_type,
                    "content": row.content,
                    "timestamp": row.timestamp.isoformat(),
                    "rag_trace": row.rag_trace,
                }
                for row in rows
            ]
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

            # 删除会话
            db.delete(session)
            # 提交事务到数据库
            db.commit()
            # 删除 Redis 缓存 (使用 _messages_cache_key)
            cache.delete(self._messages_cache_key(user_id, agent_id, session_id))
            # 删除 Redis 缓存 (使用 _sessions_cache_key)
            cache.delete(self._sessions_cache_key(user_id, agent_id))
            return True
        finally:
            db.close()


storage = ConversationStorage()
