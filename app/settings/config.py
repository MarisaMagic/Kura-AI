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

    CORS_ORIGINS: typing.List = ["*"]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: typing.List = ["*"]
    CORS_ALLOW_HEADERS: typing.List = ["*"]

    DEBUG: bool = True
    # 为 True 时，每次调用 LLM 前在服务端终端打印完整输入消息列表（含系统/用户/工具等）
    DEBUG_AGENT_KB_PROMPT: bool = True

    PROJECT_ROOT: str = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    BASE_DIR: str = os.path.abspath(os.path.join(PROJECT_ROOT, os.pardir))
    LOGS_ROOT: str = os.path.join(BASE_DIR, "app/logs")
    # 用户头像本地目录（可通过环境变量 USER_AVATAR_ROOT 覆盖，例如 Linux 上 /data/user_avatar）
    USER_AVATAR_ROOT: str = os.path.join(BASE_DIR, "data", "user_avatar")
    # 浏览器访问路径前缀（与 app 中 StaticFiles 挂载一致）
    USER_AVATAR_URL_PREFIX: str = "/api/v1/media/user_avatar"
    # 智能体头像：本地目录（可通过环境变量 USER_AGENT_AVATAR_ROOT 覆盖，例如 /data/user_agents_avatar）
    USER_AGENT_AVATAR_ROOT: str = os.path.join(BASE_DIR, "data", "user_agents_avatar")
    USER_AGENT_AVATAR_URL_PREFIX: str = "/api/v1/media/user_agents_avatar"
    SECRET_KEY: str = "3488a63e1765035d386f05409663f55c83bfae3b3c61a932744b20ad14244dcf"  # openssl rand -hex 32
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

    # 全局嵌入（DashScope 兼容 OpenAI /embeddings）
    EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_API_KEY: typing.Optional[str] = None
    EMBEDDING_DIM: int = 1024
    EMBEDDING_BATCH_SIZE: int = 10

    # Milvus
    MILVUS_HOST: str = "127.0.0.1"
    MILVUS_PORT: str = "19530"
    MILVUS_COLLECTION: str = "kura_ai_kb"

    # 智能体知识库文档根目录：data/user_agent_docs/user_{id}/{agent_id}/
    USER_AGENT_KB_DOCS_ROOT: str = os.path.join(BASE_DIR, "data", "user_agent_docs")
    # 会话对话附件：data/user_agent_uploads/user_{id}/{agent_id}/{session}/
    USER_AGENT_CHAT_UPLOAD_ROOT: str = os.path.join(BASE_DIR, "data", "user_agent_uploads")
    # 单次请求：附件个数、单文件大小（字节）、会话附件总大小上限
    CHAT_UPLOAD_MAX_FILES_PER_MESSAGE: int = 5
    CHAT_UPLOAD_MAX_BYTES_PER_FILE: int = 15 * 1024 * 1024
    CHAT_UPLOAD_MAX_SESSION_BYTES: int = 80 * 1024 * 1024
    CHAT_UPLOAD_MAX_ATTACHMENTS_PER_SESSION: int = 80

    # RAG：可选单独指定打分模型；未设置则与智能体对话模型相同
    RAG_GRADE_MODEL: typing.Optional[str] = None

    # Auto-merge / 叶子层（与 SuperMew 一致）
    AUTO_MERGE_ENABLED: bool = True
    AUTO_MERGE_THRESHOLD: int = 2
    LEAF_RETRIEVE_LEVEL: int = 3

    @property
    def chat_database_url(self) -> str:
        raw = (self.CHAT_DATABASE_URL or self.DATABASE_URL or "").strip()
        if not raw:
            raise ValueError(
                "请配置 CHAT_DATABASE_URL 或 DATABASE_URL（PostgreSQL 连接串）用于智能体聊天存储。"
            )
        return raw


settings = Settings()
