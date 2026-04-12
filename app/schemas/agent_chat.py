from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """
    智能体对话请求
    :param agent_id: 智能体 ID
    :param message: 用户消息
    :param session_id: 会话 ID，前端生成
    """
    agent_id: int = Field(..., description="智能体 ID")
    message: str = Field(..., min_length=1, description="用户消息")
    session_id: Optional[str] = Field("default_session", description="会话 ID，前端生成")
    use_knowledge_retrieval: bool = Field(
        True,
        description="为 True 时允许知识库检索工具；为 False 时仅通用知识回答",
    )


class ChatResponse(BaseModel):
    """
    智能体对话响应
    :param response: 响应消息
    :param rag_trace: RAG 追踪信息
    """
    response: str
    rag_trace: Optional[dict[str, Any]] = None


class MessageInfo(BaseModel):
    """
    消息信息
    :param type: 消息类型
    :param content: 消息内容
    :param timestamp: 消息时间戳
    :param rag_trace: RAG 追踪信息
    :param rag_steps: 检索步骤（与 SSE rag_step 一致，用于历史回放）
    """
    type: str
    content: str
    timestamp: str
    rag_trace: Optional[dict[str, Any]] = None
    rag_steps: Optional[list[dict[str, Any]]] = None


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
