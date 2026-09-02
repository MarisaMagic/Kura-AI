"""
会话记忆专用 Milvus 集合：与知识库 kura_ai_kb 完全隔离。
稀疏检索走服务端 BM25 Function（bm25_fn），与知识库一致。
"""

from __future__ import annotations

import logging

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker

from app.kb.milvus_client import _dense_dim, milvus_client_kwargs, milvus_escape
from app.settings import settings

logger = logging.getLogger(__name__)


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


def _describe_collection(client: MilvusClient, collection_name: str) -> dict:
    try:
        info = client.describe_collection(collection_name=collection_name)
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _field_names_from_desc(info: dict) -> set[str]:
    return {
        str(f.get("name"))
        for f in (info.get("fields") or [])
        if isinstance(f, dict) and f.get("name")
    }


def _dense_dim_from_desc(info: dict) -> int | None:
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


def _has_bm25_function(info: dict) -> bool:
    for fn in info.get("functions") or []:
        if not isinstance(fn, dict):
            continue
        ftype_raw = fn.get("function_type") or fn.get("type")
        label = f"{fn.get('name') or ''} {getattr(ftype_raw, '__class__', type(ftype_raw)).__name__} {repr(ftype_raw)}".lower()
        if "bm25" in label:
            return True
    return False


