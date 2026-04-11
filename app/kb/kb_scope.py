"""知识库隔离键：每个 (用户, 智能体) 唯一。"""


def kb_scope_for(user_id: int, agent_id: int) -> str:
    """
    构建知识库隔离键（用户ID + 智能体ID）
    通过用户ID和智能体ID构建一个唯一的知识库隔离键，隔离不同用户和智能体的知识库。
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :return: 知识库隔离键
    """
    return f"u{int(user_id)}_a{int(agent_id)}"
