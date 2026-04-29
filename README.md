<p align="center">
  <a href="https://github.com/MarisaMagic/Kura-AI">
    <img alt="Kura AI Logo" width="200" src="https://github.com/mizhexiaoxiao/vue-fastapi-admin/blob/main/deploy/sample-picture/logo.svg">
  </a>
</p>

<h1 align="center">Kura AI</h1>

## 项目简介

基于 FastAPI + Vue 3 + LangChain + LangGraph 的多模态智能体中心项目。支持用户自定义智能体配置、多轮对话、长短期记忆、RAG 多模态知识库检索、附件内容对话等功能。智能体能够基于 ReAct 模式自主思考、调用 Agent Tools、知识库检索、多模态内容理解解决用户问题。

使用项目模板: [vue-fastapi-admin](https://github.com/mizhexiaoxiao/vue-fastapi-admin)

---

## 核心功能

### 智能体中心

![](/deploy/sample-picture/agent-hub.png)

### 智能体对话

![](/deploy/sample-picture/agent-chat-1.png)

可进行多轮对话：

![](/deploy/sample-picture/agent-chat-2.png)

支持 latex、代码块渲染：

![](/deploy/sample-picture/agent-chat-3.png)

![](/deploy/sample-picture/agent-chat-4.png)

### 附件内容对话

![](/deploy/sample-picture/agent-attach-1.png)

可支持多模态大模型读取图片附件：

![](/deploy/sample-picture/agent-attach-2.png)

### 知识库检索

![](/deploy/sample-picture/agent-kb-1.png)

知识库配置：

![](/deploy/sample-picture/agent-kb-2.png)

### 历史对话列表

![](/deploy/sample-picture/agent-list.png)

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

```
# ===== Embedding Model (多模态) =====
EMBEDDING_API_KEY=[你的多模态向量嵌入模型 API Key]
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

# ===== KnowledgeBase Imgs URL Base =====
PUBLIC_API_BASE=http://127.0.0.1:9999
```
