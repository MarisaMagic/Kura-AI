"""记忆在 Milvus 中的隔离键（scope）。

两级 scope：

- **会话级** ``s:u{user}_a{agent}_s{session}``：本会话的 episodic 分段摘要与任务态事实。
  随会话删除而清理，受滚动上限与闲置 TTL 约束。
- **用户级** ``u:u{user}_a{agent}``：跨会话长期记忆（稳定偏好、硬约束、长期实体）。
  只随用户主动删除或用户级 TTL 清理。

隔离红线：user 段必须是**当前调用者**的 user_id，绝不能填智能体属主的 id。
共享智能体（user_agent_share）场景下，属主与共享用户各自拥有独立的记忆空间；
若误用属主 id，共享用户就能读到属主的偏好，属主也能被共享用户的对话污染记忆。
（知识库的 kb_scope 相反：它按属主隔离，因为共享的是属主的资料库。）
"""

from __future__ import annotations

SESSION_SCOPE_PREFIX = "s:"
USER_SCOPE_PREFIX = "u:"


def _norm(user_id: int, agent_id: int, session_id: str = "") -> str:
    return f"u{int(user_id)}_a{int(agent_id)}" + (f"_s{(session_id or '').strip()}" if session_id else "")


def session_memory_scope(user_id: int, agent_id: int, session_id: str) -> str:
    """会话级 scope：仅本会话可见。"""
    return SESSION_SCOPE_PREFIX + _norm(user_id, agent_id, session_id)


def user_memory_scope(user_id: int, agent_id: int) -> str:
    """用户级 scope：同一用户在同一智能体下跨会话共享。

    :param user_id: **当前调用者**的 user_id（共享智能体时不是属主 id）
    """
    return USER_SCOPE_PREFIX + _norm(user_id, agent_id)


def memory_scope_for(user_id: int, agent_id: int, session_id: str) -> str:
    """兼容旧调用名：等价于 session_memory_scope。"""
    return session_memory_scope(user_id, agent_id, session_id)


def is_user_scope(scope: str) -> bool:
    return str(scope or "").startswith(USER_SCOPE_PREFIX)


def is_session_scope(scope: str) -> bool:
    return str(scope or "").startswith(SESSION_SCOPE_PREFIX)
