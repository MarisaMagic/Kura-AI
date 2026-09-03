"""版本化 schema 补丁（取代旧散装 ensure_* 幂等补丁与已移除的 aerich）。

约定：
- 每个补丁有唯一编号名（如 ``001_user_token_version``）；成功应用后写入
  ``schema_patch_log`` 表，后续启动跳过，不重复执行。
- 补丁在 ``PATCHES`` 中按声明顺序执行；新补丁只能追加到末尾，
  不得改动既有条目的编号、语句或顺序（否则已入库的日志语义会漂移）。
- ``before_schemas=True`` 的补丁必须先于 ``Tortoise.generate_schemas`` 执行：
  generate_schemas 会为带 description 的新列生成 COMMENT ON COLUMN（PG 无
  IF EXISTS），旧库缺列时直接报错、事后补丁来不及兜底；全新库上表尚不存在，
  补丁内 ALTER 失败会被吞掉并跳过记录，由建表覆盖新列，下次启动重试后记录。
- 补丁仅在其全部语句成功时才记录为已应用；任一语句失败则下次启动重试
  （与旧 ensure_* 的失败行为一致，不会把真实故障掩盖成"已应用"）。
- 数据修正类补丁（如 ``100_*``）没有 IF NOT EXISTS 之类的天然幂等，仅靠日志
  去重：预置规则后续变化时请追加新编号（如 ``101_*``），不要修改既有条目。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from tortoise import Tortoise

from app.log import logger


@dataclasses.dataclass(frozen=True)
class SchemaPatch:
    name: str  # 唯一编号名，形如 "001_user_token_version"
    description: str
    statements: tuple[str, ...] = ()
    before_schemas: bool = False  # True: 须在 generate_schemas 之前执行
    statements_fn: Callable[[], list[str]] | None = None  # 运行时构造语句（数据修正类）


def _mcp_preset_confirm_policy_statements() -> list[str]:
    """已知只读 MCP 预置的存量 auto 配置收紧为 never（避免 Context7 等查询服务被误拦）。"""
    from app.mcp_client.presets import MCP_SERVER_PRESETS

    readonly_urls = [p["url"] for p in MCP_SERVER_PRESETS if p.get("confirm_policy") == "never"]
    if not readonly_urls:
        return []
    quoted = ", ".join("'" + u.replace("'", "''") + "'" for u in readonly_urls)
    return [
        "UPDATE user_agent_mcp_server SET confirm_policy = 'never'"
        f" WHERE confirm_policy = 'auto' AND url IN ({quoted})"
    ]


PATCHES: tuple[SchemaPatch, ...] = (
    SchemaPatch(
        name="001_user_token_version",
        description="user 表补 token_version 列（refresh 吊销）",
        statements=('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS "token_version" INT NOT NULL DEFAULT 0',),
        before_schemas=True,
    ),
    SchemaPatch(
        name="002_user_avatar",
        description="user 表补 avatar 列",
        statements=('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS "avatar" VARCHAR(255)',),
    ),
    SchemaPatch(
        name="003_user_agent_base_url",
        description="user_agent 表补 base_url 列",
        statements=('ALTER TABLE "user_agent" ADD COLUMN IF NOT EXISTS "base_url" VARCHAR(512)',),
    ),
    SchemaPatch(
        name="004_user_agent_supports_vision",
        description="user_agent 表补 supports_vision 列",
        statements=(
            'ALTER TABLE "user_agent" ADD COLUMN IF NOT EXISTS "supports_vision" BOOL NOT NULL DEFAULT FALSE',
        ),
    ),
    SchemaPatch(
        name="005_user_agent_is_published",
        description="user_agent 表补 is_published 列",
        statements=(
            'ALTER TABLE "user_agent" ADD COLUMN IF NOT EXISTS "is_published" BOOL NOT NULL DEFAULT FALSE',
        ),
    ),
    SchemaPatch(
        name="006_user_agent_share_table",
        description="补 user_agent_share 表及索引",
        statements=(
            '''CREATE TABLE IF NOT EXISTS "user_agent_share" (
    "id" BIGSERIAL PRIMARY KEY,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "agent_id" BIGINT NOT NULL REFERENCES "user_agent" ("id") ON DELETE CASCADE,
    "user_id" BIGINT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE
)''',
            'CREATE INDEX IF NOT EXISTS "idx_user_agent_share_agent" ON "user_agent_share" ("agent_id")',
            'CREATE INDEX IF NOT EXISTS "idx_user_agent_share_user" ON "user_agent_share" ("user_id")',
            'CREATE UNIQUE INDEX IF NOT EXISTS "uid_user_agent_share_agent_user"'
            ' ON "user_agent_share" ("agent_id", "user_id")',
        ),
    ),
    SchemaPatch(
        name="007_user_agent_mcp_confirm_policy",
        description="user_agent_mcp_server 表补 confirm_policy 列",
        statements=(
            "ALTER TABLE \"user_agent_mcp_server\" ADD COLUMN IF NOT EXISTS"
            " \"confirm_policy\" VARCHAR(16) NOT NULL DEFAULT 'auto'",
        ),
    ),
    SchemaPatch(
        name="008_user_agent_sub_llm",
        description="user_agent 表补子智能体（打杂模型）配置列",
        statements=(
            'ALTER TABLE "user_agent" ADD COLUMN IF NOT EXISTS "sub_model_name" VARCHAR(100)',
            'ALTER TABLE "user_agent" ADD COLUMN IF NOT EXISTS "sub_base_url" VARCHAR(512)',
            'ALTER TABLE "user_agent" ADD COLUMN IF NOT EXISTS "sub_api_key_ciphertext" TEXT',
        ),
        before_schemas=True,
    ),
    SchemaPatch(
        name="100_mcp_preset_confirm_policy",
        description="只读 MCP 预置的存量 auto 配置收紧为 never（数据修正）",
        statements_fn=_mcp_preset_confirm_policy_statements,
    ),
)

_LOG_TABLE_SQL = '''CREATE TABLE IF NOT EXISTS "schema_patch_log" (
    "name" VARCHAR(100) PRIMARY KEY,
    "description" TEXT NOT NULL DEFAULT '',
    "applied_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''


async def _applied_patch_names(conn) -> set[str]:
    rows = await conn.execute_query_dict('SELECT "name" FROM "schema_patch_log"')
    return {r["name"] for r in rows}


async def apply_schema_patches(*, before_schemas: bool) -> None:
    """按阶段应用版本化补丁：before_schemas=True 仅跑建表前补丁，否则跑其余全部。"""
    conn = Tortoise.get_connection("default")
    await conn.execute_query(_LOG_TABLE_SQL)
    applied = await _applied_patch_names(conn)

    for patch in PATCHES:
        if patch.before_schemas != before_schemas:
            continue
        if patch.name in applied:
            continue
        statements = list(patch.statements_fn()) if patch.statements_fn else list(patch.statements)
        if not statements:
            continue
        failed = False
        for sql in statements:
            try:
                await conn.execute_query(sql)
            except Exception as e:
                failed = True
                logger.warning("schema 补丁 {} 执行失败（下次启动重试）: {}", patch.name, e)
        if failed:
            continue
        try:
            await conn.execute_query(
                'INSERT INTO "schema_patch_log" ("name", "description") VALUES ($1, $2)',
                [patch.name, patch.description],
            )
        except Exception as e:
            logger.warning("schema 补丁 {} 已执行但日志写入失败（下次启动可能重放）: {}", patch.name, e)
            continue
        logger.info("schema 补丁已应用: {}（{}）", patch.name, patch.description)
