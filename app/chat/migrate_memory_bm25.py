"""会话记忆集合迁移：启用 Milvus 服务端 BM25 Function（幂等，可安全重跑）。

旧集合的稀疏检索基于进程内 BM25 词表（跨重启索引漂移、IDF 失真），
新 schema 用服务端 BM25 Function（bm25_fn）替代本地稀疏向量；
dense 向量直读复制，不重调 embedding API。

用法（在项目根目录）：
    python -m app.chat.migrate_memory_bm25                    # 迁移，保留旧集合备份
    python -m app.chat.migrate_memory_bm25 --purge-backup     # 迁移成功后删除备份
    python -m app.chat.migrate_memory_bm25 --dry-run          # 只检查现状，不执行
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from pymilvus import MilvusClient

from app.chat.milvus_memory import ChatMemoryMilvusManager, get_chat_memory_milvus

_COPY_SCALAR_FIELDS = [
    "memory_scope",
    "text",
    "turn_index",
    "turn_key",
    "chunk_index",
    "chunk_id",
]

_INT_FIELDS = {"turn_index", "turn_key", "chunk_index"}

_BATCH_SIZE = 1000


def _has_bm25_function(desc: dict) -> bool:
    for fn in desc.get("functions") or []:
        if not isinstance(fn, dict):
            continue
        ftype_raw = fn.get("function_type") or fn.get("type")
        label = f"{fn.get('name') or ''} {getattr(ftype_raw, '__class__', type(ftype_raw)).__name__} {repr(ftype_raw)}".lower()
        if "bm25" in label:
            return True
    return False


def _row_to_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        return to_dict()  # type: ignore[no-any-return]
    return {}


def _copy_rows(client: MilvusClient, src: str, dst: str) -> int:
    iterator = client.query_iterator(
        collection_name=src,
        batch_size=_BATCH_SIZE,
        output_fields=["dense_embedding", *_COPY_SCALAR_FIELDS],
    )
    copied = 0
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows = []
            for raw in batch:
                d = _row_to_dict(raw)
                dense = d.get("dense_embedding")
                if not dense:
                    continue
                row: dict[str, Any] = {"dense_embedding": list(dense)}
                for field in _COPY_SCALAR_FIELDS:
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
                client.insert(collection_name=dst, data=rows)
                copied += len(rows)
                print(f"  已拷贝 {copied} 行...", flush=True)
    finally:
        try:
            iterator.close()
        except Exception:
            pass
    return copied


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移会话记忆集合到服务端 BM25 Function schema")
    parser.add_argument("--purge-backup", action="store_true", help="迁移成功后删除旧集合备份")
    parser.add_argument("--dry-run", action="store_true", help="仅检查并打印现状，不执行迁移")
    args = parser.parse_args(argv)

    mgr = get_chat_memory_milvus()
    client = mgr._get_client()
    name = mgr.collection_name
    backup = f"{name}_bak_legacy_sparse"
    tmp = f"{name}_bm25_v2"

    if not client.has_collection(name):
        print(f"集合 {name} 不存在，无需迁移（首次写入时会直接按含 BM25 Function 的新 schema 创建）。")
        return 0

    desc = client.describe_collection(name)
    desc_dict = desc if isinstance(desc, dict) else {}
    if _has_bm25_function(desc_dict):
        print(f"集合 {name} 已启用服务端 BM25 Function，跳过迁移。")
        return 0

    stats = client.get_collection_stats(name) or {}
    row_count = int(stats.get("row_count", 0) or 0)
    print(f"待迁移集合: {name} | 行数: {row_count}")
    print("迁移流程: 建临时新集合(含 BM25 Function) → 直读复制 dense 向量与标量 → 换名切换 → 旧集合保留为备份")
    if args.dry_run:
        print("--dry-run: 仅检查，未执行。")
        return 0

    if client.has_collection(tmp):
        print(f"发现残留临时集合 {tmp}，先删除...")
        client.drop_collection(tmp)
    if client.has_collection(backup):
        print(f"发现已有备份集合 {backup}，删除以便重新备份...")
        client.drop_collection(backup)

    try:
        print(f"创建临时集合 {tmp}（含 BM25 Function）...")
        tmp_mgr = ChatMemoryMilvusManager()
        tmp_mgr.client = client
        tmp_mgr.collection_name = tmp
        tmp_mgr.init_collection(collection_name=tmp)
        if not client.has_collection(tmp):
            print(f"临时集合 {tmp} 创建失败，中止。")
            return 1

        copied = _copy_rows(client, name, tmp)
        print(f"拷贝完成: {copied} 行 -> {tmp}")
        client.flush(collection_name=tmp)

        client.rename_collection(old_name=name, new_name=backup)
        client.rename_collection(old_name=tmp, new_name=name)
        print(f"集合切换完成: {name}（新 schema），旧集合已备份为 {backup}")
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
