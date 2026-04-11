"""
Milvus：密集 + 稀疏混合检索，按 kb_scope 隔离。
用于将加载文档中的文本转换为密集向量和稀疏向量，并存储到 Milvus 中。
"""

from __future__ import annotations

from pymilvus import AnnSearchRequest, DataType, MilvusClient, RRFRanker

from app.settings import settings


def _dense_dim() -> int:
    return max(1, int(settings.EMBEDDING_DIM or 1024))


def milvus_escape(s: str) -> str:
    """
    转义 Milvus 过滤表达式中的特殊字符
    :param s: 字符串
    :return: 转义后的字符串
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def kb_filter_expr(kb_scope: str, extra: str = "") -> str:
    """
    构建知识库过滤表达式
    :param kb_scope: 知识库范围
    :param extra: 额外过滤表达式
    :return: 过滤表达式
    """
    esc = milvus_escape(kb_scope)
    base = f'kb_scope == "{esc}"'
    if extra:
        return f"{base} && {extra}"
    return base


class MilvusManager:
    def __init__(self) -> None:
        """
        初始化 Milvus 管理器
        :return: None 
        """
        self.host = (settings.MILVUS_HOST or "127.0.0.1").strip()
        self.port = (settings.MILVUS_PORT or "19530").strip()
        self.collection_name = (settings.MILVUS_COLLECTION or "mg_agent_kb").strip()
        self.uri = f"http://{self.host}:{self.port}"
        self.client: MilvusClient | None = None

    def _get_client(self) -> MilvusClient:
        """
        获取 Milvus 客户端
        :return: MilvusClient
        """
        if self.client is None:
            self.client = MilvusClient(uri=self.uri)
        return self.client

    def init_collection(self, dense_dim: int | None = None) -> None:
        """
        初始化 Milvus 集合
        创建一个名为 mg_agent_kb 的集合，并添加以下字段：
        - id: 主键
        - dense_embedding: 密集向量
        - sparse_embedding: 稀疏向量
        - kb_scope: 知识库范围
        - text: 文本
        - filename: 文件名
        - file_type: 文件类型
        - file_path: 文件路径
        - page_number: 页码
        - chunk_idx: 分块索引
        - chunk_id: 分块ID
        - parent_chunk_id: 父级分块ID
        - root_chunk_id: 根级分块ID
        - chunk_level: 分块层级
        
        创建索引：
        - dense_embedding: 使用 HNSW 索引
        - sparse_embedding: 使用 SPARSE_INVERTED_INDEX 索引
        
        :param dense_dim: 密集向量维度
        :return: None
        """
        if dense_dim is None:
            dense_dim = _dense_dim()
        client = self._get_client()
        if client.has_collection(self.collection_name):
            return
        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("kb_scope", DataType.VARCHAR, max_length=128)
        schema.add_field("text", DataType.VARCHAR, max_length=2000)
        schema.add_field("filename", DataType.VARCHAR, max_length=512)
        schema.add_field("file_type", DataType.VARCHAR, max_length=50)
        schema.add_field("file_path", DataType.VARCHAR, max_length=1024)
        schema.add_field("page_number", DataType.INT64)
        schema.add_field("chunk_idx", DataType.INT64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("root_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_level", DataType.INT64)

        index_params = client.prepare_index_params()

        """
        Dense 向量使用 HNSW 索引
        Milvus 中的 HNSW 索引是一种多层图的索引结构，基于图结构进行最近邻搜索。
        Layer 0 是根层，Layer 1 是第一层，以此类推。
        Layer 0 的节点数为 M，Layer 1 的节点数为 M/2，以此类推。
        图中的每个节点存储的是向量和指向下一层的指针。
        图中的每条边连接了当前节点相似的邻居向量节点。
        从根层开始，逐层搜索，每一层找到局部最相似的节点，并作为下一层的入口，直到找到最近的邻居。
        直到 Layer 0 时，维护一个大小为 ef 的候选队列。从上一层的入口节点开始，不断展开邻居，最多保留 ef 个候选节点。
        """
        index_params.add_index(
            field_name="dense_embedding",
            index_type="HNSW",  # 使用 HNSW 索引
            metric_type="IP",  # 使用 IP 距离
            params={"M": 16, "efConstruction": 256},  # M: 节点最多连接的相似节点; efConstruction: 维护的候选队列大小
        )

        """
        Sparse 向量使用 SPARSE_INVERTED_INDEX 索引
        SPARSE_INVERTED_INDEX 索引是一种基于倒排索引的稀疏向量索引。
        """
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",  # 使用 SPARSE_INVERTED_INDEX 索引
            metric_type="IP",  # 使用 IP 距离
            params={"drop_ratio_build": 0.2},  # drop_ratio_build: 构建索引时，丢弃的稀疏向量比例
        )

        # 创建集合
        client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )

    def insert(self, data: list[dict]) -> None:
        """
        插入数据
        :param data: 数据列表，每个元素是一个字典，字典中包含以下字段：
            - id: 主键
            - dense_embedding: 密集向量
            - sparse_embedding: 稀疏向量
            - kb_scope: 知识库范围
            - text: 文本
            - filename: 文件名
            - file_type: 文件类型
            - file_path: 文件路径
            - page_number: 页码
            - chunk_idx: 分块索引
            - chunk_id: 分块ID
            - parent_chunk_id: 父级分块ID
            - root_chunk_id: 根级分块ID
            - chunk_level: 分块层级
        :return: None
        """
        self._get_client().insert(self.collection_name, data)

    def query(
        self,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
        limit: int = 10000,
    ) -> list:
        """
        查询数据
        :param filter_expr: 过滤表达式
        :param output_fields: 输出字段列表
        :param limit: 限制返回的记录数
        :return: 数据列表
        """
        return self._get_client().query(
            collection_name=self.collection_name,
            filter=filter_expr,
            output_fields=output_fields or ["filename", "file_type", "kb_scope"],
            limit=limit,
        )

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """
        根据分块ID查询数据
        :param chunk_ids: 分块ID列表
        :return: 数据列表
        """
        ids = [x for x in chunk_ids if x]
        if not ids:
            return []
        quoted = ", ".join([f'"{milvus_escape(item)}"' for item in ids])
        filter_expr = f"chunk_id in [{quoted}]"
        return self.query(
            filter_expr=filter_expr,
            output_fields=[
                "text",
                "filename",
                "file_type",
                "page_number",
                "chunk_id",
                "parent_chunk_id",
                "root_chunk_id",
                "chunk_level",
                "chunk_idx",
                "kb_scope",
            ],
            limit=len(ids),
        )

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        sparse_embedding: dict,
        top_k: int,
        filter_expr: str,
        rrf_k: int = 60,
    ) -> list[dict]:
        """
        混合检索
        :param dense_embedding: 密集向量
        :param sparse_embedding: 稀疏向量
        :param top_k: 限制返回的记录数
        :param filter_expr: 过滤表达式
        :param rrf_k: RRF 参数
        :return: 数据列表
        """
        output_fields = [
            "text",
            "filename",
            "file_type",
            "page_number",
            "chunk_id",
            "parent_chunk_id",
            "root_chunk_id",
            "chunk_level",
            "chunk_idx",
            "kb_scope",
        ]
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

        """
        RRF 重排序
        RRF 是一种基于 Rerank 的排序算法，用于优化混合检索结果。
        """
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
                        "filename": hit.get("filename", ""),
                        "file_type": hit.get("file_type", ""),
                        "page_number": hit.get("page_number", 0),
                        "chunk_id": hit.get("chunk_id", ""),
                        "parent_chunk_id": hit.get("parent_chunk_id", ""),
                        "root_chunk_id": hit.get("root_chunk_id", ""),
                        "chunk_level": hit.get("chunk_level", 0),
                        "chunk_idx": hit.get("chunk_idx", 0),
                        "kb_scope": hit.get("kb_scope", ""),
                        "score": hit.get("distance", 0.0),
                    }
                )
        return formatted

    def dense_retrieve(
        self,
        dense_embedding: list[float],
        top_k: int,
        filter_expr: str,
    ) -> list[dict]:
        """
        密集检索
        :param dense_embedding: 密集向量
        :param top_k: 限制返回的记录数
        :param filter_expr: 过滤表达式
        :return: 数据列表
        """
        results = self._get_client().search(
            collection_name=self.collection_name,
            data=[dense_embedding],
            anns_field="dense_embedding",
            search_params={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k,
            output_fields=[
                "text",
                "filename",
                "file_type",
                "page_number",
                "chunk_id",
                "parent_chunk_id",
                "root_chunk_id",
                "chunk_level",
                "chunk_idx",
                "kb_scope",
            ],
            filter=filter_expr,
        )
        formatted: list[dict] = []
        for hits in results:
            for hit in hits:
                ent = hit.get("entity", {}) or {}
                formatted.append(
                    {
                        "id": hit.get("id"),
                        "text": ent.get("text", ""),
                        "filename": ent.get("filename", ""),
                        "file_type": ent.get("file_type", ""),
                        "page_number": ent.get("page_number", 0),
                        "chunk_id": ent.get("chunk_id", ""),
                        "parent_chunk_id": ent.get("parent_chunk_id", ""),
                        "root_chunk_id": ent.get("root_chunk_id", ""),
                        "chunk_level": ent.get("chunk_level", 0),
                        "chunk_idx": ent.get("chunk_idx", 0),
                        "kb_scope": ent.get("kb_scope", ""),
                        "score": hit.get("distance", 0.0),
                    }
                )
        return formatted

    def delete(self, filter_expr: str) -> None:
        """
        删除数据
        根据过滤表达式删除 Milvus 中的数据
        :param filter_expr: 过滤表达式
        :return: None
        """
        self._get_client().delete(collection_name=self.collection_name, filter=filter_expr)

    def has_collection(self) -> bool:
        """
        判断集合是否存在
        :return: 是否存在
        """
        return self._get_client().has_collection(self.collection_name)
