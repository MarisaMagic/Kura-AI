"""知识库集合迁移：BM25 分词器切换为内置 chinese analyzer（jieba 分词）。

旧集合的 text 字段使用 standard analyzer（中文按单字切分），稀疏腿召回质量差；
新 schema 以 analyzer_params={"type": "chinese"} 建集合（jieba + cnalphanumonly）。
analyzer 属于集合 schema，无法原地修改：本脚本建新集合 → 直读复制 dense 向量与
标量（含 text，写入时服务端按新 analyzer 重算 bm25_sparse）→ 换名切换，旧集合留备份。

前置：Milvus 服务端 >= 2.6（docker-compose.yml 已升级）；集合已含 BM25 Function
（否则先跑 python -m app.kb.migrate_bm25）。

用法（在项目根目录、conda 环境「Kura-AI」下）：
    python -m app.kb.migrate_analyzer                    # 迁移，保留旧集合备份（_bak_std_analyzer）
    python -m app.kb.migrate_analyzer --purge-backup     # 迁移成功后删除备份
    python -m app.kb.migrate_analyzer --dry-run          # 只检查现状，不执行
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from app.kb.migrate_bm25 import _copy_rows, _has_bm25_function
from app.kb.milvus_client import MilvusManager

_BACKUP_SUFFIX = "_bak_std_analyzer"
_TMP_SUFFIX = "_zh_analyzer_v2"


def _text_analyzer_params(desc: dict) -> dict | None:
    """取集合 text 字段的 analyzer_params；无则返回 None。"""
    for field in desc.get("fields") or []:
        if not isinstance(field, dict) or field.get("name") != "text":
            continue
        params = field.get("params") or {}
        ap = params.get("analyzer_params")
        return ap if isinstance(ap, dict) else None
    return None


def _is_chinese_analyzer(analyzer_params: dict | None) -> bool:
    """判断 analyzer_params 是否已为中文分词（内置 chinese 或 jieba 分词器）。"""
    if not analyzer_params:
        return False
    blob = json.dumps(analyzer_params, ensure_ascii=False).lower()
    return '"type": "chinese"' in blob or "jieba" in blob


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移 kura_ai_kb 到 chinese analyzer（jieba 分词）schema")
    parser.add_argument("--purge-backup", action="store_true", help="迁移成功后删除旧集合备份")
    parser.add_argument("--dry-run", action="store_true", help="仅检查并打印现状，不执行迁移")
    args = parser.parse_args(argv)

    mgr = MilvusManager()
    client = mgr._get_client()
    name = mgr.collection_name
    backup = f"{name}{_BACKUP_SUFFIX}"
    tmp = f"{name}{_TMP_SUFFIX}"

    if not client.has_collection(name):
        print(f"集合 {name} 不存在，无需迁移（首次写入时会直接按 chinese analyzer 新 schema 创建）。")
        return 0

    desc = client.describe_collection(name)
    if not _has_bm25_function(desc):
        print(f"集合 {name} 尚未启用服务端 BM25 Function，请先运行: python -m app.kb.migrate_bm25")
        return 1

    ap = _text_analyzer_params(desc)
    if _is_chinese_analyzer(ap):
        print(f"集合 {name} 的 text 字段已使用中文分词 analyzer，跳过迁移。")
        return 0

    stats = client.get_collection_stats(name) or {}
    row_count = int(stats.get("row_count", 0) or 0)
    print(f"待迁移集合: {name} | 行数: {row_count} | 当前 analyzer_params: {ap or '（默认 standard）'}")
    print("迁移流程: 建临时新集合(chinese analyzer) → 直读复制 dense 向量与标量(稀疏腿服务端重算) → 换名切换 → 旧集合留备份")
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
        print(f"创建临时集合 {tmp}（chinese analyzer）...")
        mgr.init_collection(collection_name=tmp)
        if not client.has_collection(tmp):
            print(f"临时集合 {tmp} 创建失败，中止。")
            return 1

        copied = _copy_rows(client, name, tmp)
        print(f"拷贝完成: {copied} 行 -> {tmp}")
        client.flush(collection_name=tmp)

        client.rename_collection(old_name=name, new_name=backup)
        client.rename_collection(old_name=tmp, new_name=name)
        print(f"集合切换完成: {name}（chinese analyzer），旧集合已备份为 {backup}")

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
