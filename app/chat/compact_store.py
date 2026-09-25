"""压缩分段摘要的持久化与「路径前缀链」匹配。

分段（segment）= 一段连续轮次区间 [from_index, to_index) 的结构化摘要，落 mg_chat_compact_segments。
段以 turn_key 区间标识，不同分支的段共存于一张表，靠路径校验挑出当前分支可用的连续前缀链。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.chat.database import SessionLocal
from app.chat.db_models import ChatCompactSegment

logger = logging.getLogger(__name__)


def seg_id_of(from_turn_key: int, to_turn_key: int) -> str:
    """段稳定标识（幂等 upsert 键）。"""
    return f"s_{int(from_turn_key)}_{int(to_turn_key)}"


def _row_to_dict(row: ChatCompactSegment) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "seg_id": str(row.seg_id or seg_id_of(row.from_turn_key, row.to_turn_key)),
        "from_turn_key": int(row.from_turn_key),
        "to_turn_key": int(row.to_turn_key),
        "from_index": int(row.from_index),
        "to_index": int(row.to_index),
        "level": int(row.level or 0),
        "merged_from": list(row.merged_from or []),
        "summary": str(row.summary or ""),
        "tokens": int(row.tokens or 0),
        "model": row.model,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def load_segments(session_ref_id: int | None) -> list[dict[str, Any]]:
    """读取会话全部分段摘要，按 from_index 升序。"""
    if not session_ref_id:
        return []
    db = SessionLocal()
    try:
        rows = (
            db.query(ChatCompactSegment)
            .filter(ChatCompactSegment.session_ref_id == int(session_ref_id))
            .order_by(ChatCompactSegment.from_index.asc(), ChatCompactSegment.id.asc())
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db.close()


def is_valid_for_path(seg: dict[str, Any], path_turn_keys: list[int]) -> bool:
    """段是否落在当前路径上：区间两端的 turn_key 必须与路径同下标的 turn_key 一致。"""
    fi = int(seg.get("from_index", -1))
    ti = int(seg.get("to_index", -1))
    if fi < 0 or ti <= fi or ti > len(path_turn_keys):
        return False
    return (
        int(path_turn_keys[fi]) == int(seg.get("from_turn_key", -1))
        and int(path_turn_keys[ti - 1]) == int(seg.get("to_turn_key", -2))
    )


def chain_for_path(segments: list[dict[str, Any]], path_turn_keys: list[int]) -> list[dict[str, Any]]:
    """挑出覆盖路径「最长连续前缀」的段链（从 index 0 起，首尾相接）。

    分叉后不匹配的段自然落选；同一区间若有多份（不同 level）取 level 最高者（更粗但更省 token）。
    """
    if not segments or not path_turn_keys:
        return []
    valid = [s for s in segments if is_valid_for_path(s, path_turn_keys)]
    if not valid:
        return []
    by_from: dict[int, dict[str, Any]] = {}
    for s in valid:
        fi = int(s["from_index"])
        cur = by_from.get(fi)
        if cur is None or int(s.get("level", 0)) > int(cur.get("level", 0)):
            by_from[fi] = s
    chain: list[dict[str, Any]] = []
    pos = 0
    while True:
        nxt = by_from.get(pos)
        if nxt is None:
            break
        chain.append(nxt)
        pos = int(nxt["to_index"])
        if pos >= len(path_turn_keys):
            break
    return chain


def covered_turn_count(chain: list[dict[str, Any]]) -> int:
    """段链已覆盖的轮数（= 原文窗口起始下标）。"""
    if not chain:
        return 0
    return max(0, int(chain[-1].get("to_index", 0)))


def chain_tokens(chain: list[dict[str, Any]]) -> int:
    """段链摘要自身的 token 总量。"""
    return sum(int(s.get("tokens", 0) or 0) for s in chain)


def upsert_segment(
    session_ref_id: int,
    *,
    from_index: int,
    to_index: int,
    from_turn_key: int,
    to_turn_key: int,
    summary: str,
    tokens: int = 0,
    level: int = 0,
    merged_from: list[str] | None = None,
    model: str | None = None,
) -> str:
    """写入/覆盖一个分段摘要；返回 seg_id。按 (session, from_turn_key, to_turn_key) 唯一。"""
    text = (summary or "").strip()
    if not text or to_index <= from_index:
        return ""
    sid = seg_id_of(from_turn_key, to_turn_key)
    db = SessionLocal()
    try:
        row = (
            db.query(ChatCompactSegment)
            .filter(
                ChatCompactSegment.session_ref_id == int(session_ref_id),
                ChatCompactSegment.from_turn_key == int(from_turn_key),
                ChatCompactSegment.to_turn_key == int(to_turn_key),
            )
            .with_for_update()
            .first()
        )
        if row is None:
            row = ChatCompactSegment(
                session_ref_id=int(session_ref_id),
                seg_id=sid,
                from_turn_key=int(from_turn_key),
                to_turn_key=int(to_turn_key),
            )
            db.add(row)
        row.from_index = int(from_index)
        row.to_index = int(to_index)
        row.level = int(level)
        row.merged_from = list(merged_from or []) or None
        row.summary = text
        row.tokens = int(tokens or 0)
        row.model = model
        db.commit()
        return sid
    except Exception:
        db.rollback()
        logger.exception("upsert_segment failed")
        return ""
    finally:
        db.close()


def delete_segments(session_ref_id: int, seg_ids: list[str]) -> int:
    """按 seg_id 删除段（归并后清理被合并的细粒度段）。"""
    ids = [str(s) for s in (seg_ids or []) if s]
    if not session_ref_id or not ids:
        return 0
    db = SessionLocal()
    try:
        n = (
            db.query(ChatCompactSegment)
            .filter(
                ChatCompactSegment.session_ref_id == int(session_ref_id),
                ChatCompactSegment.seg_id.in_(ids),
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)
    except Exception:
        db.rollback()
        logger.exception("delete_segments failed")
        return 0
    finally:
        db.close()


def delete_segments_below(session_ref_id: int, to_index: int) -> int:
    """删除完全落在 to_index 之前的段（分支切换后清理不再可达的旧段）。"""
    if not session_ref_id:
        return 0
    db = SessionLocal()
    try:
        n = (
            db.query(ChatCompactSegment)
            .filter(
                ChatCompactSegment.session_ref_id == int(session_ref_id),
                ChatCompactSegment.to_index <= int(to_index),
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)
    except Exception:
        db.rollback()
        logger.exception("delete_segments_below failed")
        return 0
    finally:
        db.close()


def count_segments(session_ref_id: int | None) -> int:
    """会话段总数（用于归并触发判断）。"""
    if not session_ref_id:
        return 0
    db = SessionLocal()
    try:
        return int(
            db.query(ChatCompactSegment)
            .filter(ChatCompactSegment.session_ref_id == int(session_ref_id))
            .count()
        )
    finally:
        db.close()


def oldest_segments(session_ref_id: int, level: int, limit: int) -> list[dict[str, Any]]:
    """取指定 level 的最旧若干段（分层归并的输入）。"""
    if not session_ref_id or limit <= 0:
        return []
    db = SessionLocal()
    try:
        rows = (
            db.query(ChatCompactSegment)
            .filter(
                ChatCompactSegment.session_ref_id == int(session_ref_id),
                ChatCompactSegment.level == int(level),
            )
            .order_by(ChatCompactSegment.from_index.asc(), ChatCompactSegment.id.asc())
            .limit(int(limit))
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db.close()


def purge_segments_for_session(session_ref_id: int | None) -> int:
    """删除会话全部分段摘要（会话删除时由 FK 级联兜底，此处供脚本/异常清理用）。"""
    if not session_ref_id:
        return 0
    db = SessionLocal()
    try:
        n = (
            db.query(ChatCompactSegment)
            .filter(ChatCompactSegment.session_ref_id == int(session_ref_id))
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)
    except Exception:
        db.rollback()
        logger.exception("purge_segments_for_session failed")
        return 0
    finally:
        db.close()


def stale_session_ref_ids(before: datetime, limit: int = 200) -> list[tuple[int, datetime]]:
    """按会话 updated_at 找出闲置会话（TTL GC 用），返回 (session_ref_id, updated_at)。"""
    from app.chat.db_models import ChatSession as ChatSessionRow

    db = SessionLocal()
    try:
        rows = (
            db.query(ChatSessionRow.id, ChatSessionRow.updated_at)
            .filter(ChatSessionRow.updated_at < before)
            .order_by(ChatSessionRow.updated_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [(int(r.id), r.updated_at) for r in rows]
    finally:
        db.close()
