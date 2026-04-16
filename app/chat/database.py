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
    # create_all 不会为已有表添加新列，需显式迁移
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS rag_steps JSON")
        )
        conn.execute(
            text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS content_json JSON")
        )
        conn.execute(text("ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS error_text TEXT"))


def get_db_session() -> Session:
    """
    获取聊天数据库会话
    """
    return SessionLocal()
