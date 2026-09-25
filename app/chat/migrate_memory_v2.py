"""会话记忆集合迁移到 v2 schema（幂等，可安全重跑）。

v2 的两处不兼容变更：
- **主键从 auto_id 的 INT64 ``id`` 改为 VARCHAR ``chunk_id``**：写入天然幂等。
  v1 的并发丢更新会让同一轮被重复归档并插出重复行，v2 下同 chunk_id 直接覆盖；
- 新增 ``kind`` / ``from_turn_key`` / ``to_turn_key`` / ``level`` /
  ``fact_key`` / ``fact_type`` / ``value_score`` / ``created_at``，
  支撑蒸馏记忆（episodic/factual）、分层收敛与 TTL 淘汰。

dense 向量直读复制，不重调 embedding API；v1 存量行统一标为 ``kind="raw"``。
复制过程按 chunk_id upsert，v1 遗留的重复行会被自然合并。

用法（在项目根目录）：
    python -m app.chat.migrate_memory_v2                 # 迁移，保留旧集合备份
    python -m app.chat.migrate_memory_v2 --purge-backup  # 迁移成功后删除备份
    python -m app.chat.migrate_memory_v2 --dry-run       # 只检查现状，不执行
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from typing import Any

from pymilvus import MilvusClient

from app.chat.milvus_memory import (
    KIND_RAW,
    ChatMemoryMilvusManager,
    _REQUIRED_FIELDS,
    _field_names_from_desc,
    _pk_is_chunk_id,
    get_chat_memory_milvus,
)

_COPY_SCALAR_FIELDS = [
    "memory_scope",
    "text",
    "turn_index",
    "turn_key",
    "chunk_index",
    "chunk_id",
]
_INT_FIELDS = {"turn_index", "turn_key", "chunk_index"}
_BATCH_SIZE = 500

# v1 scope 无前缀（u{uid}_a{aid}_s{sid}），v2 加了 s:/u: 前缀用于区分会话级与用户级。
# 迁移时必须改写，否则检索/删除/GC 都按新前缀构造表达式，旧数据将永远不可达且无法清理。
# session id 允许为空（`u1_a2_s`），故用 `.*` 而非 `.+`。
_V1_SESSION_SCOPE_RE = re.compile(r"^u\d+_a\d+_s.*$")
_V1_USER_SCOPE_RE = re.compile(r"^u\d+_a\d+$")


def migrate_scope(scope: str) -> tuple[str, str]:
    """把 v1 scope 改写为 v2 scope。

    :return: (新 scope, 状态)：kept=已是 v2 / rewritten=已改写 / unknown=无法识别
    """
    s = str(scope or "")
    if s.startswith(("s:", "u:")):
        return s, "kept"
    if _V1_SESSION_SCOPE_RE.match(s):
        return "s:" + s, "rewritten"
    if _V1_USER_SCOPE_RE.match(s):
        return "u:" + s, "rewritten"
    return s, "unknown"


def _row_to_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        return to_dict()  # type: ignore[no-any-return]
    return {}


def is_v2(desc: dict) -> bool:
    """集合是否已是 v2（主键为 chunk_id 且必备字段齐全）。"""
    fields = _field_names_from_desc(desc)
    return _pk_is_chunk_id(desc) and not (_REQUIRED_FIELDS - fields)


def _copy_rows(client: MilvusClient, src: str, dst: str) -> dict[str, int]:
    """直读复制 dense 向量与标量，并把 v1 scope 改写为 v2 scope。

    :return: {"copied", "deduped", "scope_rewritten", "scope_unknown"}
    """
    iterator = client.query_iterator(
        collection_name=src,
        batch_size=_BATCH_SIZE,
        output_fields=["dense_embedding", *_COPY_SCALAR_FIELDS],
    )
    stats = {"copied": 0, "deduped": 0, "scope_rewritten": 0, "scope_unknown": 0}
    seen: set[str] = set()
    now_ms = int(time.time() * 1000)
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows: list[dict[str, Any]] = []
            for raw in batch:
                d = _row_to_dict(raw)
                dense = d.get("dense_embedding")
                if not dense:
                    continue
                scope, scope_status = migrate_scope(str(d.get("memory_scope") or ""))
                if scope_status == "rewritten":
                    stats["scope_rewritten"] += 1
                elif scope_status == "unknown":
                    stats["scope_unknown"] += 1
                cid = str(d.get("chunk_id") or "").strip()
                if not cid:
                    # v1 早期行可能没有 chunk_id，用 (scope, turn_key, chunk_index) 合成一个稳定主键
                    cid = "legacy_{}_{}_{}".format(
                        scope[:40],
                        int(d.get("turn_key") or 0),
                        int(d.get("chunk_index") or 0),
                    )
                if cid in seen:
                    stats["deduped"] += 1
                    continue
                seen.add(cid)
                row: dict[str, Any] = {
                    "chunk_id": cid[:512],
                    "memory_scope": scope,
                    "dense_embedding": list(dense),
                    "kind": KIND_RAW,
                    "from_turn_key": int(d.get("turn_key") or 0),
                    "to_turn_key": int(d.get("turn_key") or 0),
                    "level": 0,
                    "fact_key": "",
                    "fact_type": "",
                    "value_score": 0.5,
                    "created_at": now_ms,
                }
                for field in _COPY_SCALAR_FIELDS:
                    if field in ("chunk_id", "memory_scope"):
                        continue
                    value = d.get(field)
                    if field in _INT_FIELDS:
                        try:
                            value = int(value or 0)
                        except (TypeError, ValueError):
                            value = 0
                    else:
                        value = value or ""
                    row[field] = value
                rows.append(row)
            if rows:
                client.upsert(collection_name=dst, data=rows)
                stats["copied"] += len(rows)
                print(f"  已拷贝 {stats['copied']} 行...", flush=True)
    finally:
        try:
            iterator.close()
        except Exception:
            pass
    return stats


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移会话记忆集合到 v2 schema（chunk_id 主键 + 蒸馏记忆字段）")
    parser.add_argument("--purge-backup", action="store_true", help="迁移成功后删除旧集合备份")
    parser.add_argument("--dry-run", action="store_true", help="仅检查并打印现状，不执行迁移")
    args = parser.parse_args(argv)

    mgr = get_chat_memory_milvus()
    client = mgr._get_client()
    name = mgr.collection_name
    backup = f"{name}_bak_v1"
    tmp = f"{name}_v2_tmp"

    if not client.has_collection(name):
        print(f"集合 {name} 不存在，无需迁移（首次写入时会直接按 v2 schema 创建）。")
        return 0

    desc = client.describe_collection(name)
    desc_dict = desc if isinstance(desc, dict) else {}
    if is_v2(desc_dict):
        print(f"集合 {name} 已是 v2 schema，跳过迁移。")
        return 0

    fields = sorted(_field_names_from_desc(desc_dict))
    stats = client.get_collection_stats(name) or {}
    row_count = int(stats.get("row_count", 0) or 0)
    print(f"待迁移集合: {name} | 行数: {row_count} | 现有字段: {fields}")
    print("迁移流程: 建 v2 临时集合 → 直读复制 dense 与标量（按 chunk_id 去重）→ 换名切换 → 旧集合保留为备份")
    if args.dry_run:
        print("--dry-run: 仅检查，未执行。")
        return 0

    for stale in (tmp, backup):
        if client.has_collection(stale):
            print(f"发现残留集合 {stale}，先删除...")
            client.drop_collection(stale)

    try:
        print(f"创建临时集合 {tmp}（v2 schema）...")
        tmp_mgr = ChatMemoryMilvusManager()
        tmp_mgr.client = client
        tmp_mgr.collection_name = tmp
        tmp_mgr.init_collection(collection_name=tmp)
        if not client.has_collection(tmp):
            print("临时集合创建失败，中止。")
            return 1

        stats = _copy_rows(client, name, tmp)
        print(
            f"拷贝完成: {stats['copied']} 行 -> {tmp}"
            f"（合并重复主键 {stats['deduped']} 行，scope 前缀改写 {stats['scope_rewritten']} 行）"
        )
        if stats["scope_unknown"]:
            print(
                f"⚠️ 有 {stats['scope_unknown']} 行 scope 无法识别（既非 v2 前缀也非 v1 格式），"
                "已原样复制；这些行将无法被检索/清理，请人工确认。"
            )
        client.flush(collection_name=tmp)

        client.rename_collection(old_name=name, new_name=backup)
        client.rename_collection(old_name=tmp, new_name=name)
        print(f"集合切换完成: {name}（v2 schema），旧集合已备份为 {backup}")
        mgr._init_done = False
        mgr._has_bm25 = False
        mgr.init_collection()

        if args.purge_backup and client.has_collection(backup):
            client.drop_collection(backup)
            print(f"已按 --purge-backup 删除备份 {backup}")

        final_stats = client.get_collection_stats(name) or {}
        print(f"迁移成功: {name} 现含 {int(final_stats.get('row_count', 0) or 0)} 行。")
        if not args.purge_backup:
            print(f"提示: 确认无误后可手动删除备份，MilvusClient(uri).drop_collection('{backup}')。")
        print("提示: v1 存量行已标为 kind=raw；新的蒸馏记忆（episodic/factual）会在后续对话中自动写入。")
        return 0
    except Exception:
        traceback.print_exc()
        try:
            if client.has_collection(tmp):
                client.drop_collection(tmp)
        except Exception:
            pass
        try:
            if client.has_collection(backup) and not client.has_collection(name):
                client.rename_collection(old_name=backup, new_name=name)
                print(f"已回滚: {backup} 恢复为 {name}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(run())
