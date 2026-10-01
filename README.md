<p align="center">
  <a href="https://github.com/MarisaMagic/Kura-AI">
    <img alt="Kura AI Logo" width="200" src="deploy/sample-picture/logo.svg">
  </a>
</p>

<h1 align="center">Kura AI</h1>

## 项目简介

基于 FastAPI + Vue 3 + LangChain + LangGraph 的知识库对话智能体平台。支持自定义智能体（模型、提示词、知识库、MCP 工具）、多轮对话、长短期记忆、多模态知识库与会话附件理解。智能体基于 Tool-Use-Loop 自主规划，按需调用知识库检索、联网搜索、会话记忆、附件读写及 MCP 外部工具完成任务。

使用项目模板: [vue-fastapi-admin](https://github.com/mizhexiaoxiao/vue-fastapi-admin)

---

## 核心功能

### 智能体中心

![](/deploy/sample-picture/agent-hub.png)

### 智能体对话

![](/deploy/sample-picture/agent-chat-1.png)

![](/deploy/sample-picture/agent-chat-3.png)

![](/deploy/sample-picture/agent-chat-4.png)

### 知识库检索

![](/deploy/sample-picture/agent-kb-1.png)

![](/deploy/sample-picture/agent-kb-2.png)

![](/deploy/sample-picture/agent-kb-3.png)

### 历史对话列表

![](/deploy/sample-picture/agent-list.png)

### 联网搜索

![](/deploy/sample-picture/agent-web-search.png)

![](/deploy/sample-picture/agent-web-search-2.png)

### MCP 外部工具

![](/deploy/sample-picture/agent-mcp-1.png)

![](/deploy/sample-picture/agent-mcp-2.png)

### 附件内容对话

![](/deploy/sample-picture/agent-attach-1.png)

![](/deploy/sample-picture/agent-attach-2.png)

### 智能体共享

![](/deploy/sample-picture/agent-share-1.png)

![](/deploy/sample-picture/agent-share-2.png)

### RAG 实验平台

![](/deploy/sample-picture/rag-test-1.png)

![](/deploy/sample-picture/rag-test-2.png)

![](/deploy/sample-picture/rag-test-3.png)

![](/deploy/sample-picture/rag-test-4.png)

### 暗色主题切换

![](/deploy/sample-picture/agent-darkmode.png)

![](/deploy/sample-picture/agent-darkmode-1.png)

![](/deploy/sample-picture/agent-darkmode-2.png)

---


## 部署方式 1: 本地启动项目（适用于开发者）

### 后端

启动项目需要以下环境：
- Python 3.11

1. 创建虚拟环境

```sh
python3 -m venv venv
```

或者使用 conda 创建虚拟环境（需要提前配置好 [Anaconda](https://www.anaconda.com/download)）:

```sh
conda create -n Kura-AI python=3.11 -y
```

2. 激活虚拟环境

```sh
source venv/bin/activate  # Linux/Mac
# 或
.\venv\Scripts\activate  # Windows
```

如果使用的是 conda 虚拟环境:

```sh
conda activate Kura-AI
```

3. 安装依赖

```sh
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

4. 启动后端服务

建议预先启动 docker [数据库服务](#数据库服务)

```sh
python run.py
```

后端启动成功输出: 

```sh
2026-04-29 16:27:48 - INFO - Will watch for changes in these directories: [项目路径]
2026-04-29 16:27:48 - INFO - Uvicorn running on http://0.0.0.0:9999 (Press CTRL+C to quit)
2026-04-29 16:27:48 - INFO - Started reloader process [32712] using WatchFiles
2026-04-29 16:27:52 - INFO - Started server process [25192]
2026-04-29 16:27:52 - INFO - Waiting for application startup.
2026-04-29 16:27:52 - INFO - Application startup complete.
```

访问 http://localhost:9999/docs 可查看API文档

---

### 前端

启动项目前端环境建议：
- node v18.8.0+

1. 进入前端目录

```sh
cd web
```

2. 安装依赖 

建议使用 pnpm: https://pnpm.io/zh/installation

```sh
npm i -g pnpm # 已安装可忽略
pnpm i # 或者 npm i
```

3. 启动

```sh
pnpm dev
```

前端启动成功输出：

```sh
VITE v5.4.21  ready in 20287 ms

➜  Local:   http://localhost:3100/                                                                                 
➜  Network: http://169.254.128.15:3100/                                                                               
➜  Network: http://169.254.47.198:3100/                                                                               
➜  Network: http://192.168.32.245:3100/                                                                               
➜  Network: http://172.17.128.1:3100/                                                                                 
➜  press h + enter to show help   
```

---

### 数据库服务

使用 docker 部署 PostgreSQL、Redis、Milvus 镜像服务（需要先安装 [Docker](https://www.docker.com/products/docker-desktop/)）。

docker 配置脚本: `docker-compose.yml`

```sh
# 读取当前目录下的 docker-compose.yml 文件，并启动其中定义的所有服务。
# 如果本地没有数据库镜像文件会先拉取镜像
docker compose up -d
```

启动数据库镜像服务成功输出:

![](deploy/sample-picture/docker-compose-up.png)

停止所有数据库服务:

```sh
docker compose stop
```

停止数据库镜像服务成功输出:

![](deploy/sample-picture/docker-compose-stop.png)

二次开发：用上面的命令只起数据库，再分别 `python run.py` 与 `pnpm dev`。若要把前后端也打进镜像、一条命令访问网页，见下方「快速一键部署」。

---

### 环境变量配置

将仓库根目录的 `.env.example` 复制为 `.env`，再填写密钥（填好的 `.env` 勿提交仓库）：

```sh
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

至少填写：

- `SECRET_KEY`（生成：`openssl rand -hex 32`）
- `INITIAL_ADMIN_PASSWORD`（至少 8 位，含字母与数字）
- `EMBEDDING_API_KEY`
- 启用知识库重排时再填 `RERANK_API_KEY`
- 国内联网搜索可填 `WEB_SEARCH_BOCHA_API_KEY`（默认同时启用博查 Semantic Reranker）

完整项与注释见 `.env.example`。公网上线请再对照下方「公网部署清单」。

---

## 部署方式2: 一键快速部署（Docker）

### 运行命令

前置：已安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，并已按上一节准备好根目录 `.env`（至少填写 `SECRET_KEY`、`INITIAL_ADMIN_PASSWORD`、`EMBEDDING_API_KEY`）。

在项目根目录运行命令：

```sh
# 首次或代码有变更时加 --build
docker compose -f docker-compose.prod.yml up -d --build
```

启动成功：

![](deploy/sample-picture/docker-compose-prod.png)

启动成功后浏览器打开 **http://localhost:8088** 即可访问（端口可用 `.env` 里的 `WEB_PORT` 更改）。

---

### 常用命令与说明

一键快速部署通过把 Vue 打成静态文件由 Nginx 托管，FastAPI 单独一个容器；Nginx 将 `/api/v1` 反代到后端（和本地 `pnpm dev` 的 Vite 代理同一思路）。数据库仍用现有 `docker-compose.yml`。

首次构建会拉 Python / Node / Milvus 等镜像，并执行 `pnpm build` 与 `pip install`，可能需要十几分钟。之后再启动会快很多。

常用命令：

```sh
# 查看状态
docker compose -f docker-compose.prod.yml ps

# 只停前后端，数据库继续跑（方便切回本地 python / pnpm 开发）
docker compose -f docker-compose.prod.yml stop backend frontend

# 停止全部（含数据库）
docker compose -f docker-compose.prod.yml stop

# 看后端日志
docker compose -f docker-compose.prod.yml logs -f backend
```

说明：

- 已在跑 `docker compose up -d`（仅数据库）时，再执行上面的 prod 命令只会补起 `backend` / `frontend`，数据目录共用 `volumes/`。
- 容器内会覆盖 `.env` 里的本机地址：`DATABASE_URL` / `REDIS_URL` / `MILVUS_HOST` 改为 Docker 服务名，`UVICORN_HOST=0.0.0.0`。本机开发不受影响。
- 后端不对外暴露 9999；浏览器只访问 Nginx 的 `WEB_PORT`。
- 公网请把 `PROD_PUBLIC_API_BASE` 设为站点根地址（如 `https://your.domain`），并继续核对下面的清单。

相关文件：`deploy/Dockerfile.backend`、`deploy/Dockerfile.frontend`、`deploy/nginx.conf`、`docker-compose.prod.yml`。

数据库结构/数据补丁：启动时自动应用版本化补丁（见 [`app/core/schema_patches.py`](app/core/schema_patches.py)，按 `schema_patch_log` 表去重）。

---

## 高并发架构设计与实测

面向「单机多容器 + 百级并发对话 + 知识库批量上传」场景的一轮系统性并发治理（4 个阶段 + 2 期深度异步化），全部改动均附真实压测数据。

### 部署形态

Nginx 托管前端静态资源，并把 `/api/v1` 反代到 **4 个 backend 副本**（运行时 DNS 轮询实现负载均衡，SSE 事件存 Redis，订阅可落任意副本）；文档解析/向量化由独立的 **kb-worker 副本**消费 Redis 可靠队列，不与 API 争抢事件循环。

```
                      ┌────────────── Nginx (8088) ──────────────┐
浏览器 ──► 静态资源    │  /api/v1  ──►  运行时 DNS 轮询 → backend ×4 │
                      └──────────────────┬───────────────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                ▼                                ▼
 backend ×4（FastAPI/uvicorn）      kb-worker ×2（可靠队列消费）      PostgreSQL / Redis / Milvus / MinIO
```

### 四项关键设计

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
- nginx 以变量 `proxy_pass` 强制运行时解析 Docker DNS，副本增减无需重载。

**4. 对话与检索链路全异步化（双轨设计）**
- 检索链：`AsyncMilvusClient`（真协程 gRPC）+ DashScope `AioMultiModalEmbedding`（aiohttp）+ `httpx.AsyncClient` rerank + 异步 RAG 子图（`complex` 策略下 step-back/HyDE **并发生成**）；
- 对话链：SQLAlchemy `AsyncSession`（psycopg async）承接全部消息读写，Redis 走 `redis.asyncio`；
- **双轨**：同步实现完整保留供 `/chat`、工具线程、实验与 worker 使用；引入 `sync_fallback` 兼容层——Windows Proactor 事件循环下 psycopg 异步不可用时自动退化线程池，生产 Linux 走真异步；
- 工具层按 Agent 路径分派：异步 Agent 注入 `coroutine=` 版知识库工具、同步路径沿用线程模型。

### 实测数据（本机 Docker，真实压测）

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

### 压测复现

- `tests/load/mock_llm_server.py`：零依赖 OpenAI 兼容 Mock LLM（流式，延迟可控），配合 `docker-compose.test.yml` 起容器版，压测不消耗上游配额；
- `tests/load/load_test.py`：对话阶梯并发压测（jobs/SSE 双路径 + 在线状态采样），用法见脚本头注释。

---

## 公网部署清单

上线前请核对（本仓库默认面向本机开发）：

- `DEBUG=false`（否则 Header `token=dev` 可跳过 JWT）
- `ALLOW_PUBLIC_REGISTRATION=false`
- `DOCS_ENABLED=false`
- `ALLOW_PRIVATE_UPSTREAM_URLS=false`
- `UVICORN_HOST=127.0.0.1`，前面用 Nginx/Caddy 做 HTTPS 反代（`docker-compose.prod.yml` 已在容器内用 Nginx 反代，且不把 9999 映射到宿主机）
- `AUTH_TRUST_X_FORWARDED_FOR` 仅在**可信**反代之后开启；nginx 须覆盖（而非追加）`X-Forwarded-For`
- 生产单独配置 `API_KEY_ENCRYPTION_KEY`，不要只靠 `SECRET_KEY` 派生
- Access JWT 默认 15 分钟；刷新令牌为 HttpOnly cookie（见 `.env.example`）
- `.env` 必须设置 `POSTGRES_PASSWORD`、`MINIO_APP_ROOT_PASSWORD`；compose 不再内置弱口令
- `docker compose` 端口已绑定 `127.0.0.1`；不要把数据库/对象存储端口映射到公网
- 可选：`MILVUS_TOKEN`（已开鉴权的 Milvus / Zilliz）
- Redis 不可用时，非 DEBUG 环境登录/注册会返回 503（fail-closed），请保证 Redis 可用
