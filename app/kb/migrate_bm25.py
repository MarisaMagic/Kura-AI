"""知识库集合迁移：启用 Milvus 服务端 BM25 Function（幂等，可安全重跑）。

旧集合的稀疏检索基于进程内 BM25 词表（跨重启索引漂移、IDF 失真），
新 schema 用服务端 BM25 Function（bm25_fn）替代本地稀疏向量；
dense 向量直读复制，不重调 embedding API（零 API 费用）。

用法（在项目根目录、conda 环境「Kura-AI」下）：
    python -m app.kb.migrate_bm25                    # 迁移，保留旧集合备份（_bak_bm25）
    python -m app.kb.migrate_bm25 --purge-backup     # 迁移成功后删除备份
    python -m app.kb.migrate_bm25 --dry-run          # 只检查现状，不执行
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from pymilvus import MilvusClient

from app.kb.milvus_client import MilvusManager

# 拷贝时需要原样带回的字段（dense_embedding 为向量，其余为标量）
_COPY_SCALAR_FIELDS = [
    "kb_scope",
    "text",
    "filename",
    "file_type",
    "file_path",
    "page_number",
    "chunk_idx",
    "chunk_id",
    "parent_chunk_id",
    "root_chunk_id",
    "chunk_level",
    "content_type",
    "image_path",
    "position_start",
    "position_end",
    "image_position_x",
    "image_position_y",
    "image_width",
    "image_height",
]

_INT_FIELDS = {
    "page_number",
    "chunk_idx",
    "chunk_level",
    "position_start",
    "position_end",
    "image_position_x",
    "image_position_y",
    "image_width",
    "image_height",
}

_BATCH_SIZE = 1000


def _has_bm25_function(desc: dict) -> bool:
    """判断集合描述中是否已注册 BM25 Function。"""
    for fn in desc.get("functions") or []:
        if not isinstance(fn, dict):
            continue
        # type 可能是 FunctionType 枚举（str() 会变成数字），用 repr 取枚举名
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
    """分页读取旧集合（含 dense 向量），批量写入新集合；返回拷贝行数。"""
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
    parser = argparse.ArgumentParser(description="迁移 kura_ai_kb 到服务端 BM25 Function schema")
    parser.add_argument("--purge-backup", action="store_true", help="迁移成功后删除旧集合备份")
    parser.add_argument("--dry-run", action="store_true", help="仅检查并打印现状，不执行迁移")
    args = parser.parse_args(argv)

    mgr = MilvusManager()
    client = mgr._get_client()
    name = mgr.collection_name  # 如 kura_ai_kb
    backup = f"{name}_bak_legacy_sparse"
    tmp = f"{name}_bm25_v2"

    if not client.has_collection(name):
        print(f"集合 {name} 不存在，无需迁移（首次写入时会直接按含 BM25 Function 的新 schema 创建）。")
        return 0

    desc = client.describe_collection(name)
    if _has_bm25_function(desc):
        print(f"集合 {name} 已启用服务端 BM25 Function，跳过迁移。")
        return 0

    stats = client.get_collection_stats(name) or {}
    row_count = int(stats.get("row_count", 0) or 0)
    print(f"待迁移集合: {name} | 行数: {row_count}")
    print("迁移流程: 建临时新集合(含 BM25 Function) → 直读复制 dense 向量与标量 → 换名切换 → 旧集合保留为备份")
    if args.dry_run:
        print("--dry-run: 仅检查，未执行。")
        return 0

    # 1. 清理残留临时集合（上次异常中断留下）
    if client.has_collection(tmp):
        print(f"发现残留临时集合 {tmp}，先删除...")
        client.drop_collection(tmp)
    if client.has_collection(backup):
        print(f"发现已有备份集合 {backup}，删除以便重新备份...")
        client.drop_collection(backup)

    try:
        # 2. 用新 schema（含 bm25_fn Function）创建临时集合
        print(f"创建临时集合 {tmp}（含 BM25 Function）...")
        mgr.init_collection(collection_name=tmp)
        if not client.has_collection(tmp):
            print(f"临时集合 {tmp} 创建失败，中止。")
            return 1

        # 3. 直读复制旧数据（dense 向量不重算）
        copied = _copy_rows(client, name, tmp)
        print(f"拷贝完成: {copied} 行 -> {tmp}")
        client.flush(collection_name=tmp)

        # 4. 切换：旧集合改名为备份，临时集合改名为正式名
        client.rename_collection(old_name=name, new_name=backup)
        client.rename_collection(old_name=tmp, new_name=name)
        print(f"集合切换完成: {name}（新 schema），旧集合已备份为 {backup}")

        if args.purge_backup and client.has_collection(backup):
            client.drop_collection(backup)
            print(f"已按 --purge-backup 删除备份 {backup}")

        final_stats = client.get_collection_stats(name) or {}
        print(f"迁移成功: {name} 现含 {int(final_stats.get('row_count', 0) or 0)} 行。")
        print("后续: 运行检索 smoke test 验证混合检索（例如上传新文档后提问精确词）。")
        if not args.purge_backup:
            print(f"提示: 确认无误后可手动删除备份，MilvusClient(uri).drop_collection('{backup}')。")
        return 0
    except Exception:
        traceback.print_exc()
        # 失败回滚：删除残留临时集合；若已换名则尝试恢复
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