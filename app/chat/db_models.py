"""
聊天库 ORM（SQLAlchemy），与主应用 Tortoise 库分离。
支持多用户、多智能体的聊天场景，每个对话会话可以包含多条消息
级联删除: 当会话删除时，关联的消息也会自动删除
索引优化: 关键字段都添加了索引，提高查询性能
JSON存储: 使用 JSON 类型存储灵活的元数据和追踪信息
时间戳: 记录创建和更新时间，便于追踪

智能体知识库，用于存储智能体知识库的元数据和分块信息。
支持多用户、多智能体的知识库，每个知识库可以包含多个文件，每个文件可以包含多个分块。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.chat.database import Base


class KbDocument(Base):
    """
    智能体知识库：已上传文件元数据（展示名为原始上传文件名）。
    :param id: 主键
    :param kb_scope: 知识库范围
    :param display_filename: 展示文件名
    :param stored_filename: 存储文件名
    :param file_type: 文件类型
    :param chunk_count: 分块数量
    :param created_at: 创建时间
    :param updated_at: 更新时间
    """

    __tablename__ = "mg_kb_documents"
    __table_args__ = (UniqueConstraint("kb_scope", "display_filename", name="uq_mg_kb_scope_display_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kb_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class KbParentChunk(Base):
    """
    知识库父级分块（L1/L2），用于 Auto-merge。
    :param chunk_id: 分块ID
    :param kb_scope: 知识库范围
    :param parent_chunk_id: 父级分块ID
    :param root_chunk_id: 根级分块ID
    :param chunk_level: 分块层级
    :param chunk_idx: 分块索引
    :param updated_at: 更新时间
    """

    __tablename__ = "mg_kb_parent_chunks"

    chunk_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    kb_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_chunk_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    root_chunk_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    chunk_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


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
    rag_steps: 流式检索步骤列表 (可选，用于历史回放)
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
    rag_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)

    session = relationship("ChatSession", back_populates="messages")
