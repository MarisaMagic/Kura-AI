"""一次性迁移：管理端 SQLite (db.sqlite3) -> PostgreSQL，保留原主键 ID。

前置：先启动一次后端（`python run.py` 或 Docker 后端容器），让 aerich 在目标库建好表结构。

用法（开发机，数据库服务已由 docker compose 拉起）：
    python scripts/migrate_sqlite_to_pg.py --source db.sqlite3
    python scripts/migrate_sqlite_to_pg.py --source db.sqlite3 --target postgres://user:pass@host:5432/langchain_app

--target 缺省时读取项目根目录 .env 的 ADMIN_DATABASE_URL / DATABASE_URL。
目标表非空时默认报错退出；加 --force 会先清空对应表再导入。

迁移前请备份：copy db.sqlite3 db.sqlite3.bak
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

# 与 settings.timezone 一致：SQLite 中 naive 时间戳为本地墙钟时间
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# 按外键依赖顺序排列；aerich / sqlite_sequence 不迁移
TABLES = [
    "dept",
    "deptclosure",
    "user",
    "role",
    "menu",
    "api",
    "auditlog",
    "user_agent",
    "user_agent_mcp_server",
    "user_agent_share",
    "user_agent_recent",
    "user_role",
    "role_menu",
    "role_api",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def normalize_pg_dsn(raw: str) -> str:
    for prefix in (
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgresql+asyncpg://",
        "postgres://",
        "postgresql://",
    ):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    raise SystemExit(f"无法识别的 PostgreSQL 连接串: {raw!r}")


def default_target() -> str:
    env_file = PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    raw = values.get("ADMIN_DATABASE_URL") or values.get("DATABASE_URL") or ""
    if not raw:
        raise SystemExit("未提供 --target，且 .env 中没有 ADMIN_DATABASE_URL / DATABASE_URL")
    return raw


def parse_dt(value) -> datetime | None:
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    # 目标列为 timestamptz：aware 值直接保留（绝对时刻正确）；
    # naive 值是本地墙钟时间，附加本地时区使绝对时刻不变
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=LOCAL_TZ)


async def migrate(source: str, target: str, force: bool) -> None:
    src = sqlite3.connect(source)
    conn = await asyncpg.connect(normalize_pg_dsn(target))
    try:
        type_rows = await conn.fetch(
            "SELECT table_name, column_name, udt_name "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
        col_types = {(r["table_name"], r["column_name"]): r["udt_name"] for r in type_rows}
        pg_tables = {t for (t, _) in col_types}

        missing = [t for t in TABLES if t not in pg_tables]
        if missing:
            raise SystemExit(
                f"目标库缺少表 {missing}；请先启动一次后端完成建表，再执行迁移。"
            )

        async with conn.transaction():
            for table in TABLES:
                src_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
                if not src_cols:
                    print(f"[skip] {table}: 源库无此表")
                    continue
                cols = [c for c in src_cols if (table, c) in col_types]
                dropped = [c for c in src_cols if (table, c) not in col_types]
                if dropped:
                    print(f"[info] {table}: 忽略源库多余列 {dropped}")

                existing = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
                if existing:
                    if not force:
                        raise SystemExit(
                            f"目标表 {table} 已有 {existing} 行；确认覆盖请加 --force（会先清空该表）"
                        )
                    await conn.execute(f'TRUNCATE TABLE "{table}" CASCADE')
                    print(f"[force] {table}: 已清空 {existing} 行")

                quoted = ", ".join(f'"{c}"' for c in cols)
                rows = src.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
                records = []
                for row in rows:
                    rec = []
                    for c, v in zip(cols, row):
                        udt = col_types[(table, c)]
                        if v is None:
                            rec.append(None)
                        elif udt == "bool":
                            rec.append(bool(v))
                        elif udt in ("timestamp", "timestamptz"):
                            rec.append(parse_dt(v))
                        elif udt in ("json", "jsonb") and not isinstance(v, str):
                            rec.append(str(v))
                        else:
                            rec.append(v)
                    records.append(tuple(rec))

                if records:
                    await conn.copy_records_to_table(table, records=records, columns=cols)
                if "id" in cols:
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f'COALESCE((SELECT MAX(id) FROM "{table}"), 1))'
                    )
                print(f"[ok] {table}: {len(records)} 行")

        # 校验：行数一致
        mismatch = []
        for table in TABLES:
            src_cnt = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            dst_cnt = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
            if src_cnt != dst_cnt:
                mismatch.append((table, src_cnt, dst_cnt))
        if mismatch:
            raise SystemExit(f"行数校验失败: {mismatch}")
        print("[ok] 各表行数一致")

        # 校验：管理端外键无孤儿
        fk_checks = [
            ("user_agent", "user_id", "user"),
            ("user_agent_mcp_server", "agent_id", "user_agent"),
            ("user_agent_share", "agent_id", "user_agent"),
            ("user_agent_recent", "agent_id", "user_agent"),
            ("user_role", "user_id", "user"),
            ("role_menu", "menu_id", "menu"),
            ("role_api", "api_id", "api"),
        ]
        for table, col, ref in fk_checks:
            orphans = await conn.fetchval(
                f'SELECT COUNT(*) FROM "{table}" t '
                f'WHERE NOT EXISTS (SELECT 1 FROM "{ref}" r WHERE r.id = t."{col}")'
            )
            if orphans:
                raise SystemExit(f"外键校验失败: {table}.{col} 有 {orphans} 条孤儿记录")
        print("[ok] 管理端外键完整")

        # 校验：聊天库（mg_*，若同库）引用的 user/agent 存在
        for mg_table in ("mg_chat_sessions", "mg_chat_attachments"):
            if mg_table not in pg_tables:
                continue
            for col, ref in (("user_id", "user"), ("agent_id", "user_agent")):
                orphans = await conn.fetchval(
                    f'SELECT COUNT(*) FROM "{mg_table}" t '
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{ref}" r WHERE r.id = t."{col}")'
                )
                if orphans:
                    raise SystemExit(
                        f"聊天库校验失败: {mg_table}.{col} 有 {orphans} 条记录指向不存在的 {ref}"
                    )
        print("[ok] 聊天库引用完整（user_id / agent_id 均可对上）")
    finally:
        src.close()
        await conn.close()

    print("迁移完成。建议重启后端后验证登录、智能体列表与历史会话。")


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite -> PostgreSQL 一次性迁移（保留原 ID）")
    ap.add_argument("--source", required=True, help="源 SQLite 文件路径，如 db.sqlite3")
    ap.add_argument("--target", help="目标 PG DSN；缺省读 .env 的 ADMIN_DATABASE_URL / DATABASE_URL")
    ap.add_argument("--force", action="store_true", help="目标表非空时先清空再导入")
    args = ap.parse_args()

    if not Path(args.source).exists():
        raise SystemExit(f"源文件不存在: {args.source}")
    target = args.target or default_target()
    asyncio.run(migrate(args.source, target, args.force))


if __name__ == "__main__":
    sys.exit(main())