class ChatMemoryMilvusManager:
    def __init__(self) -> None:
        self.host = (settings.MILVUS_HOST or "127.0.0.1").strip()
        self.port = (settings.MILVUS_PORT or "19530").strip()
        self.collection_name = (settings.MILVUS_COLLECTION_CHAT_MEMORY or "kura_ai_chat_memory").strip()
        self.uri = f"http://{self.host}:{self.port}"
        self.client: MilvusClient | None = None
        # 热路径禁止每次检索 describe_collection
        self._init_done = False
        self._has_bm25 = False

    def _get_client(self) -> MilvusClient:
        if self.client is None:
            self.client = MilvusClient(**milvus_client_kwargs())
        return self.client

    def _apply_bm25_schema(self, schema, dense_dim: int) -> None:
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
        schema.add_field("memory_scope", DataType.VARCHAR, max_length=256)
        schema.add_field("text", DataType.VARCHAR, max_length=_text_varchar_max(), enable_analyzer=True)
        schema.add_field("turn_index", DataType.INT64)
        schema.add_field("turn_key", DataType.INT64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("bm25_sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="bm25_fn",
                function_type=FunctionType.BM25,
                input_field_names=["text"],
                output_field_names="bm25_sparse",
            )
        )

    def _index_params(self, client: MilvusClient):
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense_embedding",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 256},
        )
        index_params.add_index(
            field_name="bm25_sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        return index_params

    def init_collection(self, dense_dim: int | None = None, *, collection_name: str | None = None) -> None:
        """
        初始化会话记忆集合（服务端 BM25 Function）。
        正式集合名在进程内缓存「已对齐」，热路径不再 describe。
        缺 BM25 的旧集合不在热路径 drop，需跑 python -m app.chat.migrate_memory_bm25。
        collection_name 供迁移脚本创建临时集合。
        """
        if dense_dim is None:
            dense_dim = _dense_dim()
        name = collection_name or self.collection_name
        is_primary = name == self.collection_name
        if is_primary and self._init_done:
            return

        client = self._get_client()
        if client.has_collection(name):
            info = _describe_collection(client, name)
            field_names = _field_names_from_desc(info)
            current = _dense_dim_from_desc(info)
            has_turn_key = "turn_key" in field_names
            has_bm25 = "bm25_sparse" in field_names or _has_bm25_function(info)

            if is_primary and current == dense_dim and has_turn_key:
                self._init_done = True
                self._has_bm25 = has_bm25
                if not has_bm25:
                    logger.warning(
                        "会话记忆集合 %s 仍为客户端稀疏 schema，热路径仅 dense 检索。"
                        "请运行: python -m app.chat.migrate_memory_bm25",
                        name,
                    )
                return

            if not is_primary:
                return

            if current is not None and (current != dense_dim or not has_turn_key):
                logger.warning(
                    "会话记忆 Milvus 集合 %s 的 schema 与当前定义不一致（维度=%s，含 turn_key=%s），将删除后重建（历史记忆向量会清空，后续按 PG 原文惰性重归档）",
                    name,
                    current,
                    has_turn_key,
                )
                self.drop_collection()
            elif current is None:
                logger.warning(
                    "已存在集合 %s 但无法解析 dense_embedding 维度，未自动重建；若 Milvus 报 vector dimension mismatch 请手动 drop 该集合或临时设置 CHAT_MEMORY_MILVUS_RECREATE_ON_INIT=true",
                    name,
                )
                self._init_done = True
                self._has_bm25 = has_bm25
                return
            else:
                self._init_done = True
                self._has_bm25 = has_bm25
                return

        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        self._apply_bm25_schema(schema, dense_dim)
        client.create_collection(
            collection_name=name,
            schema=schema,
            index_params=self._index_params(client),
        )
        if is_primary:
            self._init_done = True
            self._has_bm25 = True

    def insert(self, data: list[dict]) -> None:
        rows = []
        for item in data:
            row = dict(item)
            row.pop("sparse_embedding", None)
            rows.append(row)
        self._get_client().insert(self.collection_name, rows)

    def delete_by_scope(self, memory_scope: str) -> None:
        expr = memory_filter_expr(memory_scope)
        self._get_client().delete(collection_name=self.collection_name, filter=expr)

    def collection_exists(self) -> bool:
        return self._get_client().has_collection(self.collection_name)

    def drop_collection(self) -> None:
        c = self._get_client()
        if c.has_collection(self.collection_name):
            c.drop_collection(self.collection_name)
        self._init_done = False
        self._has_bm25 = False

    def _format_hits(self, results) -> list[dict]:
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

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        query_text: str = "",
        *,
        top_k: int = 8,
        filter_expr: str = "",
        rrf_k: int = 60,
    ) -> list[dict]:
        """混合检索：dense + 服务端 BM25（查询侧传文本）。旧集合无 BM25 时退化为 dense-only。"""
        output_fields = ["text", "turn_index", "turn_key", "chunk_index", "chunk_id", "memory_scope"]
        client = self._get_client()
        q = (query_text or "").strip()
        if self._has_bm25 and q:
            dense_search = AnnSearchRequest(
                data=[dense_embedding],
                anns_field="dense_embedding",
                param={"metric_type": "IP", "params": {"ef": 64}},
                limit=top_k * 2,
                expr=filter_expr,
            )
            sparse_search = AnnSearchRequest(
                data=[q],
                anns_field="bm25_sparse",
                param={"metric_type": "BM25"},
                limit=top_k * 2,
                expr=filter_expr,
            )
            results = client.hybrid_search(
                collection_name=self.collection_name,
                reqs=[dense_search, sparse_search],
                ranker=RRFRanker(k=rrf_k),
                limit=top_k,
                output_fields=output_fields,
            )
            return self._format_hits(results)

        results = client.search(
            collection_name=self.collection_name,
            data=[dense_embedding],
            anns_field="dense_embedding",
            search_params={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k,
            filter=filter_expr,
            output_fields=output_fields,
        )
        return self._format_hits(results)


_mgr: ChatMemoryMilvusManager | None = None


def get_chat_memory_milvus() -> ChatMemoryMilvusManager:
    global _mgr
    if _mgr is None:
        _mgr = ChatMemoryMilvusManager()
    return _mgr


def init_chat_memory_collection() -> None:
    mgr = get_chat_memory_milvus()
    if getattr(settings, "CHAT_MEMORY_MILVUS_RECREATE_ON_INIT", False):
        try:
            mgr.drop_collection()
        except Exception:
            logger.warning("drop chat memory collection failed", exc_info=True)
    mgr.init_collection()
