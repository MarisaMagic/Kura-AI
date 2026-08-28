"""
多模态批量写入 Milvus（文本块 + 图片块）。
分「embed_documents（只生成向量）」与「write_documents（只插入）」两个阶段，
用于知识库上传「先处理后替换」：全部向量生成成功后才删旧落新。
图片块的 text 字段为空，检索依赖多模态向量。
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from loguru import logger

from app.kb.image_store import get_image_store
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.kb.milvus_client import MilvusManager
from app.settings import settings

# 进度回调：progress_cb(stage, done, total)；stage 见各方法 docstring
ProgressCallback = Callable[[str, int, int], None]


def _batch_size() -> int:
    """
    获取批量大小
    :return: 批量大小
    """
    try:
        return max(1, int(settings.EMBEDDING_BATCH_SIZE or 10))
    except ValueError:
        return 10


def _embedding_request_timeout() -> int:
    """
    嵌入单次 HTTP 调用的短超时（秒），避免 DashScope SDK 默认 300 秒把处理任务悬死
    :return: 超时秒数
    """
    try:
        return max(1, int(settings.KB_UPLOAD_EMBEDDING_HTTP_TIMEOUT_SECONDS or 90))
    except ValueError:
        return 90


class MultimodalMilvusWriter:
    def __init__(
        self,
        embedding_service=None,
        milvus_manager: MilvusManager | None = None,
    ) -> None:
        """
        初始化 MultimodalMilvusWriter
        :param embedding_service: 多模态嵌入服务
        :param milvus_manager: Milvus管理器
        :return: None
        """
        self.embedding_service = embedding_service or get_multimodal_embedding_service()
        self.milvus_manager = milvus_manager or MilvusManager()
        self.image_store = get_image_store()

    def embed_documents(
        self,
        documents: List[dict],
        batch_size: int | None = None,
        progress_cb: ProgressCallback | None = None,
        tick_cb: Callable[[], None] | None = None,
    ) -> Tuple[List[List[float]], List[List[float]]]:
        """
        仅生成文本块与图片块的密集向量，不写入 Milvus（「先处理后替换」的前半段）。
        返回 (text_embeddings, image_embeddings)，分别与文档列表中的文本块/图片块顺序对齐。
        :param documents: 文档块列表（叶子块，content_type 区分 text/image）
        :param batch_size: 批量大小
        :param progress_cb: 每批后回调，stage 为 "text_embedding" / "image_embedding"
        :param tick_cb: 每张图片处理前回调（协作式取消/超时检查）
        :return: 文本/图片向量二元组
        """
        if not documents:
            return [], []

        bs = batch_size if batch_size is not None else _batch_size()
        timeout = _embedding_request_timeout()

        text_chunks = [doc for doc in documents if doc.get("content_type") == "text"]
        image_chunks = [doc for doc in documents if doc.get("content_type") == "image"]
        text_embeddings: List[List[float]] = []
        image_embeddings: List[List[float]] = []

        # 1. 文本块嵌入（稀疏向量由服务端 BM25 Function 基于 text 自动计算，无需客户端生成）
        if text_chunks:
            total = len(text_chunks)
            done = 0
            for i in range(0, total, bs):
                batch = text_chunks[i : i + bs]
                texts = [doc["text"] for doc in batch]

                dense_embeddings = self.embedding_service.get_text_embeddings(texts, request_timeout=timeout)
                if len(dense_embeddings) != len(batch):
                    raise RuntimeError(
                        f"嵌入服务返回向量数量不一致：请求 {len(batch)} 条，返回 {len(dense_embeddings)} 条"
                    )

                text_embeddings.extend(dense_embeddings)
                done += len(batch)
                if progress_cb is not None:
                    progress_cb("text_embedding", done, total)
                logger.info(f"Embedded {done}/{total} text chunks")

        # 2. 图片块嵌入（逐张串行，短 HTTP 超时 + 逐图检查点）
        if image_chunks:
            total = len(image_chunks)
            batch_paths = [doc.get("image_path", "") for doc in image_chunks]
            dense = self.embedding_service.get_image_embeddings(
                batch_paths,
                request_timeout=timeout,
                tick_cb=(lambda: (tick_cb() if tick_cb is not None else None)),
            )
            image_embeddings.extend(dense)
            if progress_cb is not None:
                progress_cb("image_embedding", total, total)
            logger.info(f"Embedded {total}/{total} image chunks")

        return text_embeddings, image_embeddings

    def write_documents(
        self,
        documents: List[dict],
        batch_size: int | None = None,
        text_embeddings: List[List[float]] | None = None,
        image_embeddings: List[List[float]] | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> None:
        """
        写入文档（「先处理后替换」的后半段：只做纯写入，不调用嵌入 API）。
        :param documents: 文档块列表
        :param batch_size: 批量大小
        :param text_embeddings: 已算好的文本块向量（与文本块顺序对齐）；None 则现算
        :param image_embeddings: 已算好的图片块向量（与图片块顺序对齐）；None 则现算
        :param progress_cb: 每批后回调，stage 为 "insert"
        :return: None
        """
        if not documents:
            return

        bs = batch_size if batch_size is not None else _batch_size()
        self.milvus_manager.init_collection()

        # 分离文本块和图片块
        text_chunks = [doc for doc in documents if doc.get("content_type") == "text"]
        image_chunks = [doc for doc in documents if doc.get("content_type") == "image"]

        emit = progress_cb or (lambda stage, done, total: None)
        total_chunks = max(1, len(text_chunks) + len(image_chunks))
        inserted = 0

        # 1. 文本块插入（稀疏向量由服务端 BM25 Function 基于 text 自动计算）
        if text_chunks:
            if text_embeddings is None:
                text_embeddings, _ = self.embed_documents(documents)
            if len(text_embeddings) != len(text_chunks):
                raise RuntimeError(
                    f"文本块向量数量不一致：文本块 {len(text_chunks)} 个，向量 {len(text_embeddings)} 个"
                )

            for i in range(0, len(text_chunks), bs):
                batch = text_chunks[i : i + bs]
                dense = text_embeddings[i : i + bs]

                insert_data = [
                    {
                        "dense_embedding": dense_emb,
                        "kb_scope": doc["kb_scope"],
                        "text": doc["text"],
                        "filename": doc["filename"],
                        "file_type": doc["file_type"],
                        "file_path": doc.get("file_path", ""),
                        "page_number": doc.get("page_number", 0),
                        "chunk_idx": doc.get("chunk_idx", 0),
                        "chunk_id": doc.get("chunk_id", ""),
                        "parent_chunk_id": doc.get("parent_chunk_id", ""),
                        "root_chunk_id": doc.get("root_chunk_id", ""),
                        "chunk_level": doc.get("chunk_level", 0),
                        "content_type": "text",
                        "image_path": "",
                        # 文本块位置信息
                        "position_start": doc.get("position_start", 0),
                        "position_end": doc.get("position_end", 0),
                        # 图片位置信息（文本块为0）
                        "image_position_x": 0,
                        "image_position_y": 0,
                        "image_width": 0,
                        "image_height": 0,
                    }
                    for doc, dense_emb in zip(batch, dense)
                ]

                self.milvus_manager.insert(insert_data)
                inserted += len(batch)
                emit("insert", inserted, total_chunks)
                logger.info(f"Inserted {len(insert_data)} text chunks to Milvus")

        # 2. 图片块：先保存图片元数据到数据库，再插入向量
        if image_chunks:
            saved_count = self.image_store.save_images_batch(image_chunks)
            logger.info(f"Saved {saved_count} image metadata to database")

            if image_embeddings is None:
                _, image_embeddings = self.embed_documents(documents)
            if len(image_embeddings) != len(image_chunks):
                raise RuntimeError(
                    f"图片块向量数量不一致：图片块 {len(image_chunks)} 个，向量 {len(image_embeddings)} 个"
                )

            for i in range(0, len(image_chunks), bs):
                batch = image_chunks[i : i + bs]
                dense = image_embeddings[i : i + bs]

                insert_data = [
                    {
                        "dense_embedding": dense_emb,
                        "kb_scope": doc["kb_scope"],
                        "text": "",  # 图片块不存储文本
                        "filename": doc["filename"],
                        "file_type": doc["file_type"],
                        "file_path": doc.get("file_path", ""),
                        "page_number": doc.get("page_number", 0),
                        "chunk_idx": doc.get("chunk_idx", 0),
                        "chunk_id": doc.get("chunk_id", ""),
                        "parent_chunk_id": doc.get("parent_chunk_id", ""),
                        "root_chunk_id": doc.get("root_chunk_id", ""),
                        "chunk_level": 4,  # L4 图片块
                        "content_type": "image",
                        "image_path": doc.get("image_path", ""),
                        # 文本位置信息（图片块为0）
                        "position_start": 0,
                        "position_end": 0,
                        # 图片位置信息
                        "image_position_x": doc.get("image_position_x", 0),
                        "image_position_y": doc.get("image_position_y", 0),
                        "image_width": doc.get("image_width", 0),
                        "image_height": doc.get("image_height", 0),
                    }
                    for doc, dense_emb in zip(batch, dense)
                ]

                self.milvus_manager.insert(insert_data)
                inserted += len(batch)
                emit("insert", inserted, total_chunks)
                logger.info(f"Inserted {len(insert_data)} image chunks to Milvus")