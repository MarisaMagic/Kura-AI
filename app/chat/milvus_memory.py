"""
会话记忆专用 Milvus 集合：与知识库 kura_ai_kb 完全隔离。
"""

from __future__ import annotations

import logging

from pymilvus import AnnSearchRequest, DataType, MilvusClient, RRFRanker

from app.kb.milvus_client import _dense_dim, milvus_escape
from app.settings import settings


def _text_varchar_max() -> int:
    return max(512, int(getattr(settings, "CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH", 8192) or 8192))


def memory_filter_expr(memory_scope: str, *, turn_keys: list[int] | None = None) -> str:
    """
    会话记忆检索过滤：按会话隔离；给定 turn_keys 时只命中这些轮次（当前路径上的已归档轮）。
    turn_keys 为空列表时命中不到任何行（-1 是不存在的 turn_key）。
    """
    esc = milvus_escape(memory_scope)
    expr = f'memory_scope == "{esc}"'
    if turn_keys is not None:
        keys = ",".join(str(int(k)) for k in turn_keys) or "-1"
        expr += f" && turn_key in [{keys}]"
    return expr


def _read_collection_field_names(client: MilvusClient, collection_name: str) -> set[str]:
    """从 describe_collection 解析字段名集合，失败返回空集合。"""
    try:
        info = client.describe_collection(collection_name=collection_name)
    except Exception:
        return set()
    if not isinstance(info, dict):
        return set()
    return {
        str(f.get("name"))
        for f in (info.get("fields") or [])
        if isinstance(f, dict) and f.get("name")
    }


def _read_collection_dense_dim(client: MilvusClient, collection_name: str) -> int | None:
    """从 describe_collection 解析 dense_embedding 的 dim，失败返回 None。"""
    try:
        info = client.describe_collection(collection_name=collection_name)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    for f in info.get("fields") or []:
        if not isinstance(f, dict) or f.get("name") != "dense_embedding":
            continue
        dim = (f.get("params") or {}).get("dim")
        if dim is None:
            return None
        try:
            return int(dim)
        except (TypeError, ValueError):
            return None
    return None


class ChatMemoryMilvusManager:
    def __init__(self) -> None:
        self.host = (settings.MILVUS_HOST or "127.0.0.1").strip()
        self.port = (settings.MILVUS_PORT or "19530").strip()
        self.collection_name = (settings.MILVUS_COLLECTION_CHAT_MEMORY or "kura_ai_chat_memory").strip()
        self.uri = f"http://{self.host}:{self.port}"
        self.client: MilvusClient | None = None

    def _get_client(self) -> MilvusClient:
        if self.client is None:
            self.client = MilvusClient(uri=self.uri)
        return self.client

    def init_collection(self, dense_dim: int | None = None) -> None:
        """
        初始化会话记忆集合
        :param dense_dim: 密集向量维度
        :return: None
        创建集合：
        - id: 主键
        - dense_embedding: 密集向量
        - sparse_embedding: 稀疏向量
        - memory_scope: 会话记忆范围
        - text: 文本
        - turn_index: 轮次索引
        - chunk_index: 分块索引
        - chunk_id: 分块ID
        创建索引：
        - dense_embedding: 使用 HNSW 索引
        - sparse_embedding: 使用 SPARSE_INVERTED_INDEX 索引
        """
        if dense_dim is None:
            dense_dim = _dense_dim()
        client = self._get_client()
        if client.has_collection(self.collection_name):
            current = _read_collection_dense_dim(client, self.collection_name)
            field_names = _read_collection_field_names(client, self.collection_name)
            if current == dense_dim and "turn_key" in field_names:
                return
            if current is not None:
                logging.getLogger(__name__).warning(
                    "会话记忆 Milvus 集合 %s 的 schema 与当前定义不一致（维度=%s，含 turn_key=%s），将删除后重建（历史记忆向量会清空，后续按 PG 原文惰性重归档）",
                    self.collection_name,
                    current,
                    "turn_key" in field_names,
                )
                self.drop_collection()
            else:
                logging.getLogger(__name__).warning(
                    "已存在集合 %s 但无法解析 dense_embedding 维度，未自动重建；若 Milvus 报 vector dimension mismatch 请手动 drop 该集合或临时设置 CHAT_MEMORY_MILVUS_RECREATE_ON_INIT=true",
                    self.collection_name,
                )
                return
        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("memory_scope", DataType.VARCHAR, max_length=256)
        schema.add_field("text", DataType.VARCHAR, max_length=_text_varchar_max())
        schema.add_field("turn_index", DataType.INT64)
        schema.add_field("turn_key", DataType.INT64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense_embedding",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 256},
        )
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"drop_ratio_build": 0.2},
        )

        client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )

    def insert(self, data: list[dict]) -> None:
        self._get_client().insert(self.collection_name, data)

    def delete_by_scope(self, memory_scope: str) -> None:
        expr = memory_filter_expr(memory_scope)
        self._get_client().delete(collection_name=self.collection_name, filter=expr)

    def collection_exists(self) -> bool:
        return self._get_client().has_collection(self.collection_name)

    def drop_collection(self) -> None:
        c = self._get_client()
        if c.has_collection(self.collection_name):
            c.drop_collection(self.collection_name)

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        sparse_embedding: dict,
        top_k: int,
        filter_expr: str,
        rrf_k: int = 60,
    ) -> list[dict]:
        output_fields = ["text", "turn_index", "turn_key", "chunk_index", "chunk_id", "memory_scope"]
        dense_search = AnnSearchRequest(
            data=[dense_embedding],
            anns_field="dense_embedding",
            param={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k * 2,
            expr=filter_expr,
        )
        sparse_search = AnnSearchRequest(
            data=[sparse_embedding],
            anns_field="sparse_embedding",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=top_k * 2,
            expr=filter_expr,
        )
        reranker = RRFRanker(k=rrf_k)
        results = self._get_client().hybrid_search(
            collection_name=self.collection_name,
            reqs=[dense_search, sparse_search],
            ranker=reranker,
            limit=top_k,
            output_fields=output_fields,
        )
        formatted: list[dict] = []
        for hits in results:
            for hit in hits:
                formatted.append(
                    {
                        "id": hit.get("id"),
                        "text": hit.get("text", ""),
                        "turn_index": int(hit.get("turn_index", 0) or 0),
                        "turn_key": int(hit.get("turn_key", 0) or 0),
                        "chunk_index": int(hit.get("chunk_index", 0) or 0),
                        "chunk_id": hit.get("chunk_id", ""),
                        "memory_scope": hit.get("memory_scope", ""),
                        "score": hit.get("distance", 0.0),
                    }
                )
        return formatted


_mgr: ChatMemoryMilvusManager | None = None


def get_chat_memory_milvus() -> ChatMemoryMilvusManager:
    global _mgr
    if _mgr is None:
        _mgr = ChatMemoryMilvusManager()
    return _mgr


def init_chat_memory_collection() -> None:
    import logging

    mgr = get_chat_memory_milvus()
    if getattr(settings, "CHAT_MEMORY_MILVUS_RECREATE_ON_INIT", False):
        try:
            mgr.drop_collection()
        except Exception:
            logging.getLogger(__name__).warning("drop chat memory collection failed", exc_info=True)
    mgr.init_collection()
