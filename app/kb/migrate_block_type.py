"""知识库集合迁移：新增 block_type / code_language 字段（结构感知分块）。

旧集合无这两个字段，分块管线升级后新文档需要写入。参照 migrate_analyzer.py 的
复制式迁移：建临时新集合 → 直读复制 dense 向量与标量（新字段按 content_type 回填：
文本块 text、图片块空串）→ 换名切换 → 旧集合留备份。不重调 embedding API，零费用。

注意：迁移只补齐字段默认值，存量代码/表格块仍按普通文本渲染；需要结构化语义的
旧文档请重传（kb_service 会因 chunk_pipeline_version 不一致而重建）。

用法（在项目根目录、conda 环境「Kura-AI」下）：
    python -m app.kb.migrate_block_type                    # 迁移，保留旧集合备份
    python -m app.kb.migrate_block_type --purge-backup     # 迁移成功后删除备份
    python -m app.kb.migrate_block_type --dry-run          # 只检查现状，不执行
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from pymilvus import MilvusClient

from app.kb.migrate_bm25 import _COPY_SCALAR_FIELDS, _INT_FIELDS, _row_to_dict
from app.kb.milvus_client import MilvusManager

_BACKUP_SUFFIX = "_bak_no_block_type"
_TMP_SUFFIX = "_block_type_v2"
_BATCH_SIZE = 1000
_NEW_FIELDS = ("block_type", "code_language")


def _has_field(desc: dict, name: str) -> bool:
    """集合 schema 是否已含指定字段。"""
    for field in desc.get("fields") or []:
        if isinstance(field, dict) and field.get("name") == name:
            return True
    return False


def _copy_rows(client: MilvusClient, src: str, dst: str) -> int:
    """分页读取旧集合（含 dense 向量），为新字段回填默认值后写入新集合。"""
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
            rows: list[dict[str, Any]] = []
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
                content_type = str(row.get("content_type") or "").strip().lower()
                row["block_type"] = "" if content_type == "image" else "text"
                row["code_language"] = ""
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
    parser = argparse.ArgumentParser(
        description="迁移 kura_ai_kb 到含 block_type/code_language 的 schema"
    )
    parser.add_argument("--purge-backup", action="store_true", help="迁移成功后删除旧集合备份")
    parser.add_argument("--dry-run", action="store_true", help="仅检查并打印现状，不执行迁移")
    args = parser.parse_args(argv)

    mgr = MilvusManager()
    client = mgr._get_client()
    name = mgr.collection_name
    backup = f"{name}{_BACKUP_SUFFIX}"
    tmp = f"{name}{_TMP_SUFFIX}"

    if not client.has_collection(name):
        print(f"集合 {name} 不存在，无需迁移（首次写入时会直接按新 schema 创建）。")
        return 0

    desc = client.describe_collection(name)
    if all(_has_field(desc, f) for f in _NEW_FIELDS):
        print(f"集合 {name} 已含 {'/'.join(_NEW_FIELDS)} 字段，跳过迁移。")
        return 0

    stats = client.get_collection_stats(name) or {}
    row_count = int(stats.get("row_count", 0) or 0)
    print(f"待迁移集合: {name} | 行数: {row_count}")
    print("迁移流程: 建临时新集合(含新字段) → 直读复制 dense 向量与标量(新字段回填默认值) → 换名切换 → 旧集合留备份")
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
        print(f"创建临时集合 {tmp}（含 block_type/code_language）...")
        mgr.init_collection(collection_name=tmp)
        if not client.has_collection(tmp):
            print(f"临时集合 {tmp} 创建失败，中止。")
            return 1

        copied = _copy_rows(client, name, tmp)
        print(f"拷贝完成: {copied} 行 -> {tmp}")
        client.flush(collection_name=tmp)

        client.rename_collection(old_name=name, new_name=backup)
        client.rename_collection(old_name=tmp, new_name=name)
        print(f"集合切换完成: {name}（新 schema），旧集合已备份为 {backup}")

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
