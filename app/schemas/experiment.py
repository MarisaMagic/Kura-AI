"""实验平台 API 模型。"""

from __future__ import annotations

from typing import Any, List

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
    :param name: 运行名称（可空，自动生成）
    :param configs: 消融配置矩阵
    :param question_limit: 库内题数量上限（0 为全部）
    :param include_ood: 是否包含 OOD 拒答题
    """

    dataset_id: int = Field(..., description="数据集ID")
    name: str = Field("", max_length=128, description="运行名称")
    configs: List[dict[str, Any]] = Field(..., min_length=1, description="消融配置列表")
    question_limit: int = Field(0, ge=0, le=100000, description="评测题数上限，0 为全部")
    include_ood: bool = Field(False, description="是否包含 OOD 拒答题")
