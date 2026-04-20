"""当前会话记忆在 Milvus 中的隔离键。"""


def memory_scope_for(user_id: int, agent_id: int, session_id: str) -> str:
    """
    构建单会话记忆范围键：u{user}_a{agent}_s{session_id}
    """
    sid = (session_id or "").strip()
    return f"u{int(user_id)}_a{int(agent_id)}_s{sid}"
