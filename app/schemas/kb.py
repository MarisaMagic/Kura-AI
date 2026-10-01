"""知识库 API 模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class KbDocumentItem(BaseModel):
    """
    知识库文档项
    :param display_filename: 原始上传文件名
    :param file_type: 文件类型
    :param chunk_count: 分块数量
    :param updated_at: 更新时间
    """
    display_filename: str = Field(..., description="原始上传文件名")
    file_type: str = ""
    chunk_count: int = 0
    updated_at: Optional[str] = None


class KbDocumentListResponse(BaseModel):
    """
    知识库文档列表响应
    :param documents: 知识库文档列表
    """
    documents: List[KbDocumentItem]


class KbUploadResponse(BaseModel):
    """
    知识库文档上传响应
    :param display_filename: 原始上传文件名
    :param chunk_count: 分块数量
    :param parent_chunks: 父级分块数量
    :param message: 消息
    """
    display_filename: str
    chunk_count: int
    parent_chunks: int
    message: str = ""
    unchanged: Optional[bool] = None


class KbUploadTaskResponse(BaseModel):
    """
    知识库上传受理响应：处理在后台执行，返回任务 ID 供前端轮询进度
    :param task_id: 上传任务 ID
    """
    task_id: str


class KbDeleteResponse(BaseModel):
    """
    知识库文档删除响应
    :param display_filename: 原始上传文件名
    :param message: 消息
    """
    display_filename: str
    message: str = "ok"


class KbDocsDelete(BaseModel):
    """
    批量删除知识库文档（按前端当前筛选结果传文件名）
    :param agent_id: 智能体ID
    :param filenames: 展示文件名列表（单次上限 5000）
    """

    agent_id: int = Field(..., description="智能体ID")
    filenames: List[str] = Field(..., min_length=1, max_length=5000, description="展示文件名列表")
