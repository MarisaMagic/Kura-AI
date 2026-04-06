"""智能体 API Key / Base URL：供对话/上游模型调用等后端逻辑使用。"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.user_agent import UserAgent
from app.utils.api_key_crypto import decrypt_api_key_safe


async def get_decrypted_api_key(user_id: int, agent_id: int) -> str | None:
    """
    校验归属后解密 API Key；若未配置或解密失败则返回 None。
    对话启动时调用本函数，将返回值仅用于当次请求或本会话内的 LLM 客户端。
    """
    cfg = await get_agent_llm_config(user_id, agent_id)
    return cfg.api_key if cfg else None


@dataclass
class AgentLlmConfig:
    api_key: str
    base_url: str | None
    model_name: str


def normalize_llm_base_url(url: str | None) -> str | None:
    """供 OpenAI 兼容客户端使用：空白视为未配置。"""
    if url is None:
        return None
    s = url.strip()
    return s if s else None


async def get_agent_llm_config(user_id: int, agent_id: int) -> AgentLlmConfig | None:
    """
    校验归属后返回解密后的 API Key 与 Base URL（若未填 base_url 则为 None）。
    """
    obj = await UserAgent.filter(id=agent_id, user_id=user_id).first()
    if not obj or not obj.api_key_ciphertext:
        return None
    key = decrypt_api_key_safe(obj.api_key_ciphertext)
    if not key:
        return None
    return AgentLlmConfig(
        api_key=key,
        base_url=normalize_llm_base_url(obj.base_url),
        model_name=obj.model_name,
    )
