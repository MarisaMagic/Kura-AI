"""一次性删除已弃用的会话记忆 Milvus 集合（kura_ai_chat_memory）。

会话记忆已改为 PostgreSQL 用户长期记忆（见 app/chat/user_memory.py），
该集合不再被任何代码读写。确认不再需要后执行：

    python scripts/drop_chat_memory_collection.py            # 仅检查是否存在
    python scripts/drop_chat_memory_collection.py --yes      # 执行删除

注意：只删会话记忆集合，不影响知识库集合（kura_ai_kb）。
"""

from __future__ import annotations

import argparse
import sys


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="删除已弃用的会话记忆 Milvus 集合")
    p.add_argument(
        "--collection",
        default="kura_ai_chat_memory",
        help="集合名（默认 kura_ai_chat_memory）",
    )
    p.add_argument("--yes", action="store_true", help="确认执行删除（缺省仅检查）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    name = str(args.collection or "").strip()
    if not name:
        print("集合名不能为空", file=sys.stderr)
        return 2

    from pymilvus import MilvusClient

    from app.kb.milvus_client import milvus_client_kwargs

    client = MilvusClient(**milvus_client_kwargs())
    try:
        exists = client.has_collection(name)
    except Exception as e:  # noqa: BLE001
        print(f"连接 Milvus / 查询集合失败：{e}", file=sys.stderr)
        return 1

    if not exists:
        print(f"集合不存在，无需处理：{name}")
        return 0

    if not args.yes:
        print(f"集合存在：{name}（未删除；确认后加 --yes 执行）")
        return 0

    client.drop_collection(name)
    print(f"已删除集合：{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
