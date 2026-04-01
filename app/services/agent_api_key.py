"""智能体 API Key 解密：供对话/上游模型调用等后端逻辑使用。"""

from __future__ import annotations

from app.models.user_agent import UserAgent
from app.utils.api_key_crypto import decrypt_api_key_safe


async def get_decrypted_api_key(user_id: int, agent_id: int) -> str | None:
    """
    校验归属后解密 API Key；若未配置或解密失败则返回 None。
    对话启动时调用本函数，将返回值仅用于当次请求或本会话内的 LLM 客户端。
    """
    obj = await UserAgent.filter(id=agent_id, user_id=user_id).first()
    if not obj or not obj.api_key_ciphertext:
        return None
    return decrypt_api_key_safe(obj.api_key_ciphertext)
