"""回填会话记忆的 episodic 向量（v1→v2 迁移后可选执行）。

v2 只在对话发生时惰性写入 episodic 段向量，因此迁移后存量会话要等到下一次对话才会补齐。
本脚本主动遍历所有会话，把 PG 里已有的压缩分段摘要一次性同步进向量库。

同时可选执行闲置 TTL 清理（--gc），把长期不活跃会话的向量回收掉。

用法：
    conda run -n Kura-AI python scripts/backfill_session_memory.py            # 全量回填
    conda run -n Kura-AI python scripts/backfill_session_memory.py --limit 50 # 只处理 50 个会话
    conda run -n Kura-AI python scripts/backfill_session_memory.py --gc       # 回填后执行闲置 TTL 清理
    conda run -n Kura-AI python scripts/backfill_session_memory.py --gc-only  # 只清理不回填
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _iter_sessions(limit: int):
    """按 updated_at 倒序遍历会话（最近活跃的优先回填）。"""
    from sqlalchemy import desc

    from app.chat.database import SessionLocal
    from app.chat.db_models import ChatSession as ChatSessionRow

    db = SessionLocal()
    try:
        q = db.query(
            ChatSessionRow.id,
            ChatSessionRow.user_id,
            ChatSessionRow.agent_id,
            ChatSessionRow.session_id,
        ).order_by(desc(ChatSessionRow.updated_at))
        if limit > 0:
            q = q.limit(limit)
        rows = q.all()
        return [(int(r.id), int(r.user_id), int(r.agent_id), str(r.session_id)) for r in rows]
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="回填会话记忆 episodic 向量 / 执行闲置 TTL 清理")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个会话（0=全部）")
    parser.add_argument("--gc", action="store_true", help="回填后执行闲置会话向量清理")
    parser.add_argument("--gc-only", action="store_true", help="只执行清理，不回填")
    args = parser.parse_args()

    from app.chat.database import init_chat_db

    init_chat_db()

    from app.chat.memory_archive import (
        archive_episodic_segments,
        enforce_rolling_cap,
        purge_stale_session_memory,
    )

    total = inserted = removed = failed = 0
    if not args.gc_only:
        sessions = _iter_sessions(args.limit)
        total = len(sessions)
        print(f"待回填会话数: {total}")
        for i, (_ref, uid, aid, sid) in enumerate(sessions, 1):
            try:
                info = archive_episodic_segments(uid, aid, sid)
                inserted += int(info.get("inserted") or 0)
                removed += int(info.get("removed") or 0)
                enforce_rolling_cap(uid, aid, sid)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  [{i}/{total}] 失败 u{uid} a{aid} {sid}: {e}", flush=True)
                continue
            if i % 50 == 0 or i == total:
                print(f"  进度 {i}/{total} | 新增向量 {inserted} | 清理失效向量 {removed}", flush=True)
        print(f"回填完成: 会话 {total} 个，新增向量 {inserted} 条，清理失效 {removed} 条，失败 {failed} 个")

    if args.gc or args.gc_only:
        from app.settings import settings

        days = int(getattr(settings, "CHAT_MEMORY_TTL_DAYS", 90) or 0)
        if days <= 0:
            print("CHAT_MEMORY_TTL_DAYS<=0，已关闭闲置 TTL 清理，跳过。")
        else:
            stat = purge_stale_session_memory(batch=2000)
            print(f"闲置 TTL 清理完成（>{days} 天未活跃）: 涉及会话 {stat.get('sessions', 0)} 个")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
