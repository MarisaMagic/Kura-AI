"""最近使用智能体：每用户最多 3 条，按 last_used_at 保留最新的。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.controllers.user import user_controller
from app.controllers.user_agent import user_agent_controller
from app.models.user_agent import UserAgent
from app.models.user_agent_recent import UserAgentRecent
from app.utils.user_agent_avatar import agent_avatar_url

RECENT_AGENT_LIMIT = 3


async def touch_recent_agent(user_id: int, agent_id: int) -> bool:
    """
    记录用户使用某智能体；校验归属后更新/插入，并裁剪为最多 RECENT_AGENT_LIMIT 条。
    """
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        return False

    rec = await UserAgentRecent.filter(user_id=user_id, agent_id=agent_id).first()
    now = datetime.now(timezone.utc)
    if rec:
        rec.last_used_at = now
        await rec.save()
    else:
        await UserAgentRecent.create(user_id=user_id, agent_id=agent_id, last_used_at=now)

    rows = await UserAgentRecent.filter(user_id=user_id).order_by("-last_used_at").all()
    if len(rows) > RECENT_AGENT_LIMIT:
        for r in rows[RECENT_AGENT_LIMIT:]:
            await r.delete()
    return True


async def list_recent_agents_public(user_id: int, limit: int = RECENT_AGENT_LIMIT) -> list[dict]:
    """返回最近使用的智能体公开字段（含 avatar_url），按最近使用倒序。"""
    user_obj = await user_controller.get(id=user_id)
    username = user_obj.username

    rows = await UserAgentRecent.filter(user_id=user_id).order_by("-last_used_at").limit(limit).all()
    out = []
    for r in rows:
        agent = await UserAgent.filter(id=r.agent_id, user_id=user_id).first()
        if not agent:
            continue
        d = await agent.to_dict(exclude_fields=["api_key_ciphertext"])
        d["has_api_key"] = bool(agent.api_key_ciphertext)
        d["avatar_url"] = agent_avatar_url(username, agent.avatar_filename)
        out.append(d)
    return out
