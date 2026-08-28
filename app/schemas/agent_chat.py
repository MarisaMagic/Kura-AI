from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    """
    智能体对话请求
    :param agent_id: 智能体 ID
    :param message: 用户消息
    :param session_id: 会话 ID，前端生成
    :param use_knowledge_retrieval: 为 True 时允许知识库检索工具；为 False 时仅通用知识回答
    :param attachment_ids: 本会话已上传附件 ID 列表
    :param regenerate: 为 True 时重新生成最后一轮助手回复（不新增用户消息；附件以存储中的最后一条用户消息为准）
    """
    agent_id: int = Field(..., description="智能体 ID")
    message: str = Field("", description="用户消息；可与 attachment_ids 二选一或同时使用")
    session_id: Optional[str] = Field("default_session", description="会话 ID，前端生成")
    use_knowledge_retrieval: bool = Field(
        True,
        description="为 True 时允许知识库检索工具；为 False 时仅通用知识回答",
    )
    attachment_ids: list[str] = Field(default_factory=list, description="本会话已上传附件 ID 列表")
    regenerate: bool = Field(
        False,
        description="为 True 时重新生成最后一轮助手回复（不新增用户消息；附件以存储中的最后一条用户消息为准）",
    )

    @field_validator("attachment_ids")
    @classmethod
    def _limit_attachment_ids(cls, v: list[str]) -> list[str]:
        from app.settings import settings

        mx = int(getattr(settings, "CHAT_UPLOAD_MAX_FILES_PER_MESSAGE", 5))
        if len(v) > mx:
            raise ValueError(f"单条消息引用附件数量不能超过 {mx}")
        return v

    @model_validator(mode="after")
    def _message_or_attachments(self) -> "ChatRequest":
        if self.regenerate:
            return self  # 如果 regenerate 为 True，则返回当前对象
        if not (self.message or "").strip() and not self.attachment_ids:
            raise ValueError("message 与 attachment_ids 至少填写一项")
        return self


class ChatAttachmentUploadResponse(BaseModel):
    """
    会话附件上传结果
    :param id: 附件 ID
    :param filename: 附件文件名
    :param kind: 附件类型
    :param mime: 附件 MIME 类型
    :param size_bytes: 附件大小（字节）
    """

    id: str
    filename: str
    kind: str
    mime: str
    size_bytes: int


class ChatResponse(BaseModel):
    """
    智能体对话响应
    :param response: 响应消息
    :param rag_trace: RAG 追踪信息
    :param kb_preselect: 知识库前置选档元信息（本回合在智能体前自动圈定的 file_key 等）
    """
    response: str
    rag_trace: Optional[dict[str, Any]] = None
    sources: Optional[list[dict[str, Any]]] = None
    kb_preselect: Optional[dict[str, Any]] = None


class MessageInfo(BaseModel):
    """
    消息信息
    :param type: 消息类型
    :param content: 消息内容（文本预览）
    :param content_json: LangChain 消息整块 JSON（多模态 image_ref 等）
    :param timestamp: 消息时间戳
    :param rag_trace: RAG 追踪信息
    :param rag_steps: 检索步骤（与 SSE rag_step 一致，用于历史回放）
    :param error_text: 助手消息生成失败时的错误说明（可选，用于历史展示）
    :param thinking_text: 工具调用前的过渡文本（可选，用于历史回放思考区）
    """
    type: str
    content: Any = ""
    content_json: Optional[dict[str, Any]] = None
    timestamp: str
    rag_trace: Optional[dict[str, Any]] = None
    rag_steps: Optional[list[dict[str, Any]]] = None
    error_text: Optional[str] = None
    sources: Optional[list[dict[str, Any]]] = None
    thinking_text: Optional[str] = None


class SessionMessagesResponse(BaseModel):
    """
    会话消息响应
    :param messages: 消息列表
    """
    messages: list[MessageInfo]


class SessionInfo(BaseModel):
    """
    会话信息
    :param session_id: 会话 ID
    :param updated_at: 更新时间
    :param message_count: 消息数量
    :param last_user_preview: 最后用户预览
    :param agent_id: 智能体 ID
    :param agent_name: 智能体名称
    :param updated_at_display: 更新时间展示
    """
    session_id: str
    updated_at: str
    message_count: int
    last_user_preview: str = ""
    agent_id: int | None = None
    agent_name: str = ""
    updated_at_display: str = ""


class SessionListResponse(BaseModel):
    """
    会话列表响应
    :param sessions: 会话列表
    :param total: 总数量
    :param has_more: 是否有更多
    """
    sessions: list[SessionInfo]
    total: int = 0
    has_more: bool = False


class SessionDeleteResponse(BaseModel):
    """
    会话删除响应
    :param session_id: 会话 ID
    :param message: 消息
    """
    session_id: str
    message: str


class ChatJobCreateResponse(BaseModel):
    """创建异步对话 Job 的响应（刷新后可按 job_id 重连 SSE）。"""

    job_id: str
