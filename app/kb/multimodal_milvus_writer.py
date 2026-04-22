"""
多模态批量写入 Milvus（文本块 + 图片块）。
通过 MultimodalEmbeddingService 将文本和图片转换为向量，并通过 MilvusManager 存储到 Milvus 中。
图片块的 text 字段为空，检索依赖多模态向量。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from loguru import logger

from app.kb.image_store import get_image_store
from app.kb.multimodal_embedding import get_multimodal_embedding_service
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

    def write_documents(self, documents: List[dict], batch_size: int | None = None) -> None:
        """
        写入文档（包括文本块和图片块）
        :param documents: 文档列表
        :param batch_size: 批量大小
        :return: None
        """
        if not documents:
            return
        
        bs = batch_size if batch_size is not None else _batch_size()
        self.milvus_manager.init_collection()
        
        # 分离文本块和图片块
        text_chunks = [doc for doc in documents if doc.get("content_type") == "text"]
        image_chunks = [doc for doc in documents if doc.get("content_type") == "image"]
        
        logger.info(f"Writing {len(text_chunks)} text chunks and {len(image_chunks)} image chunks")
        
        # 1. 处理文本块
        if text_chunks:
            all_texts = [doc["text"] for doc in text_chunks]
            self.embedding_service.fit_corpus(all_texts)
            
            total = len(text_chunks)
            for i in range(0, total, bs):
                batch = text_chunks[i : i + bs]
                texts = [doc["text"] for doc in batch]
                
                # 获取文本的密集向量和稀疏向量
                dense_embeddings, sparse_embeddings = self.embedding_service.get_all_embeddings(texts=texts)
                
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
                    for doc, dense_emb, sparse_emb in zip(batch, dense_embeddings, sparse_embeddings)
                ]
                
                # 插入数据到 Milvus
                self.milvus_manager.insert(insert_data)
                logger.info(f"Inserted {len(insert_data)} text chunks to Milvus")
        
        # 2. 处理图片块
        if image_chunks:
            # 先保存图片元数据到数据库
            saved_count = self.image_store.save_images_batch(image_chunks)
            logger.info(f"Saved {saved_count} image metadata to database")
            
            # 批量处理图片向量
            total = len(image_chunks)
            for i in range(0, total, bs):
                batch = image_chunks[i : i + bs]
                batch_paths = [doc.get("image_path", "") for doc in batch]
                
                # 获取图片的密集向量（使用多模态模型）
                try:
                    image_dense_embeddings = self.embedding_service.get_image_embeddings(batch_paths)
                except Exception as e:
                    logger.error(f"Failed to generate image embeddings: {e}")
                    image_dense_embeddings = [[0.0] * int(settings.EMBEDDING_DIM or 1536) for _ in batch]
                
                # 图片块无文本，稀疏向量为空
                image_sparse_embeddings = self.embedding_service.get_sparse_embeddings(["" for _ in batch])
                
                # 构建插入数据
                insert_data = [
                    {
                        "dense_embedding": dense_emb,
                        "sparse_embedding": sparse_emb,
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
                        "chunk_level": doc.get("chunk_level", 4),  # L4 图片块
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
                    for doc, dense_emb, sparse_emb in zip(batch, image_dense_embeddings, image_sparse_embeddings)
                ]
                
                # 插入数据到 Milvus
                self.milvus_manager.insert(insert_data)
                logger.info(f"Inserted {len(insert_data)} image chunks to Milvus")
