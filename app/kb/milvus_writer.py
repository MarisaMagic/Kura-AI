"""
批量写入 Milvus（叶子块 + kb_scope）。
通过 EmbeddingService 将文档中的文本转换为密集向量和稀疏向量
通过 MilvusManager 将密集向量和稀疏向量存储到 Milvus 中。
"""

from __future__ import annotations

from app.kb.embedding import EmbeddingService
from app.kb.milvus_client import MilvusManager
from app.settings import settings


def _batch_size() -> int:
    """
    获取批量大小
    :return: 批量大小
    """
    try:
        return max(1, int(settings.EMBEDDING_BATCH_SIZE or 10))
    except ValueError:
        return 10


class MilvusWriter:
    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        milvus_manager: MilvusManager | None = None,
    ) -> None:
        """
        初始化 MilvusWriter
        :param embedding_service: EmbeddingService
        :param milvus_manager: MilvusManager
        :return: None
        """
        self.embedding_service = embedding_service or EmbeddingService()
        self.milvus_manager = milvus_manager or MilvusManager()

    def write_documents(self, documents: list[dict], batch_size: int | None = None) -> None:
        """
        写入文档
        :param documents: 文档列表
        :param batch_size: 批量大小
        :return: None
        """
        if not documents:
            return
        bs = batch_size if batch_size is not None else _batch_size()
        self.milvus_manager.init_collection()
        all_texts = [doc["text"] for doc in documents]
        self.embedding_service.fit_corpus(all_texts)
        total = len(documents)
        for i in range(0, total, bs):
            batch = documents[i : i + bs]
            texts = [doc["text"] for doc in batch]
            # 获取文本的密集向量和稀疏向量
            dense_embeddings, sparse_embeddings = self.embedding_service.get_all_embeddings(texts)
            # 构建插入数据
            insert_data = [
                {
                    "dense_embedding": dense_emb,
                    "sparse_embedding": sparse_emb,
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
                }
                for doc, dense_emb, sparse_emb in zip(batch, dense_embeddings, sparse_embeddings)
            ]
            # 插入数据到 Milvus
            self.milvus_manager.insert(insert_data)
