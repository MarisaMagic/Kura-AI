from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    agent_id: int = Field(..., description="智能体 ID")
    message: str = Field(..., min_length=1, description="用户消息")
    session_id: Optional[str] = Field("default_session", description="会话 ID，前端生成")


class ChatResponse(BaseModel):
    response: str
    rag_trace: Optional[dict[str, Any]] = None


class MessageInfo(BaseModel):
    type: str
    content: str
    timestamp: str
    rag_trace: Optional[dict[str, Any]] = None


class SessionMessagesResponse(BaseModel):
    messages: list[MessageInfo]


class SessionInfo(BaseModel):
    session_id: str
    updated_at: str
    message_count: int
    last_user_preview: str = ""
    agent_id: int | None = None
    agent_name: str = ""
    updated_at_display: str = ""


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]
    total: int = 0
    has_more: bool = False


class SessionDeleteResponse(BaseModel):
    session_id: str
    message: str
