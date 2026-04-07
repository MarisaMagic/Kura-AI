"""聊天库 ORM（SQLAlchemy），与主应用 Tortoise 库分离。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.chat.database import Base


class ChatSession(Base):
    __tablename__ = "mg_chat_sessions"
    __table_args__ = (UniqueConstraint("user_id", "agent_id", "session_id", name="uq_mg_user_agent_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "mg_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_ref_id: Mapped[int] = mapped_column(
        ForeignKey("mg_chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    rag_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    session = relationship("ChatSession", back_populates="messages")
