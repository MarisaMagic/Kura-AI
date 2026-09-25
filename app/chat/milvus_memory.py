"""
会话记忆专用 Milvus 集合（v2）：与知识库 kura_ai_kb 完全隔离。

v2 相对 v1 的关键变化：
- **主键改为 chunk_id（VARCHAR）**：写入天然幂等。v1 用 auto_id INT64，
  一旦 metadata 并发丢更新导致同一轮被重复归档，就会插出重复行且无法 upsert 覆盖；
- 新增 kind（episodic/factual/raw）、from_turn_key/to_turn_key、level、
  fact_key/fact_type、value_score、created_at，支撑蒸馏记忆、分层归并与 TTL 淘汰；
- 稀疏检索仍走服务端 BM25 Function（bm25_fn），与知识库一致。

集合结构不兼容时会自动 drop 重建（向量可由 PG 原文重新归档，不是唯一真相源）；
需要零丢失迁移请改用 python -m app.chat.migrate_memory_v2。
"""

from __future__ import annotations

import logging
import time

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker

from app.kb.milvus_client import _dense_dim, milvus_client_kwargs, milvus_escape
from app.settings import settings

logger = logging.getLogger(__name__)

KIND_EPISODIC = "episodic"
KIND_FACTUAL = "factual"
KIND_RAW = "raw"

FACT_TYPES = ("preference", "decision", "entity", "constraint")

# v2 必备字段；缺任一即视为旧集合，需重建
_REQUIRED_FIELDS = frozenset(
    {
        "chunk_id",
        "memory_scope",
        "text",
        "kind",
        "turn_index",
        "turn_key",
        "from_turn_key",
        "to_turn_key",
        "chunk_index",
        "level",
        "fact_key",
        "fact_type",
        "value_score",
        "created_at",
        "dense_embedding",
        "bm25_sparse",
    }
)

_OUTPUT_FIELDS = [
    "text",
    "kind",
    "turn_index",
    "turn_key",
    "from_turn_key",
    "to_turn_key",
    "chunk_index",
    "chunk_id",
    "level",
    "fact_key",
    "fact_type",
    "value_score",
    "created_at",
    "memory_scope",
]


def _text_varchar_max() -> int:
    return max(512, int(getattr(settings, "CHAT_MEMORY_MILVUS_TEXT_MAX_LENGTH", 8192) or 8192))


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def memory_filter_expr(
    memory_scope: str,
    *,
    turn_keys: list[int] | None = None,
    kinds: list[str] | None = None,
    max_turn_keys: int | None = None,
    to_turn_key_at_most: int | None = None,
) -> str:
    """会话记忆检索过滤表达式。

    :param memory_scope: 隔离键（``s:`` 会话级 / ``u:`` 用户级）
    :param turn_keys: 限定命中的轮次；**空列表表示命中不到任何行**（-1 是不存在的 turn_key）
    :param kinds: 限定记忆类型（episodic / factual / raw）
    :param max_turn_keys: IN 列表长度上限；超出则降级为范围表达式，由调用方过采样后在
        Python 侧精确过滤。旧实现无上限，长会话会拼出几十 KB 的表达式，既慢又可能触发表达式长度限制。
    :param to_turn_key_at_most: 段的右端点不超过该 turn_key（episodic 段的可达性判定）
    """
    esc = milvus_escape(memory_scope)
    expr = f'memory_scope == "{esc}"'
    if kinds:
        quoted = ",".join(f'"{milvus_escape(str(k))}"' for k in kinds)
        expr += f" && kind in [{quoted}]"
    if to_turn_key_at_most is not None:
        expr += f" && to_turn_key <= {int(to_turn_key_at_most)}"
    if turn_keys is not None:
        cap = max_turn_keys if max_turn_keys is not None else _int_setting("CHAT_MEMORY_FILTER_MAX_KEYS", 256)
        keys = [int(k) for k in turn_keys]
        if not keys:
            expr += " && turn_key in [-1]"
        elif len(keys) <= max(1, cap):
            expr += f" && turn_key in [{','.join(str(k) for k in keys)}]"
        else:
            # 降级：只按范围粗筛，精度交给调用方的 Python 侧过滤（检索结果本就带 turn_key）
            expr += f" && turn_key >= {min(keys)} && turn_key <= {max(keys)}"
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


