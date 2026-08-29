<p align="center">
  <a href="https://github.com/MarisaMagic/Kura-AI">
    <img alt="Kura AI Logo" width="200" src="https://github.com/mizhexiaoxiao/vue-fastapi-admin/blob/main/deploy/sample-picture/logo.svg">
  </a>
</p>

<h1 align="center">Kura AI</h1>

## 项目简介

基于 FastAPI + Vue 3 + LangChain + LangGraph 的多模态智能体中心。支持自定义智能体（模型、提示词、知识库、MCP 工具）、多轮对话、长短期记忆、多模态知识库与会话附件理解。智能体基于 ReAct 自主规划，按需调用知识库检索、联网搜索、会话记忆、附件读写及 MCP 外部工具完成任务。

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

---


## 本地启动项目

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

建议预先启动 docker 数据库服务

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

![](deploy\sample-picture\docker-compose-up.png)

停止所有数据库服务:

```sh
docker compose stop
```

停止数据库镜像服务成功输出:

![](deploy\sample-picture\docker-compose-stop.png)


---

### 环境变量配置

建议在项目根目录下创建 `.env` 环境变量配置（需根据自身情况更改）:

```bash
# 复制为 .env 后填写。SECRET_KEY 等敏感项勿提交仓库。

# ===== App security =====
SECRET_KEY=
# 本地开发可设为 true；生产务必 false。仅 DEBUG=true 时允许 Header token=dev 免 JWT
DEBUG=false
DEBUG_AGENT_KB_PROMPT=false
# 首次启动且库中无用户时创建的管理员（密码勿提交仓库；至少 8 位且含字母与数字）
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_EMAIL=admin@localhost
INITIAL_ADMIN_PASSWORD=
# 是否开放邮箱自助注册（本机开发可 true；公网务必 false）
ALLOW_PUBLIC_REGISTRATION=false
# 本机调试 MCP / 内网模型时才设 true
ALLOW_PRIVATE_UPSTREAM_URLS=false
# 生产设 false，关闭 /docs /redoc /openapi.json
DOCS_ENABLED=true
# uvicorn 监听；公网请保持 127.0.0.1 并放在反代后
UVICORN_HOST=127.0.0.1
# 公网建议 1440（1 天）
# JWT_ACCESS_TOKEN_EXPIRE_MINUTES=1440
# 仅在可信反向代理之后设 true
AUTH_TRUST_X_FORWARDED_FOR=false
# 生产建议单独设置 Fernet 密钥
# API_KEY_ENCRYPTION_KEY=
# CORS_ORIGINS=["http://localhost:3100","http://127.0.0.1:3100"]

# ===== Embedding Model (多模态) =====
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=qwen3-vl-embedding
EMBEDDING_DIM=1536
EMBEDDING_BATCH_SIZE=10

# ===== Milvus =====
MILVUS_HOST=127.0.0.1
MILVUS_PORT=19530
MILVUS_COLLECTION=kura_ai_kb

# ===== Database / Cache =====
DATABASE_URL=postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/langchain_app
REDIS_URL=redis://127.0.0.1:6379/0

CHAT_MEMORY_MILVUS_RECREATE_ON_INIT=false

PUBLIC_API_BASE=http://127.0.0.1:9999

# ===== Web Search（联网搜索）=====
# provider: auto / ddgs / bocha / bing_html
# auto：无代理且已配博查 Key 时 bocha → bing_html；有代理时 ddgs → bocha → bing_html
WEB_SEARCH_PROVIDER=auto
WEB_SEARCH_MAX_RESULTS=5
# ddgs 格式 {country}-{language}；旧值 zh-cn 会自动归一为 cn-zh
WEB_SEARCH_REGION=cn-zh
WEB_SEARCH_TIMEOUT_SECONDS=15
# 可选：ddgs / 博查 / bing_html 均使用，如 http://127.0.0.1:7890
# WEB_SEARCH_PROXY=
# 空=自动（无代理 bing / 有代理 auto）；也可强制指定，如 bing 或 google,brave,bing
# WEB_SEARCH_DDGS_BACKEND=
# 博查 Web Search（国内无代理主路径）。申请: https://open.bochaai.com/ → API KEY 管理
WEB_SEARCH_BOCHA_API_KEY=
WEB_SEARCH_BOCHA_ENDPOINT=https://api.bochaai.com/v1/web-search
```

### 公网部署清单

上线前请核对（本仓库默认面向本机开发）：

- `DEBUG=false`（否则 Header `token=dev` 可跳过 JWT）
- `ALLOW_PUBLIC_REGISTRATION=false`
- `DOCS_ENABLED=false`
- `ALLOW_PRIVATE_UPSTREAM_URLS=false`
- `UVICORN_HOST=127.0.0.1`，前面用 Nginx/Caddy 做 HTTPS 反代
- `AUTH_TRUST_X_FORWARDED_FOR` 仅在**可信**反代正确设置 `X-Forwarded-For` 后开启
- 生产单独配置 `API_KEY_ENCRYPTION_KEY`，不要只靠 `SECRET_KEY` 派生
- 公网可将 `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` 改为 `1440`
- `docker compose` 端口已绑定 `127.0.0.1`；**务必修改** Postgres / MinIO 默认口令，且不要把这些端口映射到公网
- Redis 不可用时，非 DEBUG 环境登录/注册会返回 503（fail-closed），请保证 Redis 可用
