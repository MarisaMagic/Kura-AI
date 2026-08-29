import os
import typing

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # 用户头像本地目录（可通过环境变量 USER_AVATAR_ROOT 覆盖，例如 Linux 上 /data/user_avatar）
    USER_AVATAR_ROOT: str = os.path.join(BASE_DIR, "data", "user_avatar")
    # 浏览器访问路径前缀（与签名媒体路由一致）
    USER_AVATAR_URL_PREFIX: str = "/api/v1/media/user_avatar"
    # 智能体头像：本地目录（可通过环境变量 USER_AGENT_AVATAR_ROOT 覆盖，例如 /data/user_agents_avatar）
    USER_AGENT_AVATAR_ROOT: str = os.path.join(BASE_DIR, "data", "user_agents_avatar")
    USER_AGENT_AVATAR_URL_PREFIX: str = "/api/v1/media/user_agents_avatar"
    # 须在项目根目录 .env 中设置；勿提交仓库。生成: openssl rand -hex 32
    SECRET_KEY: str
    # 首次启动且库中无用户时创建的管理员（密码勿提交仓库；至少 8 位且含字母与数字）
    INITIAL_ADMIN_USERNAME: str = "admin"
    INITIAL_ADMIN_EMAIL: str = "admin@localhost"
    INITIAL_ADMIN_PASSWORD: typing.Optional[str] = None
    # 是否开放邮箱自助注册（公网务必 false；本机开发可在 .env 设 true）
    ALLOW_PUBLIC_REGISTRATION: bool = False
    # 为 True 时允许智能体 base_url / MCP url 指向内网或本机（仅本地调试 MCP）
    ALLOW_PRIVATE_UPSTREAM_URLS: bool = False
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
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 day
    # 用户智能体 API Key 字段级加密：优先设置环境变量 API_KEY_ENCRYPTION_KEY（Fernet 密钥，见 cryptography.fernet.Fernet.generate_key()）
    # 未设置时由 SECRET_KEY 派生（仅适合开发；生产请显式配置独立密钥）
    API_KEY_ENCRYPTION_KEY: typing.Optional[str] = None
    TORTOISE_ORM: dict = {
        "connections": {
            # SQLite configuration
            "sqlite": {
                "engine": "tortoise.backends.sqlite",
                "credentials": {"file_path": f"{BASE_DIR}/db.sqlite3"},  # Path to SQLite database file
            },
            # MySQL/MariaDB configuration
            # Install with: tortoise-orm[asyncmy]
            # "mysql": {
            #     "engine": "tortoise.backends.mysql",
            #     "credentials": {
            #         "host": "localhost",  # Database host address
            #         "port": 3306,  # Database port
            #         "user": "yourusername",  # Database username
            #         "password": "yourpassword",  # Database password
            #         "database": "yourdatabase",  # Database name
            #     },
            # },
            # PostgreSQL configuration
            # Install with: tortoise-orm[asyncpg]
            # "postgres": {
            #     "engine": "tortoise.backends.asyncpg",
            #     "credentials": {
            #         "host": "localhost",  # Database host address
            #         "port": 5432,  # Database port
            #         "user": "yourusername",  # Database username
            #         "password": "yourpassword",  # Database password
            #         "database": "yourdatabase",  # Database name
            #     },
            # },
            # MSSQL/Oracle configuration
            # Install with: tortoise-orm[asyncodbc]
            # "oracle": {
            #     "engine": "tortoise.backends.asyncodbc",
            #     "credentials": {
            #         "host": "localhost",  # Database host address
            #         "port": 1433,  # Database port
            #         "user": "yourusername",  # Database username
            #         "password": "yourpassword",  # Database password
            #         "database": "yourdatabase",  # Database name
            #     },
            # },
            # SQLServer configuration
            # Install with: tortoise-orm[asyncodbc]
            # "sqlserver": {
            #     "engine": "tortoise.backends.asyncodbc",
            #     "credentials": {
            #         "host": "localhost",  # Database host address
            #         "port": 1433,  # Database port
            #         "user": "yourusername",  # Database username
            #         "password": "yourpassword",  # Database password
            #         "database": "yourdatabase",  # Database name
            #     },
            # },
        },
        "apps": {
            "models": {
                "models": ["app.models", "aerich.models"],
                "default_connection": "sqlite",
            },
        },
        "use_tz": False,  # Whether to use timezone-aware datetimes
        "timezone": "Asia/Shanghai",  # Timezone setting
    }
    DATETIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"

    # 智能体对话：PostgreSQL（可与主库分离；未设置 CHAT_DATABASE_URL 时使用 DATABASE_URL）
    CHAT_DATABASE_URL: typing.Optional[str] = None
    DATABASE_URL: typing.Optional[str] = None
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_KEY_PREFIX: str = "kura_ai"
    REDIS_CACHE_TTL_SECONDS: int = 300
    # 智能体对话异步 Job（刷新后可重连 SSE）在 Redis 中的 TTL（秒）
    CHAT_JOB_TTL_SECONDS: int = 86400
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
    # 会话记忆向量（与知识库隔离的独立 Milvus collection）
    MILVUS_COLLECTION_CHAT_MEMORY: str = "kura_ai_chat_memory"
    # Milvus VARCHAR(text) 的 max_length；修改后需重建集合（见 CHAT_MEMORY_MILVUS_RECREATE_ON_INIT）
    CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH: int = 8192
    # 为 True 时启动 init 会先 drop 再建会话记忆 collection。另：init 时若现有集合的 dense 维与 EMBEDDING_DIM 不一致会自动 drop 重建（无需手开此项）
    CHAT_MEMORY_MILVUS_RECREATE_ON_INIT: bool = False
    # 启用：最近 N 轮进上下文 + 远期归档检索工具
    CHAT_USE_SESSION_MEMORY: bool = True
    CHAT_MEMORY_WINDOW_TURNS: int = 10
    # 单块归档字符上限（略小于原默认，减轻单条向量上下文过长）
    CHAT_MEMORY_CHUNK_MAX_CHARS: int = 1400
    CHAT_MEMORY_SEARCH_TOP_K: int = 5
    # 每轮用当前用户输入预检索会话记忆并注入 System 补充块（与工具检索互补）
    CHAT_MEMORY_PROACTIVE_INJECT: bool = True
    CHAT_MEMORY_PROACTIVE_TOP_K: int = 3
    # 归档在对话落库后后台执行，不阻塞响应
    CHAT_MEMORY_ARCHIVE_ASYNC: bool = True

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
    # 同名文档「替换落库」阶段的 Redis 互斥锁 TTL（秒）
    KB_UPLOAD_SWAP_LOCK_TTL_SECONDS: int = 600

    # 智能体知识库文档根目录：data/user_agent_docs/user_{id}/{agent_id}/
    USER_AGENT_KB_DOCS_ROOT: str = os.path.join(BASE_DIR, "data", "user_agent_docs")
    # 智能体知识库图片根目录：data/user_agent_images/user_{id}/{agent_id}/
    USER_AGENT_KB_IMAGES_ROOT: str = os.path.join(BASE_DIR, "data", "user_agent_images")
    # 浏览器访问图片路径前缀
    USER_AGENT_KB_IMAGES_URL_PREFIX: str = "/api/v1/media/user_agent_images"
    # 对外可访问的 API 根地址（无尾斜杠），用于拼接知识库图片完整 URL；例如 http://127.0.0.1:8000 或 https://api.example.com
    # 未设置时工具返回相对路径，前端需自行补全；多模态 image_url 建议使用完整 http(s) 地址
    PUBLIC_API_BASE: str = ""
    # 会话对话附件：data/user_agent_uploads/user_{id}/{agent_id}/{session}/
    USER_AGENT_CHAT_UPLOAD_ROOT: str = os.path.join(BASE_DIR, "data", "user_agent_uploads")
    # 单次请求：附件个数、单文件大小（字节）、会话附件总大小上限
    CHAT_UPLOAD_MAX_FILES_PER_MESSAGE: int = 5
    CHAT_UPLOAD_MAX_BYTES_PER_FILE: int = 15 * 1024 * 1024
    CHAT_UPLOAD_MAX_SESSION_BYTES: int = 80 * 1024 * 1024
    CHAT_UPLOAD_MAX_ATTACHMENTS_PER_SESSION: int = 80

    # RAG：可选单独指定打分模型；未设置则与智能体对话模型相同
    RAG_GRADE_MODEL: typing.Optional[str] = None
    # 扩展检索后二次质量门控：逐块打分全部不相关（或无结果）时置 no_answer，工具侧返回拒答文案；
    # 关闭后保持旧行为（改写后直接生成）
    KB_GRADE_REFUSAL_ENABLED: bool = True
    # Rerank 分数阈值：最高 relevance_score 低于该值时按「知识库无相关资料」处理；None=禁用（默认）
    RERANK_MIN_SCORE: typing.Optional[float] = None

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
    # 单轮对话内 web_search 工具最大调用次数（防 ReAct 循环触发限流）
    WEB_SEARCH_MAX_CALLS_PER_TURN: int = 2

    @property
    def chat_database_url(self) -> str:
        raw = (self.CHAT_DATABASE_URL or self.DATABASE_URL or "").strip()
        if not raw:
            raise ValueError(
                "请配置 CHAT_DATABASE_URL 或 DATABASE_URL（PostgreSQL 连接串）用于智能体聊天存储。"
            )
        return raw


settings = Settings()
