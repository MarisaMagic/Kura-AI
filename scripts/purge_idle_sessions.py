"""清理闲置会话（硬删，默认 dry-run）。

会话原文 append-only 永久保存在 PostgreSQL，长期会累积。本脚本按会话 updated_at
删除超过阈值天数的闲置会话：级联删除消息与分段摘要，并清理对象存储附件与相关缓存。

用法：
    python scripts/purge_idle_sessions.py                 # dry-run（默认，只统计）
    python scripts/purge_idle_sessions.py --days 90       # 指定阈值天数
    python scripts/purge_idle_sessions.py --yes           # 实际删除
    python scripts/purge_idle_sessions.py --days 90 --batch 500 --yes

默认关闭：CHAT_SESSION_RETENTION_DAYS=0 时（且未传 --days）脚本直接退出。
警告：删除不可恢复，请先确认/备份。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="清理闲置会话（硬删）")
    p.add_argument("--days", type=int, default=None, help="闲置天数阈值（缺省读 CHAT_SESSION_RETENTION_DAYS）")
    p.add_argument("--batch", type=int, default=None, help="单次最多处理会话数（缺省读 CHAT_SESSION_RETENTION_BATCH）")
    p.add_argument("--dry-run", action="store_true", help="只统计不删除（未传 --yes 时默认如此）")
    p.add_argument("--yes", action="store_true", help="确认执行删除")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    from app.settings import settings

    days = args.days if args.days is not None else int(getattr(settings, "CHAT_SESSION_RETENTION_DAYS", 0) or 0)
    if days <= 0:
        print("未启用：CHAT_SESSION_RETENTION_DAYS=0（可传 --days N 指定阈值）。")
        return 0
    batch = args.batch if args.batch is not None else int(getattr(settings, "CHAT_SESSION_RETENTION_BATCH", 200) or 200)
    dry = args.dry_run or not args.yes

    from app.chat.database import init_chat_db
    from app.chat.storage import storage

    init_chat_db()
    cutoff = datetime.utcnow() - timedelta(days=days)
    print(f"{'[dry-run] ' if dry else ''}删除 updated_at < {cutoff.isoformat()} 的会话（batch={batch}）…")
    res = storage.purge_idle_sessions(cutoff, batch=batch, dry_run=dry)
    print(f"扫描 {res['scanned']} 个，删除 {res['deleted']} 个。")
    for sid in res["sessions"][:50]:
        print(f"  - {sid}")
    if dry:
        print("（dry-run，未删除；确认后加 --yes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