def _pk_is_chunk_id(info: dict) -> bool:
    """主键是否为 chunk_id（VARCHAR）。v1 是 auto_id 的 INT64 id，无法就地升级。"""
    for f in info.get("fields") or []:
        if not isinstance(f, dict):
            continue
        if f.get("is_primary") or f.get("primary_key"):
            return str(f.get("name")) == "chunk_id"
    return False


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

    def _apply_schema(self, schema, dense_dim: int) -> None:
        # chunk_id 作主键：同一 (scope, turn_key, chunk_index) 重复写入即覆盖，天然幂等
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512, is_primary=True)
        schema.add_field("memory_scope", DataType.VARCHAR, max_length=256)
        schema.add_field("kind", DataType.VARCHAR, max_length=16)
        schema.add_field("text", DataType.VARCHAR, max_length=_text_varchar_max(), enable_analyzer=True)
        schema.add_field("turn_index", DataType.INT64)
        schema.add_field("turn_key", DataType.INT64)
        schema.add_field("from_turn_key", DataType.INT64)
        schema.add_field("to_turn_key", DataType.INT64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("level", DataType.INT64)
        schema.add_field("fact_key", DataType.VARCHAR, max_length=191)
        schema.add_field("fact_type", DataType.VARCHAR, max_length=24)
        schema.add_field("value_score", DataType.FLOAT)
        schema.add_field("created_at", DataType.INT64)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
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
        # 标量索引：scope+kind 是几乎所有过滤表达式的公共前缀
        index_params.add_index(field_name="memory_scope", index_type="")
        index_params.add_index(field_name="kind", index_type="")
        return index_params

    def init_collection(self, dense_dim: int | None = None, *, collection_name: str | None = None) -> None:
        """
        初始化会话记忆集合（v2 schema，服务端 BM25 Function）。
        正式集合名在进程内缓存「已对齐」，热路径不再 describe。
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
            has_bm25 = "bm25_sparse" in field_names or _has_bm25_function(info)
            missing = sorted(_REQUIRED_FIELDS - field_names)
            pk_ok = _pk_is_chunk_id(info)
            dim_ok = current == dense_dim

            if is_primary and not missing and pk_ok and dim_ok:
                self._init_done = True
                self._has_bm25 = has_bm25
                if not has_bm25:
                    logger.warning(
                        "会话记忆集合 %s 缺 BM25，热路径仅 dense 检索。请运行: python -m app.chat.migrate_memory_bm25",
                        name,
                    )
                return

            if not is_primary:
                return

            logger.warning(
                "会话记忆集合 %s 与 v2 schema 不兼容（缺失字段=%s，主键为 chunk_id=%s，维度匹配=%s），"
                "将 drop 重建；历史记忆向量会清空，后续按 PG 原文惰性重归档。"
                "需要零丢失迁移请改用: python -m app.chat.migrate_memory_v2",
                name,
                missing or "无",
                pk_ok,
                dim_ok,
            )
            self.drop_collection()

        schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
        self._apply_schema(schema, dense_dim)
        client.create_collection(
            collection_name=name,
            schema=schema,
            index_params=self._index_params(client),
        )
        if is_primary:
            self._init_done = True
            self._has_bm25 = True

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_row(item: dict, *, now_ms: int | None = None) -> dict:
        """补齐 v2 必填字段，避免缺列导致插入失败。"""
        row = dict(item)
        row.pop("sparse_embedding", None)
        row.pop("id", None)  # v1 自增主键残留
        text = str(row.get("text") or "")
        row["text"] = text[: _text_varchar_max()]
        row.setdefault("memory_scope", "")
        row.setdefault("kind", KIND_RAW)
        row.setdefault("turn_index", 0)
        row.setdefault("turn_key", 0)
        row.setdefault("from_turn_key", 0)
        row.setdefault("to_turn_key", 0)
        row.setdefault("chunk_index", 0)
        row.setdefault("level", 0)
        row.setdefault("fact_key", "")
        row.setdefault("fact_type", "")
        row.setdefault("value_score", 0.0)
        row.setdefault("created_at", int(now_ms if now_ms is not None else time.time() * 1000))
        for k in ("turn_index", "turn_key", "from_turn_key", "to_turn_key", "chunk_index", "level", "created_at"):
            try:
                row[k] = int(row[k] or 0)
            except (TypeError, ValueError):
                row[k] = 0
        try:
            row["value_score"] = float(row.get("value_score") or 0.0)
        except (TypeError, ValueError):
            row["value_score"] = 0.0
        row["fact_key"] = str(row.get("fact_key") or "")[:191]
        row["fact_type"] = str(row.get("fact_type") or "")[:24]
        row["kind"] = str(row.get("kind") or KIND_RAW)[:16]
        return row

    def upsert(self, data: list[dict]) -> int:
        """幂等写入：chunk_id 相同即覆盖。返回写入行数。"""
        if not data:
            return 0
        now_ms = int(time.time() * 1000)
        rows = [self.normalize_row(d, now_ms=now_ms) for d in data]
        self._get_client().upsert(self.collection_name, rows)
        return len(rows)

    def insert(self, data: list[dict]) -> int:
        """兼容旧调用名；v2 主键为 chunk_id，统一走 upsert 以保证幂等。"""
        return self.upsert(data)

    # ------------------------------------------------------------------
    # 查询 / 删除
    # ------------------------------------------------------------------

    def query(
        self,
        filter_expr: str,
        *,
        output_fields: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """标量查询（淘汰/归并/GC 用），不做向量检索。"""
        if not filter_expr:
            return []
        try:
            rows = self._get_client().query(
                collection_name=self.collection_name,
                filter=filter_expr,
                output_fields=output_fields or _OUTPUT_FIELDS,
                limit=max(1, int(limit)),
            )
        except Exception:
            logger.exception("chat memory query failed")
            return []
        out: list[dict] = []
        for r in rows or []:
            out.append(r if isinstance(r, dict) else dict(getattr(r, "__dict__", {}) or {}))
        return out

    def delete_by_expr(self, expr: str) -> None:
        if not expr:
            return
        self._get_client().delete(collection_name=self.collection_name, filter=expr)

    def delete_by_scope(self, memory_scope: str) -> None:
        self.delete_by_expr(f'memory_scope == "{milvus_escape(memory_scope)}"')

    def delete_by_chunk_ids(self, chunk_ids: list[str]) -> int:
        """按主键批量删除（归并后清理被合并的段）。"""
        ids = [str(c) for c in (chunk_ids or []) if c]
        if not ids:
            return 0
        n = 0
        # 分批，避免表达式过长
        for i in range(0, len(ids), 200):
            batch = ids[i : i + 200]
            quoted = ",".join(f'"{milvus_escape(c)}"' for c in batch)
            self.delete_by_expr(f"chunk_id in [{quoted}]")
            n += len(batch)
        return n

    def count_by_scope(self, memory_scope: str, *, kind: str | None = None) -> int:
        expr = f'memory_scope == "{milvus_escape(memory_scope)}"'
        if kind:
            expr += f' && kind == "{milvus_escape(kind)}"'
        try:
            rows = self._get_client().query(
                collection_name=self.collection_name,
                filter=expr,
                output_fields=["chunk_id"],
                limit=_int_setting("CHAT_MEMORY_COUNT_LIMIT", 16000),
            )
            return len(rows or [])
        except Exception:
            logger.exception("count_by_scope failed")
            return 0

    def collection_exists(self) -> bool:
        return self._get_client().has_collection(self.collection_name)

    def drop_collection(self) -> None:
        c = self._get_client()
        if c.has_collection(self.collection_name):
            c.drop_collection(self.collection_name)
        self._init_done = False
        self._has_bm25 = False

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def _format_hits(self, results) -> list[dict]:
        formatted: list[dict] = []
        for hits in results:
            for hit in hits:
                formatted.append(
                    {
                        "chunk_id": hit.get("chunk_id", ""),
                        "text": hit.get("text", ""),
                        "kind": hit.get("kind", "") or KIND_RAW,
                        "turn_index": int(hit.get("turn_index", 0) or 0),
                        "turn_key": int(hit.get("turn_key", 0) or 0),
                        "from_turn_key": int(hit.get("from_turn_key", 0) or 0),
                        "to_turn_key": int(hit.get("to_turn_key", 0) or 0),
                        "chunk_index": int(hit.get("chunk_index", 0) or 0),
                        "level": int(hit.get("level", 0) or 0),
                        "fact_key": hit.get("fact_key", "") or "",
                        "fact_type": hit.get("fact_type", "") or "",
                        "value_score": float(hit.get("value_score", 0.0) or 0.0),
                        "created_at": int(hit.get("created_at", 0) or 0),
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
        client = self._get_client()
        q = (query_text or "").strip()
        limit = max(1, int(top_k))
        if self._has_bm25 and q:
            dense_search = AnnSearchRequest(
                data=[dense_embedding],
                anns_field="dense_embedding",
                param={"metric_type": "IP", "params": {"ef": 64}},
                limit=limit * 2,
                expr=filter_expr,
            )
            sparse_search = AnnSearchRequest(
                data=[q],
                anns_field="bm25_sparse",
                param={"metric_type": "BM25"},
                limit=limit * 2,
                expr=filter_expr,
            )
            results = client.hybrid_search(
                collection_name=self.collection_name,
                reqs=[dense_search, sparse_search],
                ranker=RRFRanker(k=rrf_k),
                limit=limit,
                output_fields=_OUTPUT_FIELDS,
            )
            return self._format_hits(results)

        results = client.search(
            collection_name=self.collection_name,
            data=[dense_embedding],
            anns_field="dense_embedding",
            search_params={"metric_type": "IP", "params": {"ef": 64}},
            limit=limit,
            filter=filter_expr,
            output_fields=_OUTPUT_FIELDS,
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
