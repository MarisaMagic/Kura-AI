"""
PostgreSQL（聊天专用）SQLAlchemy 引擎与会话工厂。

SQLAlchemy 是一个强大的 SQL 工具包，提供了对象关系映射（ORM）、数据库连接池、事务管理等功能。
SQLAlchemy 的模型定义与数据库表结构分离，通过 ORM 映射，可以方便地进行数据库操作。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.settings import settings

os.environ.setdefault("PGCLIENTENCODING", "UTF8")


def _sqlalchemy_database_url(raw: str) -> str:
    """
    优先使用 psycopg3；兼容 .env 中的 postgresql+psycopg2://。
    """
    for old, new in (
        ("postgresql+psycopg2://", "postgresql+psycopg://"),
        ("postgres+psycopg2://", "postgresql+psycopg://"),
    ):
        if raw.startswith(old):
            return new + raw[len(old) :]
    return raw


def _make_engine():
    """
    创建SQLAlchemy引擎, 用于连接聊天 postgres 数据库
    """
    url = _sqlalchemy_database_url(settings.chat_database_url)
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

# 创建SQLAlchemy基类, 用于创建聊天数据库模型
Base = declarative_base()  


def init_chat_db() -> None:
    """
    让 ORM 模型挂到 Base.metadata 上
    在数据库里创建缺失的表（聊天库表结构）
    """
    import app.chat.db_models  # noqa: F401

    from sqlalchemy import text

    Base.metadata.create_all(bind=engine)
    try:
        from app.chat.milvus_memory import init_chat_memory_collection

        init_chat_memory_collection()
    except Exception:
        import logging

        logging.getLogger(__name__).warning("init_chat_memory_collection skipped", exc_info=True)
    # create_all 不会为已有表添加新列，需显式迁移
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS rag_steps JSON")
        )
        conn.execute(
            text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS content_json JSON")
        )
        conn.execute(text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS error_text TEXT"))
        conn.execute(text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS sources JSON"))
        conn.execute(text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS thinking_text TEXT"))
        conn.execute(text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS thinking_items JSON"))
        conn.execute(
            text("ALTER TABLE mg_kb_documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)")
        )
        # 消息树：parent_id / selected_child_id（存量线性会话回填为单链树）
        conn.execute(
            text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS parent_id INTEGER")
        )
        conn.execute(
            text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS selected_child_id INTEGER")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_mg_chat_messages_parent_id "
                "ON mg_chat_messages (parent_id)"
            )
        )
        # 回填 parent_id：仅处理仍为空且存在前驱行的存量线性数据；
        # 树模式下非根消息插入时必然已带 parent_id，不会被误改
        conn.execute(
            text(
                """
                WITH ordered AS (
                    SELECT id,
                           LAG(id) OVER (PARTITION BY session_ref_id ORDER BY id) AS prev_id
                    FROM mg_chat_messages
                )
                UPDATE mg_chat_messages m
                SET parent_id = o.prev_id
                FROM ordered o
                WHERE m.id = o.id
                  AND m.parent_id IS NULL
                  AND o.prev_id IS NOT NULL
                """
            )
        )
        # 回填 selected_child_id：仅处理仍为空且确有子行的存量数据（线性时每行至多一个子行）
        conn.execute(
            text(
                """
                UPDATE mg_chat_messages p
                SET selected_child_id = (
                    SELECT MAX(c.id) FROM mg_chat_messages c WHERE c.parent_id = p.id
                )
                WHERE p.selected_child_id IS NULL
                  AND EXISTS (SELECT 1 FROM mg_chat_messages c WHERE c.parent_id = p.id)
                """
            )
        )
        conn.execute(
            text(
                "ALTER TABLE mg_chat_sessions ADD COLUMN IF NOT EXISTS last_user_preview VARCHAR(160)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE mg_chat_sessions ADD COLUMN IF NOT EXISTS path_message_count INTEGER"
            )
        )


def get_db_session() -> Session:
    """
    获取聊天数据库会话
    """
    return SessionLocal()
