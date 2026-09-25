"""实验平台 API 模型。"""

from __future__ import annotations

from typing import Any, List, Literal

from pydantic import BaseModel, Field


class ExpDatasetCreate(BaseModel):
    """
    创建实验数据集
    :param name: 数据集名称
    :param description: 描述
    """

    name: str = Field(..., min_length=1, max_length=128, description="数据集名称")
    description: str = Field("", max_length=2000, description="描述")


class ExpRunCreate(BaseModel):
    """
    创建实验运行
    :param dataset_id: 数据集ID
    :param name: 运行名称（可空，自动生成「{数据集名} · 检索消融/问答测评 #N」）
    :param kind: 任务类型（retrieval=检索策略消融；qa=端到端问答测评）
    :param configs: 消融配置矩阵（qa 必须且只能 1 组）
    :param question_limit: 库内题数量上限（0 为全部）
    :param include_ood: 是否包含 OOD 拒答题
    """

    dataset_id: int = Field(..., description="数据集ID")
    name: str = Field("", max_length=128, description="运行名称")
    kind: Literal["retrieval", "qa"] = Field("retrieval", description="任务类型")
    configs: List[dict[str, Any]] = Field(..., min_length=1, description="配置列表")
    question_limit: int = Field(0, ge=0, le=100000, description="评测题数上限，0 为全部")
    include_ood: bool = Field(False, description="是否包含 OOD 拒答题")


class ExpDocsDelete(BaseModel):
    """
    批量删除实验文档（按前端当前筛选结果传文件名）
    :param dataset_id: 数据集ID
    :param filenames: 展示文件名列表（单次上限 5000）
    """

    dataset_id: int = Field(..., description="数据集ID")
    filenames: List[str] = Field(..., min_length=1, max_length=5000, description="展示文件名列表")
