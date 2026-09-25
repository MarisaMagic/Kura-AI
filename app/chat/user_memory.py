"""跨会话用户长期记忆：只存稳定的偏好/约束，落 PostgreSQL，无向量库依赖。

设计取舍：
- 会话级记忆（决策/实体）不在此存储——分段摘要已覆盖，逐字取证走 read_session_history；
- 只保留 preference / constraint 两类，按 (user, agent, fact_key) 槽位 upsert，
  同槽位内容变化则覆盖（如「用中文」→「用英文」），不同槽位互不干扰；
- 读取不走向量检索：事实条数天然很少，模型按需调用 read_user_memory 工具即可。

隔离红线：user_id 必须是**当前调用者**（共享智能体场景下属主与共享用户各自独立），
与旧的 memory_scope 约定一致。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_

from app.chat.context_budget import estimate_tokens
from app.chat.database import SessionLocal
from app.chat.db_models import ChatUserMemory
from app.settings import settings

logger = logging.getLogger(__name__)

VALID_FACT_TYPES = ("preference", "constraint")
_TYPE_LABELS = {"preference": "偏好", "constraint": "约束"}


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def fact_key_of(fact_type: str, subject: str, content: str = "") -> str:
    """事实的**槽位**键：类型 + 归一化主题（主题缺失时退化用内容前缀）。

    刻意不含 content：同一主题的新偏好在同一槽位覆盖旧偏好，而不是堆出两条互相矛盾的记忆。
    内容是否真的变了由 content_hash 判定。
    """
    t = str(fact_type or "").strip().lower()
    subj = re.sub(r"\s+", "", str(subject or "").lower())
    if not subj:
        subj = re.sub(r"\s+", "", str(content or "").lower())[:64]
    return hashlib.sha1(f"{t}|{subj}".encode("utf-8")).hexdigest()[:32]


def fact_content_hash(fact: dict[str, Any]) -> str:
    """事实内容指纹：判断同槽位是否真的起了变化，避免无意义写入。"""
    blob = "\n".join(
        re.sub(r"\s+", "", str(fact.get(k) or "").lower())
        for k in ("type", "subject", "content", "why", "how_to_apply")
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def normalize_fact(item: dict[str, Any]) -> dict[str, Any] | None:
    """校验并归一化一条抽取出的事实；非偏好/约束或不合格返回 None。"""
    if not isinstance(item, dict):
        return None
    ftype = str(item.get("type") or "").strip().lower()
    if ftype not in VALID_FACT_TYPES:
        return None
    content = str(item.get("content") or item.get("text") or "").strip()
    if len(content) < 4:
        return None
    subject = str(item.get("subject") or "").strip()[:120]
    max_chars = max(64, _int_setting("CHAT_MEMORY_FACT_MAX_CHARS", 600))
    fact = {
        "type": ftype,
        "subject": subject,
        "content": content[:max_chars],
        "why": str(item.get("why") or "").strip()[:200],
        "how_to_apply": str(item.get("how_to_apply") or "").strip()[:200],
    }
    fact["fact_key"] = fact_key_of(ftype, subject, content)
    fact["content_hash"] = fact_content_hash(fact)
    return fact


def _row_to_dict(row: ChatUserMemory) -> dict[str, Any]:
    return {
        "fact_key": str(row.fact_key or ""),
        "fact_type": str(row.fact_type or ""),
        "subject": str(row.subject or ""),
        "content": str(row.content or ""),
        "why": str(row.why or ""),
        "how_to_apply": str(row.how_to_apply or ""),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _enforce_cap(db: Any, user_id: int, agent_id: int) -> None:
    """超上限时按 updated_at 删最旧（同一事务内）。"""
    cap = _int_setting("CHAT_MEMORY_USER_FACT_MAX", 500)
    if cap <= 0:
        return
    base = db.query(ChatUserMemory).filter(
        ChatUserMemory.user_id == int(user_id),
        ChatUserMemory.agent_id == int(agent_id),
    )
    total = int(base.count())
    if total <= cap:
        return
    victims = (
        base.with_entities(ChatUserMemory.id)
        .order_by(ChatUserMemory.updated_at.asc(), ChatUserMemory.id.asc())
        .limit(total - cap)
        .all()
    )
    ids = [int(v[0]) for v in victims]
    if ids:
        db.query(ChatUserMemory).filter(ChatUserMemory.id.in_(ids)).delete(
            synchronize_session=False
        )
        db.commit()


def store_user_facts(
    user_id: int,
    agent_id: int,
    facts: list[dict[str, Any]],
    *,
    session_id: str = "",
) -> dict[str, int]:
    """把抽取出的偏好/约束按槽位 upsert 到 PG（无 embedding、无 Milvus）。

    :param session_id: 仅用于日志定位，不参与存储键
    :return: {"inserted", "skipped", "updated"} 计数
    """
    out = {"inserted": 0, "skipped": 0, "updated": 0}
    if not getattr(settings, "CHAT_USER_MEMORY_ENABLED", True):
        return out
    normalized = [f for f in (normalize_fact(x) for x in (facts or [])) if f]
    if not normalized:
        return out
    # 同批次内同槽位只保留最后一条，避免批内自相矛盾
    by_key = {f["fact_key"]: f for f in normalized}

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for key, fact in by_key.items():
            row = (
                db.query(ChatUserMemory)
                .filter(
                    ChatUserMemory.user_id == int(user_id),
                    ChatUserMemory.agent_id == int(agent_id),
                    ChatUserMemory.fact_key == key,
                )
                .with_for_update()
                .first()
            )
            if row is None:
                db.add(
                    ChatUserMemory(
                        user_id=int(user_id),
                        agent_id=int(agent_id),
                        fact_key=key,
                        fact_type=fact["type"],
                        subject=fact["subject"],
                        content=fact["content"],
                        why=fact["why"],
                        how_to_apply=fact["how_to_apply"],
                        content_hash=fact["content_hash"],
                        created_at=now,
                        updated_at=now,
                    )
                )
                out["inserted"] += 1
            elif str(row.content_hash or "") == fact["content_hash"]:
                out["skipped"] += 1
            else:
                row.fact_type = fact["type"]
                row.subject = fact["subject"]
                row.content = fact["content"]
                row.why = fact["why"]
                row.how_to_apply = fact["how_to_apply"]
                row.content_hash = fact["content_hash"]
                row.updated_at = now
                out["updated"] += 1
        db.commit()
        _enforce_cap(db, int(user_id), int(agent_id))
    except Exception:
        db.rollback()
        logger.exception("store_user_facts failed session=%s", session_id)
        return {"inserted": 0, "skipped": 0, "updated": 0}
    finally:
        db.close()
    return out


def list_user_facts(
    user_id: int, agent_id: int, *, keyword: str = "", limit: int | None = None
) -> list[dict[str, Any]]:
    """按需读取用户长期记忆；keyword 非空时对主题/正文/依据做不区分大小写的包含匹配。"""
    max_items = max(1, int(limit or _int_setting("CHAT_MEMORY_READ_MAX_ITEMS", 50)))
    db = SessionLocal()
    try:
        q = db.query(ChatUserMemory).filter(
            ChatUserMemory.user_id == int(user_id),
            ChatUserMemory.agent_id == int(agent_id),
        )
        kw = (keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            q = q.filter(
                or_(
                    ChatUserMemory.subject.ilike(like),
                    ChatUserMemory.content.ilike(like),
                    ChatUserMemory.why.ilike(like),
                    ChatUserMemory.how_to_apply.ilike(like),
                )
            )
        rows = (
            q.order_by(ChatUserMemory.updated_at.desc(), ChatUserMemory.id.desc())
            .limit(max_items)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.exception("list_user_facts failed")
        return []
    finally:
        db.close()


def format_user_facts(rows: list[dict[str, Any]], *, max_tokens: int | None = None) -> str:
    """把事实渲染成给模型阅读的文本块，受 token 预算约束。"""
    if not rows:
        return "暂无跨会话长期记忆（用户偏好/约束）。"
    budget = max(
        200,
        int(
            max_tokens
            if max_tokens is not None
            else _int_setting("CHAT_MEMORY_TOOL_MAX_TOKENS", 3000)
        ),
    )
    header = "【用户长期记忆（跨会话，偏好与硬约束）】"
    lines = [header]
    used = estimate_tokens(header)
    for r in rows:
        label = _TYPE_LABELS.get(str(r.get("fact_type") or ""), "记忆")
        text = f"- [{label}] {r.get('subject') or ''}：{r.get('content') or ''}"
        if r.get("why"):
            text += f"（依据：{r['why']}）"
        if r.get("how_to_apply"):
            text += f"（适用：{r['how_to_apply']}）"
        cost = estimate_tokens(text)
        if used + cost > budget:
            break
        used += cost
        lines.append(text)
    return "\n".join(lines)


def delete_user_facts(
    user_id: int, agent_id: int, *, keyword: str = "", all: bool = False
) -> int:
    """删除长期记忆：``all=True`` 清空该用户在本智能体的全部；否则按关键词模糊匹配。

    匹配字段：subject / content / why / how_to_apply。两者都不给返回 0（由调用方提示）。
    """
    kw = (keyword or "").strip()
    if not all and not kw:
        return 0
    db = SessionLocal()
    try:
        q = db.query(ChatUserMemory).filter(
            ChatUserMemory.user_id == int(user_id),
            ChatUserMemory.agent_id == int(agent_id),
        )
        if not all:
            like = f"%{kw}%"
            q = q.filter(
                or_(
                    ChatUserMemory.subject.ilike(like),
                    ChatUserMemory.content.ilike(like),
                    ChatUserMemory.why.ilike(like),
                    ChatUserMemory.how_to_apply.ilike(like),
                )
            )
        n = q.delete(synchronize_session=False)
        db.commit()
        return int(n or 0)
    except Exception:
        db.rollback()
        logger.exception("delete_user_facts failed")
        return 0
    finally:
        db.close()


def purge_user_memory(user_id: int, agent_id: int) -> int:
    """删除某用户在某智能体下的全部长期记忆；返回删除条数。"""
    db = SessionLocal()
    try:
        n = (
            db.query(ChatUserMemory)
            .filter(
                ChatUserMemory.user_id == int(user_id),
                ChatUserMemory.agent_id == int(agent_id),
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)
    except Exception:
        db.rollback()
        logger.exception("purge_user_memory failed")
        return 0
    finally:
        db.close()


def purge_user_memory_for_agent(agent_id: int) -> int:
    """删除某智能体下所有用户的长期记忆（智能体被删除时调用）。"""
    db = SessionLocal()
    try:
        n = (
            db.query(ChatUserMemory)
            .filter(ChatUserMemory.agent_id == int(agent_id))
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)
    except Exception:
        db.rollback()
        logger.exception("purge_user_memory_for_agent failed")
        return 0
    finally:
        db.close()
