"""
聊天库 ORM（SQLAlchemy），与主应用 Tortoise 库分离。
支持多用户、多智能体的聊天场景，每个对话会话可以包含多条消息
级联删除: 当会话删除时，关联的消息也会自动删除
索引优化: 关键字段都添加了索引，提高查询性能
JSON存储: 使用 JSON 类型存储灵活的元数据和追踪信息
时间戳: 记录创建和更新时间，便于追踪

智能体知识库，用于存储智能体知识库的元数据和分块信息。
支持多用户、多智能体的知识库，每个知识库可以包含多个文件，每个文件可以包含多个分块。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.chat.database import Base


class KbDocument(Base):
    """
    智能体知识库：已上传文件元数据（展示名为原始上传文件名）。
    :param id: 主键
    :param kb_scope: 知识库范围
    :param display_filename: 展示文件名
    :param stored_filename: 存储文件名
    :param file_type: 文件类型
    :param chunk_count: 分块数量
    :param created_at: 创建时间
    :param updated_at: 更新时间
    """

    __tablename__ = "mg_kb_documents"
    __table_args__ = (UniqueConstraint("kb_scope", "display_filename", name="uq_mg_kb_scope_display_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kb_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 文件内容 sha256，用于同内容重传跳过重建
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class KbParentChunk(Base):
    """
    知识库父级分块（L1/L2），用于 Auto-merge。
    :param chunk_id: 分块ID
    :param kb_scope: 知识库范围
    :param parent_chunk_id: 父级分块ID
    :param root_chunk_id: 根级分块ID
    :param chunk_level: 分块层级
    :param chunk_idx: 分块索引
    :param updated_at: 更新时间
    """

    __tablename__ = "mg_kb_parent_chunks"

    chunk_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    kb_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_chunk_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    root_chunk_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    chunk_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class KbImage(Base):
    """
    知识库图片元数据表，用于存储从文档中提取的图片信息。
    :param id: 图片唯一ID
    :param kb_scope: 知识库范围
    :param filename: 原始文件名
    :param display_filename: 展示文件名
    :param stored_relpath: 存储相对路径
    :param file_size: 文件大小（字节）
    :param mime_type: MIME类型
    :param width: 图片宽度
    :param height: 图片高度
    :param format: 图片格式
    :param caption: 预留字段（可选说明文字，当前不入库生成）
    :param embedding_model: 使用的嵌入模型
    :param source_document: 来源文档名
    :param page_number: 所在页码
    :param position_in_doc: 在文档中的位置信息
    :param chunk_id: 关联的向量块ID
    :param parent_chunk_id: 关联的父块ID
    :param root_chunk_id: 关联的根块ID
    :param created_at: 创建时间
    :param updated_at: 更新时间
    """

    __tablename__ = "mg_kb_images"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    
    # 文件信息
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    display_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=True)
    
    # 图片属性
    width: Mapped[int] = mapped_column(Integer, nullable=True)
    height: Mapped[int] = mapped_column(Integer, nullable=True)
    format: Mapped[str] = mapped_column(String(20), nullable=True)
    
    # 语义信息
    caption: Mapped[str] = mapped_column(Text, nullable=True, default="")  # 预留，默认空
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    
    # 关联信息
    source_document: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    # 图片在页面中的位置信息
    position_x: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    position_y: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    position_width: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    position_height: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    
    # 检索相关
    chunk_id: Mapped[str] = mapped_column(String(512), nullable=True, index=True)
    parent_chunk_id: Mapped[str] = mapped_column(String(512), nullable=True, index=True)
    root_chunk_id: Mapped[str] = mapped_column(String(512), nullable=True, index=True)
    
    # 图文关联
    related_text_ids: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)  # 关联的文本块ID列表
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ExpDataset(Base):
    """
    实验数据集（RAG 检索评测）：文档 + 问题集的逻辑分组。
    文档本体复用知识库管线（kb_scope = exp:d{id}，元数据在 mg_kb_documents）。
    id: 主键
    name: 数据集名称
    description: 描述
    doc_count: 已入库文档数（缓存值，随上传/删除更新）
    question_count: 问题数（含 OOD）
    created_by: 创建者用户ID
    created_at/updated_at: 时间戳
    """

    __tablename__ = "mg_exp_datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ExpQuestion(Base):
    """
    实验问题集条目。
    id: 主键
    dataset_id: 所属数据集
    ext_id: 外部 ID（如 RAG_test case id）
    question: 问题文本
    answer: 参考答案（可空）
    gold_file_keys: 目标文档文件名列表（命中判定依据；OOD 题为空）
    stratum: 分层标签（numeric/named/multi 等）
    is_ood: 是否库外拒答题
    created_at: 创建时间
    """

    __tablename__ = "mg_exp_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ext_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    gold_file_keys: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    stratum: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    is_ood: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ExpRun(Base):
    """
    实验运行：一次多配置消融对比。
    id: 主键
    dataset_id: 数据集
    name: 运行名称
    configs: 配置矩阵（JSONB 列表，每项含 retrieval_mode/fusion/rerank/top_k/rrf_k/candidate_multiplier）
    question_limit: 评测题数上限（0 表示全部）
    include_ood: 是否包含 OOD 题
    kind: 任务类型（retrieval=检索消融；qa=端到端问答测评）
    eval_config_idx: 端到端问答评测选中的配置下标（QA 任务固定 0；检索任务为 NULL）
    snapshot: 运行时数据集快照（doc_count/question_count/answer_count）
    status: queued/running/completed/cancelled/failed
    created_by: 创建者用户ID
    error: 失败原因
    created_at/finished_at: 时间戳
    """

    __tablename__ = "mg_exp_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="retrieval")
    configs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    question_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    include_ood: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    eval_config_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class ExpRunResult(Base):
    """
    实验运行逐题结果（run × config × question 一行）。
    id: 主键
    run_id: 运行ID（级联删除）
    config_idx: 配置在 configs 中的下标
    question_id: 问题ID
    is_ood: 冗余标记，便于聚合
    retrieved: 文档级检索结果（JSONB 列表：filename/rank/score/chunk 摘要）
    hit: 是否命中（top_k 内出现任一 gold 文档）
    hit_rank: 首个 gold 文档的文档级排名（未命中为 0）
    reciprocal_rank: 1/hit_rank
    recall: Recall@k（gold 命中占比）
    top1_score: 首位结果分数（OOD 误命中分析）
    max_rerank_score: rerank 最高分（未 rerank 为空）
    rerank_below_min: rerank 分数门控是否判定「无相关资料」
    latency_ms: 检索耗时
    error: 单题失败原因
    answer: 端到端生成答案（仅评测配置；未启用为空）
    answer_latency_ms: 生成调用耗时（不含检索与判分）
    answer_metrics: 答案评测指标（JSONB：em/f1/numeric_hit/correctness/faithfulness/refused/gen_error/judge_error）
    created_at: 创建时间
    """

    __tablename__ = "mg_exp_run_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mg_exp_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    config_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    question_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    is_ood: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retrieved: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hit_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reciprocal_rank: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    recall: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    top1_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rerank_below_min: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answer_metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ChatSession(Base):
    """
    聊天会话模型
    id: 主键
    user_id: 用户ID (必填, 有索引)
    agent_id: 智能体ID (必填, 有索引)
    session_id: 会话ID (必填, 有索引)
    metadata_json: 元数据
    updated_at: 更新时间
    created_at: 创建时间
    确保同一用户同一智能体同一会话只存在一个会话
    与 ChatMessage 是一对多关系, 级联删除(当会话删除时，关联的消息也会自动删除)
    """
    __tablename__ = "mg_chat_sessions"
    __table_args__ = (UniqueConstraint("user_id", "agent_id", "session_id", name="uq_mg_user_agent_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_user_preview: Mapped[str | None] = mapped_column(String(160), nullable=True)
    path_message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    memory_cursor = relationship(
        "ChatMemoryCursor",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ChatMemoryCursor(Base):
    """
    会话记忆归档水位线：已成功写入 Milvus 的最后一轮 turn 索引（从 0 起）。
    与 mg_chat_sessions 一对一，随会话删除级联删除。
    """

    __tablename__ = "mg_chat_memory_cursor"

    session_ref_id: Mapped[int] = mapped_column(
        ForeignKey("mg_chat_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_archived_turn_index: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    session: Mapped[ChatSession] = relationship("ChatSession", back_populates="memory_cursor")


class ChatMessage(Base):
    """
    聊天消息模型
    id: 主键
    session_ref_id: 会话ID (外键, 关联到 mg_chat_sessions.id, 级联删除)
    parent_id: 父消息ID (自引用, 构成会话内消息树; 根消息为空)
    selected_child_id: 当前选中的子消息ID (沿它解析当前分支路径; 叶子为空)
    message_type: 消息类型
    content: 消息内容 (文本)
    content_json: LangChain 消息整块 JSON（含多模态 image_ref 等），优先于 content 回放
    timestamp: 消息时间 (默认值为当前时间)
    rag_trace: 检索增强生成跟踪 (可选)
    rag_steps: 流式检索步骤列表 (可选，用于历史回放)
    error_text: 本条助手消息生成失败时的错误文案 (可选)
    thinking_text: 工具调用前的过渡文本 (可选，用于历史回放思考区)
    通过 session 属性回关到 ChatSession
    """
    __tablename__ = "mg_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_ref_id: Mapped[int] = mapped_column(
        ForeignKey("mg_chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("mg_chat_messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    selected_child_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    rag_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rag_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_references: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 图片引用列表
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 知识库来源列表（与回答中 [来源N] 编号对应）
    thinking_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 工具调用前的过渡文本（思考区内容）
    thinking_items: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 思考区有序时间线：step/text 交错

    session = relationship("ChatSession", back_populates="messages")


class ChatAttachment(Base):
    """
    会话内用户上传附件（与知识库 mg_kb_documents 分离）。
    id: 主键
    user_id: 用户ID (必填, 有索引)
    agent_id: 智能体ID (必填, 有索引)
    session_id: 会话ID (必填, 有索引)
    original_filename: 原始文件名
    stored_relpath: 存储路径
    mime: 文件类型
    size_bytes: 文件大小
    kind: 文件类型
    created_at: 创建时间
    """

    __tablename__ = "mg_chat_attachments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
