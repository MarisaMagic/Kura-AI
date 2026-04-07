"""
聊天库 ORM（SQLAlchemy），与主应用 Tortoise 库分离。
支持多用户、多智能体的聊天场景，每个对话会话可以包含多条消息
级联删除: 当会话删除时，关联的消息也会自动删除
索引优化: 关键字段都添加了索引，提高查询性能
JSON存储: 使用 JSON 类型存储灵活的元数据和追踪信息
时间戳: 记录创建和更新时间，便于追踪
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.chat.database import Base


class ChatSession(Base):
    """
    聊天会话模型
    id: 主键
    user_id: 用户ID (必填, 有索引)
    agent_id: 智能体ID (必填, 有索引)
    session_id: 会话ID (必填, 有索引)
    metadata_json: 元数据
    updated_at: 更新时间
    created_at: 创建时间
    确保同一用户同一智能体同一会话只存在一个会话
    与 ChatMessage 是一对多关系, 级联删除(当会话删除时，关联的消息也会自动删除)
    """
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
    """
    聊天消息模型
    id: 主键
    session_ref_id: 会话ID (外键, 关联到 mg_chat_sessions.id, 级联删除)
    message_type: 消息类型
    content: 消息内容 (文本)
    timestamp: 消息时间 (默认值为当前时间)
    rag_trace: 检索增强生成跟踪 (可选)
    通过 session 属性回关到 ChatSession
    """
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
