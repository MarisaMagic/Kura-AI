"""
长期记忆写入：episodic 分段摘要 + factual 稳定事实，配合分层收敛、滚动淘汰与闲置 TTL。

v1 的做法是「把每个离开窗口的轮次原文切块嵌入」，问题很直接：
向量库迅速堆满寒暄、失败轮、重复提问，召回质量被稀释，存储压力线性增长且永不回收。

v2 改为**只存蒸馏结果**：

- ``episodic``：压缩产生的分段摘要（9 段结构化）。约 12 轮才产 1 段，
  而不是十几条原文块；段被压缩侧归并成 level+1 后，对应的细粒度向量随之删除，
  向量数自然收敛到 O(log n)，**不需要额外的 LLM 调用**；
- ``factual``：从同一次摘要调用里顺带抽出的稳定事实（偏好/约束/决策/实体），
  按 fact_key 去重 upsert；偏好与约束写**用户级 scope**（跨会话生效），
  决策与实体写会话级 scope；
- ``raw``：原文块归档降级为可开关的兜底（默认关闭）。

被闸门拦下或未入库的内容并不会丢：原文永远 append-only 留在 PG，
可用 read_session_history 精确翻牌。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from app.chat import compact_store
from app.chat.memory_scope import session_memory_scope, user_memory_scope
from app.chat.memory_value import dedup_window_size, is_near_duplicate, passes_value_gate, shingles
from app.chat.milvus_memory import (
    KIND_EPISODIC,
    KIND_FACTUAL,
    KIND_RAW,
    get_chat_memory_milvus,
    memory_filter_expr,
)
from app.chat.memory_turns_text import turn_to_text
from app.chat.storage import storage
from app.kb.milvus_client import milvus_escape
from app.settings import settings

logger = logging.getLogger(__name__)

# 兼容旧引用（v1 raw 归档水位线）
_META_ARCHIVED_TURN_KEYS = "memory_archived_turn_keys"
# v2 metadata 键
_META_EPISODIC_IDS = "memory_episodic_chunk_ids"
_META_FACT_KEYS = "memory_fact_keys"
_META_FACT_HASHES = "memory_fact_hashes"
_META_DEDUP_FP = "memory_dedup_fingerprints"
_META_EVICTED_BELOW = "memory_evicted_below_turn_key"

_PREFERENCE_LIKE = ("preference", "constraint")
_VALID_FACT_TYPES = ("preference", "decision", "entity", "constraint")


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _int_setting_allow_zero(name: str, default: int) -> int:
    """允许显式配置 0 的整型读取（0 在这些开关上表示「关闭」，不能回退默认值）。"""
    v = getattr(settings, name, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool_setting(name: str, default: bool) -> bool:
    v = getattr(settings, name, default)
    return default if v is None else bool(v)


def _short_digest(*parts: Any) -> str:
    raw = ":".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _session_digest(user_id: int, agent_id: int, session_id: str) -> str:
    """会话级 chunk_id 前缀（隔离不同会话，避免主键撞车）。"""
    return _short_digest(user_id, agent_id, session_id)


def _user_digest(user_id: int, agent_id: int) -> str:
    """用户级 chunk_id 前缀。"""
    return _short_digest("u", user_id, agent_id)


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """按段落边界优先切块；无段落时退化为定长切分。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 2 <= max_chars:
            buf = f"{buf}\n\n{para}" if buf else para
            continue
        if buf:
            chunks.append(buf)
        while len(para) > max_chars:
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


def fact_key_of(fact_type: str, subject: str, content: str = "") -> str:
    """事实的**向量槽位**键：类型 + 归一化主题（主题缺失时退化用内容前缀）。

    注意这里刻意**不含 content**：同一主题的新偏好应覆盖旧偏好（同 chunk_id upsert），
    而不是在库里堆出两条互相矛盾的记忆。内容是否真的变了由 fact_content_hash 判定。
    """
    t = str(fact_type or "").strip().lower()
    subj = re.sub(r"\s+", "", str(subject or "").lower())
    if not subj:
        subj = re.sub(r"\s+", "", str(content or "").lower())[:64]
    return hashlib.sha1(f"{t}|{subj}".encode("utf-8")).hexdigest()[:32]


