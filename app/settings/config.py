import os
import typing

from pydantic_settings import BaseSettings, SettingsConfigDict


def _tortoise_pg_url(raw: str) -> str:
    """把 SQLAlchemy 风格连接串归一为 Tortoise/asyncpg 的 postgres:// URL。"""
    for prefix in (
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgresql+asyncpg://",
        "postgresql://",
        "postgres://",
    ):
        if raw.startswith(prefix):
            return "postgres://" + raw[len(prefix) :]
    raise ValueError(f"无法识别的 PostgreSQL 连接串: {raw!r}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    VERSION: str = "0.1.0"
    APP_TITLE: str = "Kura AI"
    PROJECT_NAME: str = "Kura AI"
    APP_DESCRIPTION: str = "Description"

    # 公网部署请改为实际前端域名，或通过环境变量覆盖（JSON 数组，如 ["https://app.example.com"]）
    CORS_ORIGINS: typing.List[str] = [
        "http://localhost:3100",
        "http://127.0.0.1:3100",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    CORS_ALLOW_CREDENTIALS: bool = False
    CORS_ALLOW_METHODS: typing.List[str] = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    CORS_ALLOW_HEADERS: typing.List[str] = ["*"]

    # 生产请保持 False；仅本地调试用 True（会启用 dev 令牌等开发行为）
    DEBUG: bool = False
    # 为 True 时在服务端终端打印完整 LLM 输入消息 / 知识库工具原文（易泄露隐私与密钥，默认关闭）
    DEBUG_AGENT_KB_PROMPT: bool = False

    PROJECT_ROOT: str = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    BASE_DIR: str = os.path.abspath(os.path.join(PROJECT_ROOT, os.pardir))
    LOGS_ROOT: str = os.path.join(BASE_DIR, "app/logs")
    # 用户头像在对象存储 bucket 内的 key 前缀（对象存储改造前为本地磁盘目录，语义已变更）
    USER_AVATAR_ROOT: str = "user_avatar"
    # 浏览器访问路径前缀（与签名媒体路由一致）
    USER_AVATAR_URL_PREFIX: str = "/api/v1/media/user_avatar"
    # 智能体头像在对象存储 bucket 内的 key 前缀
    USER_AGENT_AVATAR_ROOT: str = "user_agents_avatar"
    USER_AGENT_AVATAR_URL_PREFIX: str = "/api/v1/media/user_agents_avatar"
    # 须在项目根目录 .env 中设置；勿提交仓库。生成: openssl rand -hex 32
    SECRET_KEY: str
    # 首次启动且库中无用户时创建的管理员（密码勿提交仓库；至少 8 位且含字母与数字）
    INITIAL_ADMIN_USERNAME: str = "admin"
    INITIAL_ADMIN_EMAIL: str = "admin@example.com"
    INITIAL_ADMIN_PASSWORD: typing.Optional[str] = None
    # 是否开放邮箱自助注册（公网务必 false；本机开发可在 .env 设 true）
    ALLOW_PUBLIC_REGISTRATION: bool = False
    # 为 True 时允许智能体 base_url / MCP url 指向内网或本机（仅本地调试 MCP）
    ALLOW_PRIVATE_UPSTREAM_URLS: bool = False
    # 用户可配置上游出站默认钉死校验解析所得 IP；仅排障时临时关闭
    EGRESS_PIN_DNS: bool = True
    # uvicorn 监听地址；公网请放在反代后并保持 127.0.0.1
    UVICORN_HOST: str = "127.0.0.1"
    # 为 False 时关闭 /docs /redoc /openapi.json（公网建议 false）
    DOCS_ENABLED: bool = True
    # 媒体签名 URL 有效期（秒）；历史会话返回时会重签
    MEDIA_SIGNED_URL_TTL_SECONDS: int = 86400
    # 登录 / 注册限流（依赖 Redis）
    AUTH_RATE_LIMIT_ENABLED: bool = True
    # 仅在可信反向代理之后设 true；否则忽略 X-Forwarded-For，防伪造 IP 绕过限流
    AUTH_TRUST_X_FORWARDED_FOR: bool = False
    AUTH_LOGIN_RATE_LIMIT: int = 20
    AUTH_LOGIN_RATE_WINDOW_SECONDS: int = 60
    AUTH_REGISTER_RATE_LIMIT: int = 5
    AUTH_REGISTER_RATE_WINDOW_SECONDS: int = 3600
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_REFRESH_COOKIE_NAME: str = "kura_refresh"
    # 生产 HTTPS 设 true；本机 http 开发 false
    AUTH_COOKIE_SECURE: bool = False
    AUTH_COOKIE_SAMESITE: str = "lax"
    # 用户智能体 API Key 字段级加密：优先设置环境变量 API_KEY_ENCRYPTION_KEY（Fernet 密钥，见 cryptography.fernet.Fernet.generate_key()）
    # 未设置时由 SECRET_KEY 派生（仅适合开发；生产请显式配置独立密钥）
    API_KEY_ENCRYPTION_KEY: typing.Optional[str] = None
    DATETIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"

    # 管理端（Tortoise ORM）PostgreSQL 连接串；未设置时回落到 DATABASE_URL
    ADMIN_DATABASE_URL: typing.Optional[str] = None
    # 智能体对话：PostgreSQL（可与主库分离；未设置 CHAT_DATABASE_URL 时使用 DATABASE_URL）
    CHAT_DATABASE_URL: typing.Optional[str] = None
    DATABASE_URL: typing.Optional[str] = None
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_KEY_PREFIX: str = "kura_ai"
    REDIS_CACHE_TTL_SECONDS: int = 300
    # 智能体对话异步 Job（刷新后可重连 SSE）在 Redis 中的 TTL（秒）
    CHAT_JOB_TTL_SECONDS: int = 86400
    # Job 到达终态（完成/取消/失败）后的 TTL（秒）：只需覆盖断线重连与迟到追更，远小于 running 期
    CHAT_JOB_DONE_TTL_SECONDS: int = 3600
    # 流式生成中取消标记的实查最小间隔（秒）：chunk 级检查仅在到点时才真正读 Redis
    CHAT_CANCEL_CHECK_INTERVAL: float = 0.25
    # 对话生成接口（/chat、/chat/stream、/chat/jobs）按用户限流：每分钟最多次数，0=关闭
    CHAT_RATE_LIMIT_PER_MINUTE: int = 20
    # 单条用户消息字符上限（防超长输入刷 token 成本）
    CHAT_MESSAGE_MAX_LENGTH: int = 8000
    # 会话 ID 字符上限
    CHAT_SESSION_ID_MAX_LENGTH: int = 64
    # 共享（非属主）会话是否允许加载属主 MCP 工具；true 会使共享用户可驱动属主凭据，务必保持 false
    SHARE_CHAT_ALLOW_OWNER_MCP_TOOLS: bool = False
    # 注入模型前的非可信外部内容（知识库/网页检索结果）字符上限，0=不截断
    TOOL_UNTRUSTED_CONTENT_MAX_CHARS: int = 16000
    # 单个 MCP 工具返回内容的字符上限，0=不截断
    MCP_TOOL_RESULT_MAX_CHARS: int = 8000
    # 高危/写操作 MCP 工具执行前要求用户确认；确认记录按工具名与参数哈希一次性生效
    MCP_CONFIRMATION_REQUIRED: bool = True
    MCP_CONFIRMATION_TTL_SECONDS: int = 300
    MCP_CONFIRMATION_MAX_PER_TURN: int = 3
    MCP_HIGH_RISK_TOOL_PATTERNS: list[str] = []
    # 全局嵌入（DashScope 兼容 OpenAI /embeddings）- 多模态嵌入模型
    EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBEDDING_MODEL: str = "qwen3-vl-embedding"
    EMBEDDING_API_KEY: typing.Optional[str] = None
    EMBEDDING_DIM: int = 1536  # qwen3-vl-embedding 的向量维度
    EMBEDDING_BATCH_SIZE: int = 10

    # Milvus
    MILVUS_HOST: str = "127.0.0.1"
    MILVUS_PORT: str = "19530"
    MILVUS_COLLECTION: str = "kura_ai_kb"
    # 云上 / 已开鉴权的实例填 token；本地 docker standalone 通常留空
    MILVUS_TOKEN: str = ""
    # 会话记忆向量（与知识库隔离的独立 Milvus collection）
    MILVUS_COLLECTION_CHAT_MEMORY: str = "kura_ai_chat_memory"
    # Milvus VARCHAR(text) 的 max_length；修改后需重建集合（见 CHAT_MEMORY_MILVUS_RECREATE_ON_INIT）
    CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH: int = 8192
    # 为 True 时启动 init 会先 drop 再建会话记忆 collection。另：init 时若现有集合的 dense 维与 EMBEDDING_DIM 不一致会自动 drop 重建（无需手开此项）
    CHAT_MEMORY_MILVUS_RECREATE_ON_INIT: bool = False
    # 启用：远期归档检索 + 按 token 预算压缩进上下文（替代「只留最近 N 轮」滑动窗口）
    CHAT_USE_SESSION_MEMORY: bool = True
    CHAT_MEMORY_WINDOW_TURNS: int = 10  # 仅当 CHAT_COMPACT_ENABLED=false 时作为滑动窗口轮数
    CHAT_COMPACT_ENABLED: bool = True

    # ---- token 预算口径（对齐 Claude Code 的「距上限固定缓冲」，而非百分比/轮数）----
    # 智能体未单独配置 context_window 时使用的模型上下文窗口
    CHAT_MODEL_CONTEXT_WINDOW_DEFAULT: int = 128000
    # 第一层缓冲：从原始窗口里给「摘要自身输出」预留的 token（CC 用 20k，我们摘要更短）
    CHAT_COMPACT_SUMMARY_RESERVE_TOKENS: int = 8000
    # 第二层缓冲：再留一道独立安全垫；trigger = window - reserve - buffer
    CHAT_COMPACT_BUFFER_TOKENS: int = 6000
    # 压缩后保留的最近原文 token 数（须明显小于 trigger）
    CHAT_COMPACT_KEEP_TOKENS: int = 8000
    # 后台预压缩软阈值比例：估算用量超过 effective * ratio 就在后台提前算好摘要
    CHAT_COMPACT_PRECOMPACT_RATIO: float = 0.6
    # 是否允许后台预压缩（关闭后退化为仅请求内同步压缩）
    CHAT_COMPACT_PRECOMPUTE: bool = True
    # 启发式估算系数：CJK 字符≈1 token/字；其余字符≈3.6 字/token（会被真实 usage 校准覆盖）
    CHAT_TOKENS_PER_CJK_CHAR: float = 1.0
    CHAT_CHARS_PER_LATIN_TOKEN: float = 3.6

    # ---- 摘要形态 ----
    # 摘要正文上限（字符）；9 段结构化清单需要比旧的 4000 更宽
    CHAT_COMPACT_SUMMARY_MAX_CHARS: int = 8000
    # 喂给摘要器的「被移出对话」token 上限；超出时丢最旧、保最新
    CHAT_COMPACT_SUMMARIZER_INPUT_TOKENS: int = 60000
    # 单会话同时保留的分段摘要数上限；超出则本次压缩顺带把旧链归并为 level+1 的粗粒度段
    CHAT_COMPACT_MAX_SEGMENTS: int = 8
    # 段链摘要总 token 上限，超出同样触发归并
    CHAT_COMPACT_SEGMENTS_TOKEN_BUDGET: int = 4000
    # 连续失败该次数后熔断：不再调 LLM，降级为硬截断（成功后清零）
    CHAT_COMPACT_MAX_CONSECUTIVE_FAILURES: int = 3

    # ---- 轮内 micro-compact：清理旧的、可重新获取的工具结果 ----
    CHAT_MICROCOMPACT_ENABLED: bool = True
    # 白名单工具的 tool_result 只保留最近 N 条，更早的替换为占位符
    CHAT_MICROCOMPACT_KEEP_RECENT: int = 5
    # 轮内估算用量超过 window * ratio 才开始清理（未达阈值时零改动）
    CHAT_MICROCOMPACT_TRIGGER_RATIO: float = 0.6
    # 单个工具结果超过该 token 数时，无论新旧都先截断（保留头尾）
    CHAT_MICROCOMPACT_MAX_RESULT_TOKENS: int = 6000

    # ---- 会话原文翻牌工具 read_session_history（向量库的精确取证兜底通道）----
    # 同一用户提问轮次内最多成功调用次数，防止模型反复翻库
    CHAT_HISTORY_TOOL_MAX_CALLS: int = 2

    # ---- 后台任务受理模式 ----
    # thread=API 进程内后台线程（本地开发）；queue=Redis 队列交独立 worker（多副本生产必用）
    CHAT_MEMORY_TASK_MODE: str = "thread"
    # 跨进程互斥锁 TTL（秒）：同一会话的压缩/归档串行化，防止重复计算与丢更新
    CHAT_MEMORY_LOCK_TTL_SECONDS: int = 120

    # 以下为已弃用的字符口径，仅在无法解析模型窗口时兜底
    CHAT_COMPACT_TRIGGER_CHARS: int = 80000
    CHAT_COMPACT_KEEP_CHARS: int = 24000
    CHAT_COMPACT_HEADROOM_CHARS: int = 12000
    CHAT_COMPACT_TOOLS_ESTIMATE_CHARS: int = 8000
    # 单块归档字符上限（略小于原默认，减轻单条向量上下文过长）
    CHAT_MEMORY_CHUNK_MAX_CHARS: int = 1400
    CHAT_MEMORY_SEARCH_TOP_K: int = 5
    # 每轮用当前用户输入预检索会话记忆并注入 System 补充块（与工具检索互补）
    CHAT_MEMORY_PROACTIVE_INJECT: bool = True
    CHAT_MEMORY_PROACTIVE_TOP_K: int = 3
    # 归档在对话落库后后台执行，不阻塞响应
    CHAT_MEMORY_ARCHIVE_ASYNC: bool = True

    # ---- 长期记忆：只存蒸馏结果，不存原文轮次 ----
    # episodic：把压缩产生的分段摘要写入向量库（约 12 轮 1 段，而非十几条原文块）
    CHAT_MEMORY_EPISODIC_CHUNK_CHARS: int = 2000
    # factual：与摘要共用同一次 LLM 调用抽取稳定事实（偏好/约束/决策/实体），按 fact_key 去重 upsert
    CHAT_MEMORY_FACTUAL_ENABLED: bool = True
    CHAT_MEMORY_FACT_MAX_CHARS: int = 600
    CHAT_MEMORY_FACT_TOP_K: int = 3
    # raw：原文轮次块归档（v1 行为）降级为兜底，默认关闭
    CHAT_MEMORY_ARCHIVE_RAW_ENABLED: bool = False
    # 价值闸门：低于该分的轮次不单独入库（仍会进入压缩摘要输入，信息不丢）
    CHAT_MEMORY_MIN_VALUE_SCORE: float = 0.25
    CHAT_MEMORY_MIN_USER_CHARS: int = 8
    # 近重复检测：与最近 N 轮的 8-gram 指纹比对，Jaccard 超阈值即视为重复
    CHAT_MEMORY_DEDUP_WINDOW: int = 20
    CHAT_MEMORY_DEDUP_JACCARD: float = 0.85
    # 注入预算（token）：预检索注入 System 的摘录上限 / 工具调用返回上限
    CHAT_MEMORY_INJECT_MAX_TOKENS: int = 1200
    CHAT_MEMORY_TOOL_MAX_TOKENS: int = 3000
    # 过滤表达式 IN 列表长度上限；超出降级为范围表达式 + Python 侧精确过滤
    CHAT_MEMORY_FILTER_MAX_KEYS: int = 256

    # ---- 长期记忆淘汰与回收 ----
    # 单会话向量硬上限：超出按 created_at 删最旧（段摘要的分层收敛由压缩侧负责，此处只兜底）
    CHAT_MEMORY_HARD_MAX_VECTORS: int = 400
    CHAT_MEMORY_EVICT_BATCH: int = 200
    # 闲置 TTL：以 PG 会话 updated_at 为真相源，删掉长期不活跃会话的会话级向量（0=关闭）
    CHAT_MEMORY_TTL_DAYS: int = 90
    # worker 内 TTL 清理线程的扫描间隔（秒），默认 6 小时
    CHAT_MEMORY_GC_INTERVAL_SECONDS: int = 21600
    # 用户级跨会话事实的条数上限，超出删最旧
    CHAT_MEMORY_USER_FACT_MAX: int = 500
    CHAT_MEMORY_COUNT_LIMIT: int = 16000

    # 知识库文档上传任务：后台线程处理，上传接口立即返回 task_id，前端轮询进度
    KB_UPLOAD_JOB_TTL_SECONDS: int = 86400
    # 单个文档处理的整体时长上限（秒）；超过判超时中止，旧文档保留、须重传
    KB_UPLOAD_TASK_TIMEOUT_SECONDS: int = 900
    # 单次嵌入 API 调用（文本批/单张图片）的 HTTP 超时（秒），防止单调用悬死 300 秒
    KB_UPLOAD_EMBEDDING_HTTP_TIMEOUT_SECONDS: int = 90
    # 上传文件大小上限（字节）；超过直接 400 拒绝
    KB_UPLOAD_MAX_BYTES: int = 50 * 1024 * 1024
    # 同时并行处理的文档数上限；超出在队列等待（status=queued）
    KB_UPLOAD_MAX_PARALLEL: int = 8
    # 受理模式：queue=独立 worker 进程消费（生产推荐）；inline=API 进程线程池（本地开发）
    KB_UPLOAD_MODE: str = "inline"
    # 独立 worker 的处理线程数（KB_UPLOAD_MODE=queue 时生效）
    KB_WORKER_THREADS: int = 4
    # 全局排队 + 处理中任务数上限（0 表示不限制）；超过返回 429 让前端稍后重试
    KB_UPLOAD_QUEUE_MAX: int = 500
    # 单用户活动任务（排队 + 处理中）上限（0 表示不限制）
    KB_UPLOAD_USER_MAX_ACTIVE: int = 600
    # 用户活动任务集合 TTL（秒）：worker 崩溃未清理的陈旧计数在此期限内自动过期
    KB_UPLOAD_USER_ACTIVE_TTL_SECONDS: int = 7200
    # worker 崩溃回收阈值（秒）：processing 中任务心跳超过该时长才会被重投/判死
    KB_UPLOAD_STALE_SECONDS: int = 900
    # 受理阶段的 pending 对象 key 前缀（处理后由 worker 删除）
    KB_UPLOAD_PENDING_PREFIX: str = "pending-uploads"
    # 同名文档「替换落库」阶段的 Redis 互斥锁 TTL（秒）
    KB_UPLOAD_SWAP_LOCK_TTL_SECONDS: int = 600
    # 嵌入调用失败（429 限流 / 5xx / 网络抖动）的最大自动重试次数（指数退避 + 抖动）
    KB_UPLOAD_EMBEDDING_MAX_RETRIES: int = 3
    # 重试退避基数（秒）：第 n 次重试等待 base * 2**(n-1)，上限 KB_UPLOAD_EMBEDDING_RETRY_MAX_SECONDS
    KB_UPLOAD_EMBEDDING_RETRY_BASE_SECONDS: float = 1.0
    KB_UPLOAD_EMBEDDING_RETRY_MAX_SECONDS: float = 8.0
    # 全局并发调用嵌入 API 的上限（跨所有上传线程共享），避免并发过高触发服务商限流
    EMBEDDING_MAX_CONCURRENCY: int = 4
    # 等待嵌入并发额度的最长时间（秒），超时按调用失败处理
    EMBEDDING_CONCURRENCY_WAIT_SECONDS: int = 120

    # 智能体知识库文档在对象存储 bucket 内的 key 前缀：user_agent_docs/user_{id}/{agent_id}/
    USER_AGENT_KB_DOCS_ROOT: str = "user_agent_docs"
    # 智能体知识库图片在对象存储 bucket 内的 key 前缀：user_agent_images/user_{id}/{agent_id}/
    USER_AGENT_KB_IMAGES_ROOT: str = "user_agent_images"
    # 浏览器访问图片路径前缀
    USER_AGENT_KB_IMAGES_URL_PREFIX: str = "/api/v1/media/user_agent_images"
    # 对外可访问的 API 根地址（无尾斜杠）。聊天/知识库图片一律用同源相对路径 /api/v1/media/...，
    # 不拼接此值，以免 http 绝对地址被前端 CSP img-src 拦截。
    PUBLIC_API_BASE: str = ""
    # 会话对话附件在对象存储 bucket 内的 key 前缀：user_agent_uploads/user_{id}/{agent_id}/{session}/
    USER_AGENT_CHAT_UPLOAD_ROOT: str = "user_agent_uploads"

    # ===== 对象存储（MinIO / S3 兼容）；开发与生产统一使用，开发默认指向本机 minio-app 容器 =====
    # S3 API 地址（不含协议），开发 127.0.0.1:9002；生产 compose 注入 minio-app:9000
    S3_ENDPOINT: str = "127.0.0.1:9002"
    S3_ACCESS_KEY: str = "kura_app"
    # 生产务必通过环境变量覆盖，与 docker-compose 的 MINIO_APP_ROOT_PASSWORD 一致
    S3_SECRET_KEY: str = "kura_app_dev_only_change_me"
    S3_BUCKET: str = "kura-user-files"
    # 是否走 https；本机/内网容器间通信为 false
    S3_SECURE: bool = False
    # 单次请求：附件个数、单文件大小（字节）、会话附件总大小上限
    CHAT_UPLOAD_MAX_FILES_PER_MESSAGE: int = 5
    CHAT_UPLOAD_MAX_BYTES_PER_FILE: int = 15 * 1024 * 1024
    CHAT_UPLOAD_MAX_SESSION_BYTES: int = 80 * 1024 * 1024
    CHAT_UPLOAD_MAX_ATTACHMENTS_PER_SESSION: int = 80
    # 发给视觉模型的图：仅压 payload，不改对象存储原图
    CHAT_VISION_MAX_EDGE: int = 1568
    CHAT_VISION_JPEG_QUALITY: int = 80
    CHAT_VISION_MAX_BYTES: int = 400 * 1024
    # 两阶段读图：带图且启用检索类工具时，先无工具看图出描述，再纯文本 agent
    CHAT_VISION_CAPTION_ENABLED: bool = True
    CHAT_VISION_CAPTION_TIMEOUT_SECONDS: int = 60
    CHAT_VISION_CAPTION_MAX_CHARS: int = 1200

    # LLM HTTP：禁止 timeout=None；进程内并发闸门限制同时进行的对话 Job
    LLM_HTTP_CONNECT_TIMEOUT: float = 10.0
    LLM_HTTP_READ_TIMEOUT: float = 120.0
    LLM_HTTP_WRITE_TIMEOUT: float = 30.0
    LLM_HTTP_POOL_TIMEOUT: float = 10.0
    LLM_MAX_INFLIGHT: int = 8
    # 并发闸门排队等待上限（秒）：超时将任务置为 failed，避免前端无限静默等待
    LLM_QUEUE_TIMEOUT_SECONDS: float = 120.0

    # RAG：可选单独指定打分模型；未设置则与智能体对话模型相同
    RAG_GRADE_MODEL: typing.Optional[str] = None
    # 默认不走 complex（step-back + HyDE 全开）；路由若选出 complex 则降为 step_back
    RAG_ALLOW_COMPLEX_STRATEGY: bool = False
    # step-back 默认只生成问句，不先让模型答题（避免首 token 前多一次 LLM）
    RAG_STEP_BACK_ANSWER_ENABLED: bool = False
    # 扩展检索后二次质量门控：逐块打分全部不相关（或无结果）时置 no_answer，工具侧返回拒答文案；
    # 关闭后保持旧行为（改写后直接生成）
    KB_GRADE_REFUSAL_ENABLED: bool = True
    # Rerank 分数阈值：最高 relevance_score 低于该值时按「知识库无相关资料」处理；None=禁用（默认）
    RERANK_MIN_SCORE: typing.Optional[float] = None

    # ===== 实验平台端到端问答评测（生成 + LLM 判分）；未配置时回落 EMBEDDING_* =====
    # 生成/判分登 OpenAI 兼容端点；base_url/api_key 为空分别回落 EMBEDDING_BASE_URL / EMBEDDING_API_KEY
    EXP_EVAL_LLM_BASE_URL: typing.Optional[str] = None
    EXP_EVAL_LLM_API_KEY: typing.Optional[str] = None
    EXP_EVAL_ANSWER_MODEL: str = "qwen-plus"
    # 判分模型；空则与 EXP_EVAL_ANSWER_MODEL 相同
    EXP_EVAL_JUDGE_MODEL: typing.Optional[str] = None
    EXP_EVAL_TEMPERATURE: float = 0.0
    # 送入生成的资料总字符上限（超出截断）
    EXP_EVAL_MAX_CONTEXT_CHARS: int = 12000
    # 生成答案入库/送判的字符上限
    EXP_EVAL_MAX_ANSWER_CHARS: int = 4000
    # 可选 LangSmith 导出（仅手动触发；未配置 key 时导出接口返回 400）
    LANGSMITH_API_KEY: typing.Optional[str] = None
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_PROJECT: str = "kura-ai-experiment"

    # ===== 知识库重排（DashScope qwen3-vl-rerank，多模态；三者皆配置才启用，否则跳过 rerank）=====
    RERANK_MODEL: str = ""
    RERANK_API_KEY: str = ""
    # 完整端点（.../services/rerank/text-rerank/text-rerank）或基础地址（.../api/v1，自动补全路径）
    RERANK_BINDING_HOST: str = ""
    # 单次 rerank HTTP 超时（秒）
    RERANK_TIMEOUT_SECONDS: int = 15
    # 图片块是否以 base64 Data URI 参与多模态重排；False 时仅文本块参与
    RERANK_INCLUDE_IMAGES: bool = True
    # 单次送排文档数上限（含图片，控制成本与延迟）
    RERANK_MAX_CANDIDATES: int = 30
    # 单张图片参与重排的体积上限（base64 编码前字节数），超限则该图不参与重排
    RERANK_MAX_IMAGE_BYTES: int = 4 * 1024 * 1024

    # Auto-merge / 叶子层（与 SuperMew 一致）
    AUTO_MERGE_ENABLED: bool = True
    AUTO_MERGE_THRESHOLD: int = 3
    LEAF_RETRIEVE_LEVEL: int = 3
    # 多模态 RAG：命中「文本块」时按 PostgreSQL 中 related_text_ids 拉取同页/邻近关联图片并并入候选
    KB_RELATED_IMAGE_EXPANSION: bool = True
    KB_RELATED_IMAGE_MAX_PER_TEXT: int = 5
    KB_RELATED_IMAGE_MAX_TOTAL: int = 24
    # 最终 top_k 中尽量保留至少 N 个图片块（有可用图片时；与纯文本按 score 争位）
    KB_MIN_IMAGE_SLOTS: int = 2
    # 为 True 时：在智能体每轮回复前，自动调用一次模型从知识库文档列表中圈选 file_key，再在该子集上检索
    KB_DOCUMENT_PRESELECT_ENABLED: bool = True
    # 前置选档提示中最多列出多少份文档名（多文档时截断）
    KB_PRESELECT_MAX_DOC_LINES: int = 100
    # 前置选档时附带最近 N 轮对话作为上下文；为 0 则仅传当前用户问题
    KB_PRESELECT_CONTEXT_TURNS: int = 3
    KB_PRESELECT_CONTEXT_MAX_MSG_CHARS: int = 1200
    KB_PRESELECT_CONTEXT_MAX_TOTAL_CHARS: int = 5000
    # 「当前用户问题」段最大字符数（与「前序对话」分开截断）
    KB_PRESELECT_MAX_CURRENT_QUESTION_CHARS: int = 8000

    # 联网搜索。provider: auto / ddgs / bocha / bing_html
    # auto：无代理且已配博查 Key 时 bocha → bing_html；有 WEB_SEARCH_PROXY 时 ddgs → bocha → bing_html
    WEB_SEARCH_ENABLED: bool = True
    WEB_SEARCH_PROVIDER: str = "auto"
    WEB_SEARCH_MAX_RESULTS: int = 5
    # ddgs 格式 {country}-{language}；旧值 zh-cn 会自动归一为 cn-zh
    WEB_SEARCH_REGION: str = "cn-zh"
    WEB_SEARCH_TIMEOUT_SECONDS: int = 15
    # 可选代理（ddgs / 博查 / bing_html 均使用），如 http://127.0.0.1:7890
    WEB_SEARCH_PROXY: str = ""
    # 空=自动（无代理 bing / 有代理 auto）；也可强制如 bing 或 google,brave,bing
    WEB_SEARCH_DDGS_BACKEND: str = ""
    # 博查 Web Search（国内无代理主路径）；空则 auto 不走博查。申请: https://open.bochaai.com/
    WEB_SEARCH_BOCHA_API_KEY: str = ""
    WEB_SEARCH_BOCHA_ENDPOINT: str = "https://api.bochaai.com/v1/web-search"
    # 召回条数（重排前，1–50）；最终交给模型的条数仍是 WEB_SEARCH_MAX_RESULTS
    WEB_SEARCH_CANDIDATE_COUNT: int = 15
    # 时效：auto 按查询时间意图词映射；也可固定 noLimit / oneDay / oneWeek / oneMonth / oneYear
    WEB_SEARCH_FRESHNESS: str = "auto"
    # 博查 Semantic Reranker（复用 WEB_SEARCH_BOCHA_API_KEY；无 Key 或 false 则跳过）
    WEB_SEARCH_RERANK_ENABLED: bool = True
    WEB_SEARCH_RERANK_ENDPOINT: str = "https://api.bochaai.com/v1/rerank"
    WEB_SEARCH_RERANK_MODEL: str = "gte-rerank"
    WEB_SEARCH_RERANK_MIN_SCORE: float = 0.2
    WEB_SEARCH_RERANK_TIMEOUT_SECONDS: int = 10
    # 通用权威度软加权（与 rerank 融合后再截断到 MAX_RESULTS）
    WEB_SEARCH_AUTHORITY_ENABLED: bool = True
    WEB_SEARCH_AUTHORITY_BLEND: float = 0.25
    # 有时间意图时 datePublished 软加权（与权威度、rerank 三分融合）
    WEB_SEARCH_RECENCY_BLEND: float = 0.15
    # 读页：对最终 topK 的前 N 条抓取 HTML 主文（失败保留摘要）
    WEB_SEARCH_READ_ENABLED: bool = True
    WEB_SEARCH_READ_TOP_N: int = 3
    WEB_SEARCH_READ_TIMEOUT_SECONDS: int = 8
    WEB_SEARCH_READ_MAX_BYTES: int = 524288
    WEB_SEARCH_READ_MAX_CHARS: int = 2000
    # 单轮对话内 web_search 工具最大调用次数（防 ReAct 循环触发限流）
    WEB_SEARCH_MAX_CALLS_PER_TURN: int = 2
    # fetch_url 独立配额与抽文长度
    WEB_SEARCH_FETCH_MAX_CALLS_PER_TURN: int = 3
    WEB_SEARCH_FETCH_MAX_CHARS: int = 4000
    # 文字搜图（web_image_search）：召回条数、回答里给现成 Markdown 的条数、每轮调用上限
    WEB_IMAGE_SEARCH_MAX_RESULTS: int = 6
    WEB_IMAGE_SEARCH_MARKDOWN_COUNT: int = 4
    WEB_IMAGE_SEARCH_MAX_CALLS_PER_TURN: int = 2
    # 先多召回再按来源/尺寸筛选；长边小于该值且宽高已知时视为缩略图（全是缩略图则仍保留）
    WEB_IMAGE_SEARCH_CANDIDATE_COUNT: int = 18
    WEB_IMAGE_SEARCH_MIN_EDGE: int = 240
    # 搜图多模态重排（复用知识库 RERANK_*；模型须含 vl-rerank，否则跳过）
    WEB_IMAGE_RERANK_ENABLED: bool = True
    WEB_IMAGE_SIZE_BLEND: float = 0.15

    @property
    def chat_database_url(self) -> str:
        raw = (self.CHAT_DATABASE_URL or self.DATABASE_URL or "").strip()
        if not raw:
            raise ValueError(
                "请配置 CHAT_DATABASE_URL 或 DATABASE_URL（PostgreSQL 连接串）用于智能体聊天存储。"
            )
        return raw

    @property
    def admin_database_url(self) -> str:
        raw = (self.ADMIN_DATABASE_URL or self.DATABASE_URL or "").strip()
        if not raw:
            raise ValueError(
                "请配置 ADMIN_DATABASE_URL 或 DATABASE_URL（PostgreSQL 连接串）用于管理端存储。"
            )
        return raw

    @property
    def TORTOISE_ORM(self) -> dict:
        return {
            "connections": {"default": _tortoise_pg_url(self.admin_database_url)},
            "apps": {
                "models": {
                    "models": ["app.models"],
                    "default_connection": "default",
                },
            },
            "use_tz": False,  # Whether to use timezone-aware datetimes
            "timezone": "Asia/Shanghai",  # Timezone setting
        }


settings = Settings()
