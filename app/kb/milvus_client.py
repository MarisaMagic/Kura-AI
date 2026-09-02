"""
Milvus：密集 + 稀疏混合检索，按 kb_scope 隔离。
用于将加载文档中的文本转换为密集向量和稀疏向量，并存储到 Milvus 中。
"""

from __future__ import annotations

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker

from app.settings import settings

# Milvus 单请求 query 的 (offset+limit) 上界，超过会报 invalid max query result window
MILVUS_MAX_QUERY_WINDOW = 16384


def milvus_client_kwargs() -> dict:
    """MilvusClient 连接参数。已开鉴权的实例可设 MILVUS_TOKEN。"""
    host = (settings.MILVUS_HOST or "127.0.0.1").strip()
    port = (settings.MILVUS_PORT or "19530").strip()
    kw: dict = {"uri": f"http://{host}:{port}"}
    token = (getattr(settings, "MILVUS_TOKEN", None) or "").strip()
    if token:
        kw["token"] = token
    return kw


def _milvus_query_row_to_dict(row: object) -> dict:
    if isinstance(row, dict):
        return row
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        return to_dict()  # type: ignore[no-any-return]
    return {}


def _dense_dim() -> int:
    return max(1, int(settings.EMBEDDING_DIM or 1024))


def milvus_escape(s: str) -> str:
    """
    转义 Milvus 过滤表达式中的特殊字符
    :param s: 字符串
    :return: 转义后的字符串
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_content_type(raw: object, chunk_level: object) -> str:
    """
    Milvus 中 content_type 可能为缺失或空串，此时 .get('content_type', 'text') 仍会得到 ''，
    下游若用 == 'text' / == 'image' 分类，会导致两类皆空、检索结果全部被丢弃。
    """
    s = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if s in ("text", "image"):
        return s
    try:
        lv = int(chunk_level or 0)
    except (TypeError, ValueError):
        lv = 0
    if lv == 4:
        return "image"
    return "text"


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


def filename_in_filter_expr(filenames: list[str]) -> str:
    """
    将多文档约束拼成 Milvus 布尔表达式：filename in ["a","b"] 或单文件 filename == "a"
    调用方需保证列表非空且已规范（不含重复）。
    """
    if not filenames:
        raise ValueError("filename_in_filter_expr requires at least one filename")
    if len(filenames) == 1:
        return f'filename == "{milvus_escape(filenames[0])}"'
    parts = ", ".join(f'"{milvus_escape(fn)}"' for fn in filenames)
    return f"filename in [{parts}]"


class MilvusManager:
    def __init__(self) -> None:
        """
        初始化 Milvus 管理器
        :return: None 
        """
        self.host = (settings.MILVUS_HOST or "127.0.0.1").strip()
        self.port = (settings.MILVUS_PORT or "19530").strip()
        self.collection_name = (settings.MILVUS_COLLECTION or "kura_ai_kb").strip()
        self.uri = f"http://{self.host}:{self.port}"
        self.client: MilvusClient | None = None

    def _get_client(self) -> MilvusClient:
        """
        获取 Milvus 客户端
        :return: MilvusClient
        """
        if self.client is None:
            self.client = MilvusClient(**milvus_client_kwargs())
        return self.client

    def init_collection(self, dense_dim: int | None = None, *, collection_name: str | None = None) -> None:
        """
        初始化 Milvus 集合
        创建一个名为 kura_ai_kb 的集合，并添加以下字段：
        - id: 主键
        - dense_embedding: 密集向量
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
        - content_type: 内容类型（text/image）
        - image_path: 图片路径（图片块才有）
        - position_start: 文本在页内的起始位置（文本块才有）
        - position_end: 文本在页内的结束位置（文本块才有）
        - image_position_x: 图片位置X坐标（图片块才有）
        - image_position_y: 图片位置Y坐标（图片块才有）
        - image_width: 图片宽度（图片块才有）
        - image_height: 图片高度（图片块才有）

        稀疏向量不使用本地 BM25 词表，改为服务端 BM25 Function（bm25_fn）：
        以 text 字段为输入、bm25_sparse 为输出，由 Milvus 在写入时自动计算，
        查询侧直接用文本发起 BM25 检索，彻底消除客户端词表漂移/IDF 失真问题。
        analyzer 使用 standard（英文按词切分、中文按单字切分，与原有单字 BM25 行为一致）。

        索引：
        - dense_embedding: 使用 HNSW 索引
        - bm25_sparse: SPARSE_INVERTED_INDEX（BM25 度量；本版本加载集合要求向量字段均须有索引）

        :param dense_dim: 密集向量维度
        :param collection_name: 目标集合名，缺省用 self.collection_name（供迁移脚本创建临时集合）
        :return: None
        """
        if dense_dim is None:
            dense_dim = _dense_dim()
        name = collection_name or self.collection_name
        client = self._get_client()
        if client.has_collection(name):
            return
        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
        schema.add_field("kb_scope", DataType.VARCHAR, max_length=128)
        schema.add_field("text", DataType.VARCHAR, max_length=2000, enable_analyzer=True)
        schema.add_field("filename", DataType.VARCHAR, max_length=512)
        schema.add_field("file_type", DataType.VARCHAR, max_length=50)
        schema.add_field("file_path", DataType.VARCHAR, max_length=1024)
        schema.add_field("page_number", DataType.INT64)
        schema.add_field("chunk_idx", DataType.INT64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("root_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_level", DataType.INT64)
        schema.add_field("content_type", DataType.VARCHAR, max_length=20)
        schema.add_field("image_path", DataType.VARCHAR, max_length=1024)
        # 文本块位置字段
        schema.add_field("position_start", DataType.INT64)
        schema.add_field("position_end", DataType.INT64)
        # 图片位置字段
        schema.add_field("image_position_x", DataType.INT64)
        schema.add_field("image_position_y", DataType.INT64)
        schema.add_field("image_width", DataType.INT64)
        schema.add_field("image_height", DataType.INT64)
        # BM25 Function 的输出字段（稀疏向量，无需建索引，由服务端在写入时基于 text 计算）
        schema.add_field("bm25_sparse", DataType.SPARSE_FLOAT_VECTOR)

        # 服务端 BM25 稀疏向量（写入时基于 text 自动计算）
        # 注意：本 Milvus 版本（2.5.x）的 BM25 Function 不接受任何 params（analyzer 配置需 2.6+），
        # 使用内置 standard 分词器（英文按词、中文按 unicode 词组切分；中文精确单字召回由 dense 腿兜底）。
        bm25_function = Function(
            name="bm25_fn",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names="bm25_sparse",
        )
        schema.add_function(bm25_function)

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
        # BM25 Function 输出字段必须建 SPARSE_INVERTED_INDEX（本版本集合加载要求向量字段有索引）
        index_params.add_index(
            field_name="bm25_sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )

        # 创建集合
        client.create_collection(
            collection_name=name,
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

    def list_distinct_filenames(self, kb_scope: str, *, query_row_limit: int | None = None) -> list[str]:
        """
        按 kb_scope 查询 Milvus 中出现过的不重复 filename（用于智能体选档白名单）。
        单次标量 query 的 limit 受 MILVUS_MAX_QUERY_WINDOW 约束，大量分块时用 query_iterator 逐批拉取再去重。
        用于智能体在检索知识库之前选定文档候选范围。
        :param kb_scope: 知识库范围
        :param query_row_limit: 最多扫描的实体行数；None 表示扫到匹配集结束。需控制超大规模库成本时可设上限（如 65535）。
        :return: 文件名列表
        """
        esc = milvus_escape(kb_scope)
        filter_expr = f'kb_scope == "{esc}"'
        client = self._get_client()
        seen: set[str] = set()
        scanned = 0
        iterator = None
        try:
            if getattr(client, "query_iterator", None) is not None:
                # 使用 query_iterator 逐批拉取再去重
                iterator = client.query_iterator(
                    collection_name=self.collection_name,
                    batch_size=min(2048, MILVUS_MAX_QUERY_WINDOW - 1),
                    filter=filter_expr,
                    output_fields=["filename"],
                )
                stop = False
                while not stop:
                    if query_row_limit is not None and scanned >= query_row_limit:
                        break
                    batch = iterator.next()
                    if not batch:
                        break
                    for r in batch:
                        if query_row_limit is not None and scanned >= query_row_limit:
                            stop = True
                            break
                        scanned += 1
                        d = _milvus_query_row_to_dict(r)
                        fn = (d.get("filename") or "").strip()
                        if fn:
                            seen.add(fn)
            else:
                cap = (
                    min(MILVUS_MAX_QUERY_WINDOW, query_row_limit)
                    if query_row_limit is not None
                    else MILVUS_MAX_QUERY_WINDOW
                )
                # 使用 query 一次性拉取再去重
                rows = client.query(
                    collection_name=self.collection_name,
                    filter=filter_expr,
                    output_fields=["filename"],
                    limit=max(1, int(cap)),
                )
                for r in rows or []:
                    d = _milvus_query_row_to_dict(r)
                    fn = (d.get("filename") or "").strip()
                    if fn:
                        seen.add(fn)
        finally:
            if iterator is not None:
                try:
                    iterator.close()
                except Exception:
                    pass
        return sorted(seen)

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
        query_text: str,
        top_k: int,
        filter_expr: str,
        rrf_k: int = 60,
    ) -> list[dict]:
        """
        混合检索：dense（HNSW）+ 服务端 BM25（bm25_fn，输入为查询文本），RRF 融合。
        :param dense_embedding: 密集向量
        :param query_text: 查询文本（作为 BM25 Function 的输入）
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
            "content_type",  # 文本或图片
            "image_path",    # 图片路径
            "position_start", "position_end",  # 文本位置
            "image_position_x", "image_position_y", "image_width", "image_height",  # 图片位置
        ]
        dense_search = AnnSearchRequest(
            data=[dense_embedding],
            anns_field="dense_embedding",
            param={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k * 2,
            expr=filter_expr,
        )
        # BM25 稀疏腿：直接用文本发起，服务端经 bm25_fn 对查询文本计算稀疏向量并召回
        sparse_search = AnnSearchRequest(
            data=[query_text],
            anns_field="bm25_sparse",
            param={"metric_type": "BM25"},
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
                cl = hit.get("chunk_level", 0)
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
                        "content_type": _normalize_content_type(hit.get("content_type"), cl),
                        "image_path": hit.get("image_path", ""),
                        "position_start": hit.get("position_start", 0),
                        "position_end": hit.get("position_end", 0),
                        "image_position_x": hit.get("image_position_x", 0),
                        "image_position_y": hit.get("image_position_y", 0),
                        "image_width": hit.get("image_width", 0),
                        "image_height": hit.get("image_height", 0),
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
                "content_type",  # 文本或图片
                "image_path",    # 图片路径
                "position_start", "position_end",  # 文本位置
                "image_position_x", "image_position_y", "image_width", "image_height",  # 图片位置
            ],
            filter=filter_expr,
        )
        formatted: list[dict] = []
        for hits in results:
            for hit in hits:
                ent = hit.get("entity", {}) or {}
                cl = ent.get("chunk_level", 0)
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
                        "content_type": _normalize_content_type(ent.get("content_type"), cl),
                        "image_path": ent.get("image_path", ""),
                        "position_start": ent.get("position_start", 0),
                        "position_end": ent.get("position_end", 0),
                        "image_position_x": ent.get("image_position_x", 0),
                        "image_position_y": ent.get("image_position_y", 0),
                        "image_width": ent.get("image_width", 0),
                        "image_height": ent.get("image_height", 0),
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