def fact_content_hash(fact: dict[str, Any]) -> str:
    """事实内容指纹：用于判断同槽位事实是否真的起了变化，避免重复嵌入。"""
    blob = "\n".join(
        re.sub(r"\s+", "", str(fact.get(k) or "").lower())
        for k in ("type", "subject", "content", "why", "how_to_apply")
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def normalize_fact(item: dict[str, Any]) -> dict[str, Any] | None:
    """校验并归一化一条抽取出的事实；不合格返回 None。"""
    if not isinstance(item, dict):
        return None
    ftype = str(item.get("type") or "").strip().lower()
    if ftype not in _VALID_FACT_TYPES:
        return None
    content = str(item.get("content") or item.get("text") or "").strip()
    if len(content) < 4:
        return None
    subject = str(item.get("subject") or "").strip()[:120]
    max_chars = max(64, _int_setting("CHAT_MEMORY_FACT_MAX_CHARS", 600))
    return {
        "type": ftype,
        "subject": subject,
        "content": content[:max_chars],
        "why": str(item.get("why") or "").strip()[:200],
        "how_to_apply": str(item.get("how_to_apply") or "").strip()[:200],
        "fact_key": fact_key_of(ftype, subject, content),
    }


def fact_scope(fact: dict[str, Any], *, user_id: int, agent_id: int, session_id: str) -> tuple[str, str]:
    """事实该落到哪一级 scope。

    偏好/约束是跨会话稳定的 → 用户级；决策/实体通常只在本任务内有意义 → 会话级。
    :return: (memory_scope, 展示用 scope 标签)
    """
    if str(fact.get("type")) in _PREFERENCE_LIKE:
        return user_memory_scope(user_id, agent_id), "user"
    return session_memory_scope(user_id, agent_id, session_id), "session"


def fact_text(fact: dict[str, Any]) -> str:
    """事实入库正文：带上 why / how_to_apply，检索命中后模型才知道怎么用。"""
    parts = [f"[{fact.get('type')}] {fact.get('subject') or ''}".strip(), str(fact.get("content") or "").strip()]
    if fact.get("why"):
        parts.append(f"依据：{fact['why']}")
    if fact.get("how_to_apply"):
        parts.append(f"适用方式：{fact['how_to_apply']}")
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# episodic：把压缩分段摘要同步进向量库
# ---------------------------------------------------------------------------


def archive_episodic_segments(user_id: int, agent_id: int, session_id: str) -> dict[str, Any]:
    """把当前路径上尚未入库的压缩分段摘要写成 episodic 向量，并清掉已被归并的旧向量。

    与压缩侧的段表保持同步即天然实现分层收敛：段被归并成 level+1 后，
    旧段从段表消失，这里就把对应的细粒度向量删掉，**不需要额外的 LLM 调用**。
    """
    out = {"inserted": 0, "removed": 0, "segments": 0}
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return out
    meta, session_ref_id = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
    if not session_ref_id:
        return out
    path_keys = path_turn_keys_for_session(user_id, agent_id, session_id)
    if not path_keys:
        return out

    segments = compact_store.chain_for_path(compact_store.load_segments(session_ref_id), path_keys)
    if not segments:
        return out
    out["segments"] = len(segments)

    digest = _session_digest(user_id, agent_id, session_id)
    scope = session_memory_scope(user_id, agent_id, session_id)
    cap = _milvus_text_cap()
    chunk_chars = max(256, min(_int_setting("CHAT_MEMORY_EPISODIC_CHUNK_CHARS", 2000), cap))

    archived = {str(c) for c in (meta.get(_META_EPISODIC_IDS) or [])}
    want_ids: set[str] = set()
    insert_rows: list[dict[str, Any]] = []
    now_ms = int(time.time() * 1000)

    for seg in segments:
        summary = str(seg.get("summary") or "").strip()
        if not summary:
            continue
        from_key = int(seg.get("from_turn_key") or 0)
        to_key = int(seg.get("to_turn_key") or 0)
        level = int(seg.get("level") or 0)
        pieces = _chunk_text(summary, chunk_chars)
        for ci, piece in enumerate(pieces):
            cid = f"ep_{digest}_{from_key}_{to_key}_L{level}_c{ci}"
            want_ids.add(cid)
            if cid in archived:
                continue
            insert_rows.append(
                {
                    "chunk_id": cid,
                    "memory_scope": scope,
                    "kind": KIND_EPISODIC,
                    "text": piece[:cap],
                    "turn_index": int(seg.get("from_index") or 0),
                    "turn_key": from_key,
                    "from_turn_key": from_key,
                    "to_turn_key": to_key,
                    "chunk_index": ci,
                    "level": level,
                    "value_score": 0.9,
                    "created_at": now_ms,
                }
            )

    milvus = get_chat_memory_milvus()
    try:
        milvus.init_collection()
        if insert_rows:
            from app.kb.multimodal_embedding import get_multimodal_embedding_service

            embedder = get_multimodal_embedding_service()
            dense_list = embedder.get_text_embeddings([r["text"] for r in insert_rows])
            for r, emb in zip(insert_rows, dense_list):
                r["dense_embedding"] = emb
            milvus.upsert(insert_rows)
            out["inserted"] = len(insert_rows)

        # 段被归并/分支切换后不再出现在链上，其向量应随之删除，避免召回到失效摘要
        stale = sorted(archived - want_ids)
        if stale:
            out["removed"] = milvus.delete_by_chunk_ids(stale)

        def _patch(m: dict) -> dict:
            return {_META_EPISODIC_IDS: sorted(want_ids)}

        storage.mutate_session_metadata(user_id, agent_id, session_id, _patch)
    except Exception:
        logger.exception("archive_episodic_segments failed")
    return out


def episodic_chunk_ids_for_session(user_id: int, agent_id: int, session_id: str) -> set[str]:
    """当前会话已入库的 episodic chunk_id 集合（淘汰/校验用）。"""
    meta = storage.get_session_metadata(user_id, agent_id, session_id)
    return {str(c) for c in (meta.get(_META_EPISODIC_IDS) or [])}


# ---------------------------------------------------------------------------
# factual：稳定事实去重 upsert
# ---------------------------------------------------------------------------


def store_facts(
    user_id: int,
    agent_id: int,
    session_id: str,
    facts: list[dict[str, Any]],
    *,
    turn_key: int = 0,
    turn_index: int = 0,
) -> dict[str, int]:
    """把抽取出的稳定事实按 fact_key（槽位）upsert 到对应 scope。

    同一槽位内容未变则跳过；内容变了（如偏好从「用中文」改为「用英文」）则覆盖写入，
    而**不是**因为 key 已存在就把新值丢掉。
    """
    out = {"inserted": 0, "skipped": 0, "updated": 0}
    if not _bool_setting("CHAT_MEMORY_FACTUAL_ENABLED", True):
        return out
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return out
    normalized = [f for f in (normalize_fact(x) for x in (facts or [])) if f]
    if not normalized:
        return out

    meta, _ref = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
    known_hashes = {
        str(k): str(v) for k, v in dict(meta.get(_META_FACT_HASHES) or {}).items()
    }
    user_known_hashes = {
        str(k): str(v) for k, v in dict(meta.get("memory_user_fact_hashes") or {}).items()
    }

    digest = _user_digest(user_id, agent_id)
    cap = _milvus_text_cap()
    # 以 chunk_id 为键去重（后写覆盖先写）：保证同批内同槽位只产生一条插入，
    # 不依赖 Milvus 对重复主键的未定义行为。
    rows_by_cid: dict[str, dict[str, Any]] = {}
    new_session_keys: set[str] = set()
    new_user_keys: set[str] = set()
    new_session_hashes: dict[str, str] = {}
    new_user_hashes: dict[str, str] = {}
    now_ms = int(time.time() * 1000)

    for fact in normalized:
        scope, level = fact_scope(
            fact, user_id=user_id, agent_id=agent_id, session_id=session_id
        )
        key = fact["fact_key"]
        chash = fact_content_hash(fact)
        is_user = level == "user"
        seen_hashes = user_known_hashes if is_user else known_hashes
        # 同批次内也要去重：LLM 可能对同一事实输出多条，若只看调用前的快照，
        # 会产生两条相同 chunk_id 的插入，且 metadata 记录的 hash 与实际留存的正文可能对不上。
        live = new_user_hashes if is_user else new_session_hashes
        effective = live.get(key, seen_hashes.get(key))
        if effective == chash:
            out["skipped"] += 1
            continue
        if effective is not None:
            out["updated"] += 1
        cid = (
            f"fa_{digest}_{key}"
            if is_user
            else f"fa_{_session_digest(user_id, agent_id, session_id)}_{key}"
        )
        rows_by_cid[cid] = {
            "chunk_id": cid,
            "memory_scope": scope,
            "kind": KIND_FACTUAL,
            "text": fact_text(fact)[:cap],
            "turn_index": int(turn_index or 0),
            "turn_key": int(turn_key or 0),
            "from_turn_key": int(turn_key or 0),
            "to_turn_key": int(turn_key or 0),
            "chunk_index": 0,
            "level": 0,
            "fact_key": key,
            "fact_type": fact["type"],
            "value_score": 1.0 if fact["type"] in _PREFERENCE_LIKE else 0.8,
            "created_at": now_ms,
        }
        if is_user:
            new_user_keys.add(key)
            new_user_hashes[key] = chash
        else:
            new_session_keys.add(key)
            new_session_hashes[key] = chash

    rows = list(rows_by_cid.values())
    if not rows:
        return out
    try:
        from app.kb.multimodal_embedding import get_multimodal_embedding_service

        milvus = get_chat_memory_milvus()
        milvus.init_collection()
        embedder = get_multimodal_embedding_service()
        dense_list = embedder.get_text_embeddings([r["text"] for r in rows])
        for r, emb in zip(rows, dense_list):
            r["dense_embedding"] = emb
        milvus.upsert(rows)
        out["inserted"] = len(rows)

        def _patch(m: dict) -> dict:
            patch: dict[str, Any] = {}
            if new_session_keys:
                patch[_META_FACT_KEYS] = sorted(
                    {str(k) for k in (m.get(_META_FACT_KEYS) or [])} | new_session_keys
                )
            if new_user_keys:
                patch["memory_user_fact_keys"] = sorted(
                    {str(k) for k in (m.get("memory_user_fact_keys") or [])} | new_user_keys
                )
            if new_session_hashes:
                merged = {str(k): str(v) for k, v in dict(m.get(_META_FACT_HASHES) or {}).items()}
                merged.update(new_session_hashes)
                patch[_META_FACT_HASHES] = merged
            if new_user_hashes:
                merged = {
                    str(k): str(v) for k, v in dict(m.get("memory_user_fact_hashes") or {}).items()
                }
                merged.update(new_user_hashes)
                patch["memory_user_fact_hashes"] = merged
            return patch

        storage.mutate_session_metadata(user_id, agent_id, session_id, _patch)
    except Exception:
        logger.exception("store_facts failed")
    return out


# ---------------------------------------------------------------------------
# raw：原文块归档（v1 行为，默认关闭的兜底）
# ---------------------------------------------------------------------------


def archive_raw_turns(user_id: int, agent_id: int, session_id: str) -> dict[str, int]:
    """把离开原文窗口且通过价值闸门的轮次原文切块入库（可开关的兜底通道）。"""
    out = {"inserted": 0, "gated": 0, "dup": 0}
    if not _bool_setting("CHAT_MEMORY_ARCHIVE_RAW_ENABLED", False):
        return out
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return out

    from app.chat.compact import verbatim_keep_from_for_session
    from app.chat.memory_turns import group_turn_pairs, split_system_prefix

    # 一次取回记录：既用于还原 LangChain 消息，也用于读 error_text
    # （失败标记只存在于 DB 列上，回放成消息后就丢了，价值闸门需要它）
    records = storage.get_session_messages(user_id, agent_id, session_id)
    if not records:
        return out
    error_ids = {int(r.get("message_id") or 0) for r in records if r.get("error_text")}
    pairs: list[tuple[int, Any]] = []
    for rec in records:
        msgs_one = storage._to_langchain_messages([rec])
        if msgs_one:
            pairs.append((int(rec.get("message_id") or 0), msgs_one[0]))
    if not pairs:
        return out
    msgs = [m for _, m in pairs]
    _, body = split_system_prefix(msgs)
    body_pairs = pairs[len(pairs) - len(body):] if body else []
    turn_pairs_list = group_turn_pairs(body_pairs)
    if not turn_pairs_list:
        return out
    path_turn_keys = [t[0][0] for t in turn_pairs_list]

    meta, session_ref_id = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
    keep_from = verbatim_keep_from_for_session(
        user_id,
        agent_id,
        session_id,
        path_turn_keys=path_turn_keys,
        meta=meta,
        session_ref_id=session_ref_id,
    )
    if keep_from <= 0:
        return out

    archived = {int(k) for k in (meta.get(_META_ARCHIVED_TURN_KEYS) or [])}
    fingerprints = [frozenset(f) for f in (meta.get(_META_DEDUP_FP) or []) if isinstance(f, list)]
    evicted_below = int(meta.get(_META_EVICTED_BELOW) or 0)

    scope = session_memory_scope(user_id, agent_id, session_id)
    digest = _session_digest(user_id, agent_id, session_id)
    cap = _milvus_text_cap()
    max_chunk = max(256, min(_int_setting("CHAT_MEMORY_CHUNK_MAX_CHARS", 1400), cap))

    pending: list[tuple[int, int, str]] = []
    new_keys: set[int] = set()
    new_fps: list[list[str]] = []
    for idx, tp in enumerate(turn_pairs_list[:keep_from]):
        turn_key = int(tp[0][0])
        if turn_key in archived or (evicted_below and turn_key <= evicted_below):
            continue
        turn = [m for _, m in tp]
        user_text, assistant_text = _turn_plain_parts(turn)
        has_error = any(int(rid) in error_ids for rid, _ in tp)
        ok, _score, _reasons = passes_value_gate(user_text, assistant_text, has_error=has_error)
        if not ok:
            out["gated"] += 1
            new_keys.add(turn_key)  # 记为已处理，避免每轮重复评估
            continue
        full_text = turn_to_text(turn)
        if is_near_duplicate(full_text, fingerprints):
            out["dup"] += 1
            new_keys.add(turn_key)
            continue
        pending.append((idx, turn_key, full_text))
        new_keys.add(turn_key)
        fp = sorted(shingles(full_text))
        new_fps.append(fp)
        # 同批次内也要互相去重：一次归档常处理多轮，用户连问两遍同一件事就在这个批次里
        fingerprints.append(frozenset(fp))

    if not pending:
        if new_keys:
            _commit_raw_watermark(user_id, agent_id, session_id, new_keys, new_fps)
        return out

    rows: list[dict[str, Any]] = []
    now_ms = int(time.time() * 1000)
    for idx, turn_key, full_text in pending:
        for ci, piece in enumerate(_chunk_text(full_text, max_chunk)):
            rows.append(
                {
                    "chunk_id": f"mem_{digest}_k{turn_key}_c{ci}",
                    "memory_scope": scope,
                    "kind": KIND_RAW,
                    "text": piece[:cap],
                    "turn_index": idx,
                    "turn_key": turn_key,
                    "from_turn_key": turn_key,
                    "to_turn_key": turn_key,
                    "chunk_index": ci,
                    "level": 0,
                    "value_score": 0.5,
                    "created_at": now_ms,
                }
            )
    try:
        from app.kb.multimodal_embedding import get_multimodal_embedding_service

        milvus = get_chat_memory_milvus()
        milvus.init_collection()
        embedder = get_multimodal_embedding_service()
        dense_list = embedder.get_text_embeddings([r["text"] for r in rows])
        for r, emb in zip(rows, dense_list):
            r["dense_embedding"] = emb
        milvus.upsert(rows)
        out["inserted"] = len(rows)
        _commit_raw_watermark(user_id, agent_id, session_id, new_keys, new_fps)
    except Exception:
        logger.exception("archive_raw_turns failed")
    return out


def _turn_plain_parts(turn: list) -> tuple[str, str]:
    """从一轮消息里取 (用户文本, 助手文本)。

    失败标记不在此判定：error_text 只存在于 DB 列上，回放成 LangChain 消息后就丢了，
    由调用方按消息行 id 从记录里读。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from app.chat.message_codec import msg_content_to_str

    user_text, assistant_text = "", ""
    for m in turn:
        if isinstance(m, HumanMessage) and not user_text:
            user_text = msg_content_to_str(m.content)
        elif isinstance(m, AIMessage):
            assistant_text += msg_content_to_str(m.content)
    return user_text, assistant_text


def _commit_raw_watermark(
    user_id: int, agent_id: int, session_id: str, new_keys: set[int], new_fps: list[list[str]]
) -> None:
    """行锁内合并 raw 归档水位与近重复指纹窗口（并发安全）。"""
    if not new_keys and not new_fps:
        return
    win = dedup_window_size()

    def _patch(m: dict) -> dict:
        patch: dict[str, Any] = {}
        if new_keys:
            cur = {int(k) for k in (m.get(_META_ARCHIVED_TURN_KEYS) or [])}
            patch[_META_ARCHIVED_TURN_KEYS] = sorted(cur | new_keys)
        if new_fps:
            cur_fp = [f for f in (m.get(_META_DEDUP_FP) or []) if isinstance(f, list)]
            patch[_META_DEDUP_FP] = (cur_fp + new_fps)[-win:]
        return patch

    try:
        storage.mutate_session_metadata(user_id, agent_id, session_id, _patch)
    except Exception:
        logger.exception("_commit_raw_watermark failed")


# ---------------------------------------------------------------------------
# 淘汰：滚动上限 + 闲置 TTL
# ---------------------------------------------------------------------------


def enforce_rolling_cap(user_id: int, agent_id: int, session_id: str) -> dict[str, int]:
    """会话级向量数超硬上限时，按 created_at 从旧到新删除。

    episodic 段已被压缩侧的分层归并收敛，这里只兜底 raw/异常增长；
    删除的只是向量，PG 原文与段摘要仍在，read_session_history 依然可翻牌。
    """
    out = {"removed": 0}
    hard_max = _int_setting_allow_zero("CHAT_MEMORY_HARD_MAX_VECTORS", 400)
    if hard_max <= 0:
        return out
    scope = session_memory_scope(user_id, agent_id, session_id)
    try:
        milvus = get_chat_memory_milvus()
        if not milvus.collection_exists():
            return out
        rows = milvus.query(
            memory_filter_expr(scope),
            output_fields=["chunk_id", "created_at", "kind", "turn_key", "fact_key"],
            limit=hard_max + _int_setting("CHAT_MEMORY_EVICT_BATCH", 200),
        )
        if len(rows) <= hard_max:
            return out
        rows.sort(key=lambda r: (int(r.get("created_at") or 0), str(r.get("chunk_id") or "")))
        victims = rows[: len(rows) - hard_max]
        ids = [str(r.get("chunk_id")) for r in victims if r.get("chunk_id")]
        if not ids:
            return out
        out["removed"] = milvus.delete_by_chunk_ids(ids)
        # metadata 要与实际存量对齐：被删掉的 episodic cid / 会话级事实槽位必须从「已入库」集合移除，
        # 否则段/事实仍在 PG 或可再抽取，却因仍被记为已归档而永不回填，检索永久缺失。
        evicted_episodic = {str(r.get("chunk_id")) for r in victims if r.get("kind") == KIND_EPISODIC}
        evicted_fact_keys = {
            str(r.get("fact_key")) for r in victims if r.get("kind") == KIND_FACTUAL and r.get("fact_key")
        }
        raw_victims = [int(r.get("turn_key") or 0) for r in victims if r.get("kind") == KIND_RAW]
        watermark = max(raw_victims) if raw_victims else 0

        def _patch(m: dict) -> dict:
            patch: dict[str, Any] = {}
            if evicted_episodic:
                cur = {str(c) for c in (m.get(_META_EPISODIC_IDS) or [])}
                patch[_META_EPISODIC_IDS] = sorted(cur - evicted_episodic)
            if evicted_fact_keys:
                patch[_META_FACT_KEYS] = sorted(
                    {str(k) for k in (m.get(_META_FACT_KEYS) or [])} - evicted_fact_keys
                )
                hashes = {str(k): str(v) for k, v in dict(m.get(_META_FACT_HASHES) or {}).items()}
                for k in evicted_fact_keys:
                    hashes.pop(k, None)
                patch[_META_FACT_HASHES] = hashes
            if watermark:
                patch[_META_EVICTED_BELOW] = max(int(m.get(_META_EVICTED_BELOW) or 0), watermark)
            return patch

        if evicted_episodic or evicted_fact_keys or watermark:
            storage.mutate_session_metadata(user_id, agent_id, session_id, _patch)
    except Exception:
        logger.exception("enforce_rolling_cap failed")
    return out


def enforce_user_fact_cap(user_id: int, agent_id: int, *, known_fact_count: int | None = None) -> int:
    """用户级事实条数上限：超出按 created_at 删最旧。

    :param known_fact_count: 调用方已从会话 metadata 数出的事实条数；
        低于上限时直接返回，省掉每轮一次的 Milvus 查询。
    """
    cap = _int_setting_allow_zero("CHAT_MEMORY_USER_FACT_MAX", 500)
    if cap <= 0:
        return 0
    if known_fact_count is not None and int(known_fact_count) <= cap:
        return 0
    scope = user_memory_scope(user_id, agent_id)
    try:
        milvus = get_chat_memory_milvus()
        if not milvus.collection_exists():
            return 0
        rows = milvus.query(
            memory_filter_expr(scope, kinds=[KIND_FACTUAL]),
            output_fields=["chunk_id", "created_at"],
            limit=cap + 200,
        )
        if len(rows) <= cap:
            return 0
        rows.sort(key=lambda r: (int(r.get("created_at") or 0), str(r.get("chunk_id") or "")))
        return milvus.delete_by_chunk_ids([str(r["chunk_id"]) for r in rows[: len(rows) - cap]])
    except Exception:
        logger.exception("enforce_user_fact_cap failed")
        return 0


def purge_stale_session_memory(days: int | None = None, *, batch: int = 200) -> dict[str, int]:
    """闲置 TTL GC：以 PG 会话 updated_at 为真相源，删掉长期不活跃会话的**会话级**向量。

    用户级事实不在此清理（另有 CHAT_MEMORY_USER_FACT_TTL_DAYS 与每用户上限）。
    """
    from datetime import datetime, timedelta

    out = {"sessions": 0, "removed": 0}
    ttl_days = _int_setting_allow_zero("CHAT_MEMORY_TTL_DAYS", 90) if days is None else int(days)
    if ttl_days <= 0:
        return out
    cutoff = datetime.utcnow() - timedelta(days=ttl_days)
    try:
        targets = compact_store.stale_session_ref_ids(cutoff, limit=batch)
    except Exception:
        logger.exception("purge_stale_session_memory: 查询闲置会话失败")
        return out
    if not targets:
        return out

    from app.chat.db_models import ChatSession as ChatSessionRow
    from app.chat.database import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(
                ChatSessionRow.id,
                ChatSessionRow.user_id,
                ChatSessionRow.agent_id,
                ChatSessionRow.session_id,
            )
            .filter(ChatSessionRow.id.in_([int(r) for r, _ in targets]))
            .all()
        )
    finally:
        db.close()

    try:
        milvus = get_chat_memory_milvus()
        if not milvus.collection_exists():
            return out
        for r in rows:
            uid, aid, sid = int(r.user_id), int(r.agent_id), str(r.session_id)
            scope = session_memory_scope(uid, aid, sid)
            try:
                milvus.delete_by_expr(f'memory_scope == "{milvus_escape(scope)}"')

                # 同步清空「已入库」记录：会话若重新活跃，段仍在 PG、事实可再抽取，均可回填。
                # 只清会话级键，用户级跨会话事实（memory_user_fact_*）不在本次删除范围内。
                def _patch(m: dict) -> dict:
                    return {
                        _META_EPISODIC_IDS: [],
                        _META_FACT_KEYS: [],
                        _META_FACT_HASHES: {},
                    }

                storage.mutate_session_metadata(uid, aid, sid, _patch)
                out["sessions"] += 1
            except Exception:
                logger.warning("TTL GC 删除会话记忆失败 scope=%s", scope, exc_info=True)
    except Exception:
        logger.exception("purge_stale_session_memory failed")
    return out


# ---------------------------------------------------------------------------
# 检索侧辅助
# ---------------------------------------------------------------------------


def episodic_allowed_to_keys(
    user_id: int,
    agent_id: int,
    session_id: str,
    path_turn_keys: list[int] | None = None,
    meta: dict | None = None,
    session_ref_id: int | None = None,
) -> list[int]:
    """当前路径上可检索的 episodic 段右端点 turn_key 列表。

    段链长度受 CHAT_COMPACT_MAX_SEGMENTS 约束（默认 8），因此这个 IN 列表天然很短，
    彻底解决了 v1「archived_turn_keys 无上限增长 → 过滤表达式无限膨胀」的问题。
    """
    if meta is None or session_ref_id is None:
        loaded_meta, loaded_ref = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
        meta = loaded_meta if meta is None else meta
        session_ref_id = loaded_ref if session_ref_id is None else session_ref_id
    if not session_ref_id:
        return []
    if path_turn_keys is None:
        path_turn_keys = path_turn_keys_for_session(user_id, agent_id, session_id)
    if not path_turn_keys:
        return []
    chain = compact_store.chain_for_path(compact_store.load_segments(session_ref_id), path_turn_keys)
    return sorted({int(s.get("to_turn_key") or 0) for s in chain if s.get("to_turn_key")})


def path_turn_keys_for_session(user_id: int, agent_id: int, session_id: str) -> list[int]:
    """当前路径各轮的稳定身份（turn_key = 轮首用户消息行 id）。无消息时返回空列表。"""
    from app.chat.memory_turns import group_turn_pairs, split_system_prefix

    pairs = storage.load_path_with_ids(user_id, agent_id, session_id)
    if not pairs:
        return []
    msgs = [m for _, m in pairs]
    _, body = split_system_prefix(msgs)
    body_pairs = pairs[len(pairs) - len(body):] if body else []
    return [t[0][0] for t in group_turn_pairs(body_pairs) if t]


def archived_turn_keys_on_path(
    user_id: int,
    agent_id: int,
    session_id: str,
    path_turn_keys: list[int] | None = None,
    meta: dict | None = None,
) -> list[int]:
    """（raw 模式）已归档且落在当前路径上的 turn_key 列表。"""
    if path_turn_keys is None:
        path_turn_keys = path_turn_keys_for_session(user_id, agent_id, session_id)
    if not path_turn_keys:
        return []
    if meta is None:
        meta = storage.get_session_metadata(user_id, agent_id, session_id)
    archived = {int(k) for k in (meta.get(_META_ARCHIVED_TURN_KEYS) or [])}
    return [k for k in path_turn_keys if k in archived]


def _milvus_text_cap() -> int:
    return max(512, _int_setting("CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH", 8192))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def archive_session_memory(user_id: int, agent_id: int, session_id: str) -> dict[str, Any]:
    """长期记忆写入主入口：episodic 同步 → raw 兜底 → 滚动上限。

    factual 的抽取与压缩摘要共用同一次 LLM 调用，由 compact 侧回调 store_facts。
    """
    info: dict[str, Any] = {}
    if not _bool_setting("CHAT_USE_SESSION_MEMORY", True):
        return info
    try:
        info["episodic"] = archive_episodic_segments(user_id, agent_id, session_id)
    except Exception:
        logger.exception("archive_episodic_segments")
    try:
        info["raw"] = archive_raw_turns(user_id, agent_id, session_id)
    except Exception:
        logger.exception("archive_raw_turns")
    try:
        info["evict"] = enforce_rolling_cap(user_id, agent_id, session_id)
    except Exception:
        logger.exception("enforce_rolling_cap")
    try:
        # 先从会话 metadata 数一遍已入库事实，未达上限就不必每轮都查一次 Milvus
        meta = storage.get_session_metadata(user_id, agent_id, session_id)
        known = len(meta.get("memory_user_fact_keys") or [])
        info["user_fact_evict"] = enforce_user_fact_cap(
            user_id, agent_id, known_fact_count=known
        )
    except Exception:
        logger.exception("enforce_user_fact_cap")
    return info


def schedule_archive_session_memory(user_id: int, agent_id: int, session_id: str) -> None:
    """兼容旧调用名：收尾任务统一走 post_turn_job（含预压缩与锁）。"""
    from app.chat.post_turn_job import schedule_post_turn

    schedule_post_turn(user_id, agent_id, session_id)


def purge_session_memory_vectors(user_id: int, agent_id: int, session_id: str) -> None:
    """删除会话在 Milvus 中的**会话级**记忆向量（删会话前调用）。

    用户级事实跨会话有效，不随单个会话删除；如需彻底清除走用户级接口。
    """
    try:
        scope = session_memory_scope(user_id, agent_id, session_id)
        mgr = get_chat_memory_milvus()
        if mgr.collection_exists():
            mgr.delete_by_scope(scope)
    except Exception:
        logger.exception("purge_session_memory_vectors")


def purge_user_memory_vectors(user_id: int, agent_id: int) -> None:
    """删除某用户在某智能体下的**用户级**长期记忆（删除智能体/用户主动清除时调用）。"""
    try:
        scope = user_memory_scope(user_id, agent_id)
        mgr = get_chat_memory_milvus()
        if mgr.collection_exists():
            mgr.delete_by_scope(scope)
    except Exception:
        logger.exception("purge_user_memory_vectors")


def purge_all_user_memory_for_agent(agent_id: int) -> None:
    """删除**所有用户**在某智能体下的用户级长期记忆（智能体被删除时调用）。

    共享智能体场景下，属主与共享用户各自拥有独立的 ``u:`` scope；
    智能体一旦删除，这些 scope 都成了不可达的孤儿数据，必须一并清掉。
    """
    try:
        mgr = get_chat_memory_milvus()
        if not mgr.collection_exists():
            return
        # scope 形如 u:u{uid}_a{aid}；按 _a{aid} 结尾匹配，不依赖具体 uid
        mgr.delete_by_expr(f'memory_scope like "%_a{int(agent_id)}"')
    except Exception:
        logger.exception("purge_all_user_memory_for_agent")
