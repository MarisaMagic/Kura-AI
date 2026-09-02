# 已弃用与过渡项

本文记录仍存在于代码或数据中、但不应再作为新实现依赖的能力。新增功能请走现行路径。

## 迁移体系（aerich）

aerich 已弃用。`migrations/` 为 SQLite 时代产物（含 `AUTOINCREMENT`，且被 `.gitignore` 忽略），在 PostgreSQL 上不可执行。

现行做法：启动时 [app/core/init_app.py](../app/core/init_app.py) 调用 `Tortoise.generate_schemas(safe=True)`，新列/新表用 `ensure_*` 的 `ALTER TABLE ... IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` 幂等补丁。

本次不为整库引入 Alembic。新列继续加幂等补丁；未来再正规化版本化迁移。

SQLite → PostgreSQL 一次性脚本见 [scripts/migrate_sqlite_to_pg.py](../scripts/migrate_sqlite_to_pg.py)：先启动后端让 Tortoise 建表，再跑脚本。

## 客户端 BM25（`fit_corpus`）

知识库稀疏检索已迁到 Milvus 服务端 BM25 Function（`bm25_fn`），见 [app/kb/milvus_client.py](../app/kb/milvus_client.py) 与 [app/kb/migrate_bm25.py](../app/kb/migrate_bm25.py)。

- [app/kb/embedding.py](../app/kb/embedding.py)：无引用，已删除。
- [app/kb/multimodal_embedding.py](../app/kb/multimodal_embedding.py) 内进程内词表 / `fit_corpus`：记忆集合迁服务端 BM25 后已删除。
- `apply_document_name_filter`：无调用点，已删除。选档文件名从 PostgreSQL `mg_kb_documents` 读取。

记忆集合迁移：`python -m app.chat.migrate_memory_bm25`。

## `UserAgent.enable_web`

联网能力改为对话页「联网搜索」开关（`use_web_search`）。字段保留仅为兼容历史行，读写忽略。

## localStorage `access_token`

登录态改为短时 access（内存 / sessionStorage）+ HttpOnly refresh cookie。旧 localStorage 中的 `access_token` 仅作一次性过渡：读到后写入 sessionStorage 并清除 localStorage。新登录不再写入 localStorage。

## Docker 默认口令

`docker-compose.yml` 不再内置弱口令。须在 `.env` 设置 `POSTGRES_PASSWORD`、`MINIO_APP_ROOT_PASSWORD`（见 `.env.example`）。已有 Postgres volume 不会因改环境变量而改库内密码，换密需在库内执行或重建 volume。

Milvus 内网 MinIO / etcd 鉴权不强制改造现有 volume；端口仅绑定 `127.0.0.1`。云上或已开鉴权的实例可设 `MILVUS_TOKEN`。
