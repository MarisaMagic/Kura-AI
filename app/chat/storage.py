"""对话持久化：PostgreSQL + Redis（按 user_id + agent_id + session_id）。"""

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
        return f"chat_messages:{user_id}:{agent_id}:{session_id}"

    @staticmethod
    def _sessions_cache_key(user_id: int, agent_id: int) -> str:
        return f"chat_sessions:{user_id}:{agent_id}"

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
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
        db = SessionLocal()
        try:
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
                session = ChatSessionRow(
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    metadata_json=metadata or {},
                )
                db.add(session)
                db.flush()
            else:
                session.metadata_json = metadata or {}

            db.query(ChatMessageRow).filter(ChatMessageRow.session_ref_id == session.id).delete(
                synchronize_session=False
            )

            serialized = []
            now = datetime.utcnow()
            for idx, msg in enumerate(messages):
                rag_trace = None
                if extra_message_data and idx < len(extra_message_data):
                    extra = extra_message_data[idx] or {}
                    rag_trace = extra.get("rag_trace")

                db.add(
                    ChatMessageRow(
                        session_ref_id=session.id,
                        message_type=msg.type,
                        content=str(msg.content),
                        timestamp=now,
                        rag_trace=rag_trace,
                    )
                )
                serialized.append(
                    {
                        "type": msg.type,
                        "content": str(msg.content),
                        "timestamp": now.isoformat(),
                        "rag_trace": rag_trace,
                    }
                )

            session.updated_at = now
            db.commit()

            cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), serialized)
            cache.delete(self._sessions_cache_key(user_id, agent_id))
        finally:
            db.close()

    def load(self, user_id: int, agent_id: int, session_id: str) -> list:
        cached = cache.get_json(self._messages_cache_key(user_id, agent_id, session_id))
        if cached is not None:
            return self._to_langchain_messages(cached)

        records = self.get_session_messages(user_id, agent_id, session_id)
        cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), records)
        return self._to_langchain_messages(records)

    def list_session_infos(self, user_id: int, agent_id: int) -> list[dict]:
        cached = cache.get_json(self._sessions_cache_key(user_id, agent_id))
        if cached is not None:
            return cached

        db = SessionLocal()
        try:
            sessions = (
                db.query(ChatSessionRow)
                .filter(ChatSessionRow.user_id == user_id, ChatSessionRow.agent_id == agent_id)
                .order_by(ChatSessionRow.updated_at.desc())
                .all()
            )
            result = []
            for s in sessions:
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
                if last_human and (last_human.content or "").strip():
                    preview = (last_human.content or "").strip().replace("\n", " ")
                    if len(preview) > 120:
                        preview = preview[:120] + "…"
                result.append(
                    {
                        "session_id": s.session_id,
                        "updated_at": s.updated_at.isoformat(),
                        "message_count": count,
                        "last_user_preview": preview,
                    }
                )
            cache.set_json(self._sessions_cache_key(user_id, agent_id), result)
            return result
        finally:
            db.close()

    def get_session_messages(self, user_id: int, agent_id: int, session_id: str) -> list[dict]:
        cached = cache.get_json(self._messages_cache_key(user_id, agent_id, session_id))
        if cached is not None:
            return cached

        db = SessionLocal()
        try:
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
                return []

            rows = (
                db.query(ChatMessageRow)
                .filter(ChatMessageRow.session_ref_id == session.id)
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
            result = [
                {
                    "type": row.message_type,
                    "content": row.content,
                    "timestamp": row.timestamp.isoformat(),
                    "rag_trace": row.rag_trace,
                }
                for row in rows
            ]
            cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), result)
            return result
        finally:
            db.close()

    def delete_session(self, user_id: int, agent_id: int, session_id: str) -> bool:
        db = SessionLocal()
        try:
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
                return False

            db.delete(session)
            db.commit()
            cache.delete(self._messages_cache_key(user_id, agent_id, session_id))
            cache.delete(self._sessions_cache_key(user_id, agent_id))
            return True
        finally:
            db.close()


storage = ConversationStorage()
