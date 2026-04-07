"""PostgreSQL（聊天专用）SQLAlchemy 引擎与会话工厂。"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.settings import settings

os.environ.setdefault("PGCLIENTENCODING", "UTF8")


def _sqlalchemy_database_url(raw: str) -> str:
    """优先使用 psycopg3；兼容 .env 中的 postgresql+psycopg2://。"""
    for old, new in (
        ("postgresql+psycopg2://", "postgresql+psycopg://"),
        ("postgres+psycopg2://", "postgresql+psycopg://"),
    ):
        if raw.startswith(old):
            return new + raw[len(old) :]
    return raw


def _make_engine():
    url = _sqlalchemy_database_url(settings.chat_database_url)
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def init_chat_db() -> None:
    import app.chat.db_models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db_session() -> Session:
    return SessionLocal()
