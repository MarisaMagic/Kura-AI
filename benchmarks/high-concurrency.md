# Kura AI 高并发架构设计与实测

面向「单机多容器 + 百级并发对话 + 知识库批量上传」场景的一轮系统性并发治理（4 个阶段 + 2 期深度异步化），全部改动均附真实压测数据。

---

## 部署形态

Nginx 托管前端静态资源，并把 `/api/v1` 反代到 **4 个 backend 副本**（`upstream` + `server ... resolve` 动态解析 Docker DNS，`least_conn` 按活动连接数选副本，副本增减无需重载；SSE 事件存 Redis，订阅可落任意副本）；文档解析/向量化由独立的 **kb-worker 副本**消费 Redis 可靠队列，不与 API 争抢事件循环。

```
                      ┌────────────── Nginx (8088) ──────────────┐
浏览器 ──► 静态资源    │  /api/v1  ──►  upstream+least_conn(resolve) → backend ×4 │
                      └──────────────────┬───────────────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                ▼                                ▼
 backend ×4（FastAPI/uvicorn）      kb-worker ×2（可靠队列消费）      PostgreSQL / Redis / Milvus / MinIO
```

## 四项关键设计

**1. 事件循环解阻塞与统一并发闸门**（`app/utils/concurrency.py`）
- 所有同步 IO（DB/Redis/MinIO/Milvus）均不直接跑在事件循环上：早期统一 `run_sync` 移入线程池，深度异步化后热路径直接走异步客户端；
- 默认线程池显式扩容（96/副本），`loop lag` 监控（后台采样 P50/P99）暴露阻塞，`/api/v1/base/status` 可实时观测；
- LLM 并发闸门统一覆盖 `/chat`、`/chat/stream`、`/chat/jobs` 三个入口（`LLM_MAX_INFLIGHT` 按副本分摊），排队有事件反馈、超时快速失败。

**2. 上游配额保护与快速降级**（`app/utils/upstream_quota.py`，面向个人账号低配额）
- 跨进程 Redis Lua 闸门（并发 + QPS 双维度，API 副本与 kb-worker 共享），替代进程内信号量；
- 熔断器：连续 429 后短路冷却，期间调用直接降级；
- 快速降级链：rerank 限流 → 按向量分排序；embedding 熔断 → 检索返回「知识库暂时繁忙」、跳过查询扩展；`KB_EMBEDDING_YIELD`：批量上传在配额紧张时自动为前台检索让路；
- 查询向量 Redis 缓存：相同 query 零上游调用。

**3. 多副本安全**
- 队列：`BRPOPLPUSH` 可靠出队 + 任务级处理锁（重复投递幂等）+ 原子 ack（只删自己那条）+ stale 回收分布式锁；
- 会话锁/替换锁/限流均为 Redis 共享态，多副本语义一致；
- nginx 用 `upstream { server backend:9999 resolve; }` 运行时解析 Docker DNS（副本增减无需重载），`least_conn` 选活动连接最少的副本，`max_fails/fail_timeout` 做被动健康检查，`keepalive` 复用后端连接。

**4. 对话与检索链路全异步化（双轨设计）**
- 检索链：`AsyncMilvusClient`（真协程 gRPC）+ DashScope `AioMultiModalEmbedding`（aiohttp）+ `httpx.AsyncClient` rerank + 异步 RAG 子图（`complex` 策略下 step-back/HyDE **并发生成**）；
- 对话链：SQLAlchemy `AsyncSession`（psycopg async）承接全部消息读写，Redis 走 `redis.asyncio`；
- **双轨**：同步实现完整保留供 `/chat`、工具线程、实验与 worker 使用；引入 `sync_fallback` 兼容层——Windows Proactor 事件循环下 psycopg 异步不可用时自动退化线程池，生产 Linux 走真异步；
- 工具层按 Agent 路径分派：异步 Agent 注入 `coroutine=` 版知识库工具、同步路径沿用线程模型。

## 实测数据（本机 Docker，真实压测）

**① 对话链路**（mock LLM 零配额消耗；4 副本 × 闸门 25 = 100 并发活跃流）：

| 环境 | 并发 | 成功率 | P50 | loop lag P99 |
|---|---|---|---|---|
| Windows 单进程 | 100 | 100% | 4.4s | **184ms** |
| Linux 4 副本（异步化前）| 100 | 100% | 4.4s | **41ms** |
| Linux 4 副本（异步化后）| 100×2 轮 | 100% | 3.6s | **26ms** |

**② 线程占用**（20 并发，真实 DashScope/Milvus/Redis）：

| 路径 | 执行器线程增量 |
|---|---|
| 检索（同步链 to_thread） | **+20** |
| 检索（异步协程链） | **+0** |
| DB 写入（同步 to_thread） | **+20** |
| DB 写入（AsyncSession） | **+0** |

**③ 上游配额拐点**（真实 DashScope 个人账号，不同 query 探测）：

| 档位 | 桶配置 | 并发 | 结果 |
|---|---|---|---|
| A2 | 生产默认（2/2） | 30 | 14 成功 + 16 快速降级（贴 8s 上限） |
| B | 放开 QPS | 10 | 17 成功 + 3 次 rerank 429（~15%） |
| C | 放开 QPS | 30 | 20 成功 + 20 次 rerank 429（~50%） |
| **D** | **调优（emb 5/5、rerank 3/3、等待 5s）** | 30 | **25 成功 + 5 降级，零上游 429** |

结论：**rerank 为配额瓶颈**（有效容量约 10~20 QPS），embedding 配额较宽；调优参数已写入 `.env.example` 注释。

## 压测复现

- `tests/load/mock_llm_server.py`：零依赖 OpenAI 兼容 Mock LLM（流式，延迟可控），配合 `docker-compose.test.yml` 起容器版，压测不消耗上游配额；
- `tests/load/load_test.py`：对话阶梯并发压测（jobs/SSE 双路径 + 在线状态采样），用法见脚本头注释。
